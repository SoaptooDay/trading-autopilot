"""
Trading autopilot - PAPER ONLY.

Runs the day-trader skills' scripts in order, with no human in the loop, and
places bracket orders on an Alpaca PAPER account:

  scan (opening-range breakouts) -> news filter -> exit plan (stop + 2R target)
  -> time machine (edge filter) -> size -> risk check -> paper bracket order
  -> flatten everything at flat_by -> journal the day's P&L

The broker URL is hard-coded to Alpaca's paper endpoint. There is no switch to
live trading in this file, on purpose.

Usage (local; on GitHub see .github/workflows):
  python autopilot\\autopilot.py             # run today's session (waits for the open)
  python autopilot\\autopilot.py --dry-run   # do everything except send orders
  python autopilot\\autopilot.py --status    # check keys, account and market clock
"""
import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # the code folder
SKILLS = os.path.join(ROOT, "skills")
# Private data (config, state, journal, logs) lives in AUTOPILOT_DATA - on GitHub
# Actions that is a checkout of the PRIVATE trading-journal repo.
DATA = os.path.abspath(os.environ.get("AUTOPILOT_DATA") or ROOT)
TRADER = os.path.join(DATA, "trader")
LOGS = os.path.join(DATA, "logs")
JOURNAL = os.path.join(DATA, "journal.csv")
SETTINGS = os.path.join(DATA, "settings.json")
ENV_FILE = os.path.join(HERE, "keys.txt")
QUIET = os.environ.get("AUTOPILOT_QUIET") == "1"   # public CI logs: print nothing about trades

PAPER_URL = "https://paper-api.alpaca.markets"     # paper only - never change

DEFAULT_SKILL_CONFIG = {
    "budget": 2000, "cash_buffer_pct": 10, "max_position_pct": 75,
    "max_order_usd": 2000, "min_order_usd": 5,
    "max_loss_per_trade_usd": 20, "max_daily_loss_usd": 40,
    "max_trades_per_day": 1, "max_orders_per_day": 10,
    "min_reward_risk": 2.0, "require_stop": True,
    "no_entries_before": "09:45", "no_entries_after": "12:00", "flat_by": "15:55",
    "fractional": False, "broker": "alpaca", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": False, "sec_user_agent": "Your Name you@example.com",
}

DEFAULT_SETTINGS = {
    "symbols": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                "AVGO", "JPM", "V", "MA", "UNH", "XOM", "LLY", "COST", "HD", "PG", "JNJ",
                "BAC", "WMT", "NFLX", "AMD", "CRM", "ORCL", "ADBE", "KO", "PEP", "MRK",
                "ABBV", "CVX", "CSCO", "INTC", "QCOM", "TXN", "MU", "AMAT", "WFC", "GS",
                "MS", "C", "DIS", "NKE", "MCD", "CAT", "BA", "GE", "UBER", "PYPL"],
    "target_r": 2.0,
    "stop_rule": "or-mid",
    "check_every_minutes": 5,
    "require_edge": True,          # skip trades the time machine says have no edge
    "min_history_trades": 10,      # need this many past triggers before trusting history
    "skip_news_tags": ["halt", "risk", "legal"],
    "skip_if_earnings_filing_today": True,
}


# ------------------------------------------------------------------ helpers
def log(msg):
    os.makedirs(LOGS, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    if not QUIET:
        print(line, flush=True)
    with open(os.path.join(LOGS, dt.date.today().isoformat() + ".log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_env():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ensure_configs():
    cfg_path = os.path.join(TRADER, "config.json")
    cfg = read_json(cfg_path)
    if cfg is None:
        cfg = dict(DEFAULT_SKILL_CONFIG)
        write_json(cfg_path, cfg)
        log("Created trader/config.json with default limits - edit it to change them.")
    cfg["paper"] = True
    settings = read_json(SETTINGS)
    if settings is None:
        settings = dict(DEFAULT_SETTINGS)
        write_json(SETTINGS, settings)
    for k, v in DEFAULT_SETTINGS.items():
        settings.setdefault(k, v)
    return cfg, settings


# ------------------------------------------------------------------ skill scripts
EXTRA_ARGS = {}   # step -> extra CLI args (the offline test uses this)


def run_skill(step, script, args):
    """Run one skill script from the trading folder; return (exit code, stdout)."""
    path = os.path.join(SKILLS, script)
    cmd = [sys.executable, "-I", path] + args + EXTRA_ARGS.get(step, [])
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=DATA, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    if p.returncode not in (0, 1):
        log("  %s failed (exit %s): %s" % (step, p.returncode, (p.stderr or p.stdout).strip()[-400:]))
    return p.returncode, p.stdout


def run_json(step, script, args):
    code, out = run_skill(step, script, args)
    try:
        return code, json.loads(out)
    except ValueError:
        return code, None


# ------------------------------------------------------------------ Alpaca paper
class AlpacaPaper:
    def __init__(self):
        self.key = os.environ.get("ALPACA_API_KEY_ID", "")
        self.secret = os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not self.key or not self.secret or "PASTE" in self.key:
            raise SystemExit("No Alpaca paper keys. Put them in autopilot\\keys.txt (see keys.example.txt).")
        assert PAPER_URL == "https://paper-api.alpaca.markets"

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(PAPER_URL + path, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError("Alpaca %s %s -> %s %s" % (method, path, e.code, e.read().decode()[:300]))

    def account(self):
        return self._req("GET", "/v2/account")

    def clock(self):
        return self._req("GET", "/v2/clock")

    def bracket(self, symbol, qty, take_profit, stop_loss):
        return self._req("POST", "/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": "buy", "type": "market",
            "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": str(take_profit)},
            "stop_loss": {"stop_price": str(stop_loss)}})

    def flatten(self):
        self._req("DELETE", "/v2/orders")
        return self._req("DELETE", "/v2/positions?cancel_orders=true")

    def fills_today(self, day):
        return self._req("GET", "/v2/account/activities/FILL?date=%s" % day) or []


# ------------------------------------------------------------------ clock
def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class MarketClock:
    """New York time from Alpaca's clock (avoids needing tzdata on Windows)."""
    def __init__(self, broker):
        self.broker = broker
        c = broker.clock()
        ts = parse_ts(c["timestamp"])
        self.offset = ts.utcoffset()
        self.is_open = c["is_open"]
        self.next_open = parse_ts(c["next_open"])
        self.next_close = parse_ts(c["next_close"])

    def now_ny(self):
        return (dt.datetime.now(dt.timezone.utc) + self.offset).replace(tzinfo=None)

    def sleep_until(self, hhmm):
        h, m = map(int, hhmm.split(":"))
        while True:
            now = self.now_ny()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            left = (target - now).total_seconds()
            if left <= 0:
                return
            time.sleep(min(left, 300))


def hhmm_add(hhmm, minutes):
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutes
    return "%02d:%02d" % (t // 60, t % 60)


def at_to_hhmm(at):
    """'9:50 AM' -> '09:50'"""
    t = dt.datetime.strptime(at.strip().upper(), "%I:%M %p")
    return t.strftime("%H:%M")


# ------------------------------------------------------------------ the steps
def scan(settings, now_str):
    code, doc = run_json("scan", "trader-scanner/scan.py",
                         ["orb", "--symbols", ",".join(settings["symbols"]), "--json",
                          "--quiet", "--no-write", "--pause", "0.3"])
    if not doc:
        return []
    hits = [h for h in doc.get("hits", []) if "orb" in h.get("setups", [])]
    return sorted(hits, key=lambda h: h.get("pct_over", 0))   # closest to the range first


def news_ok(symbol, settings, cfg):
    args = ["--tickers", symbol, "--hours", "18", "--dry-run", "--json", "--no-moves"]
    if "you@example.com" in cfg.get("sec_user_agent", "you@example.com"):
        args.append("--no-sec")
    code, doc = run_json("news", "trader-news/news.py", args)
    if not doc:
        return True, "news unavailable"
    for it in doc.get("items", []):
        if symbol not in (it.get("touches") or [it.get("ticker")]):
            continue
        bad = set(it.get("tags", [])) & set(settings["skip_news_tags"])
        if bad:
            return False, "%s news: %s" % ("/".join(sorted(bad)), it.get("title", "")[:80])
        if (settings["skip_if_earnings_filing_today"] and it.get("kind") == "filing"
                and "earnings" in it.get("tags", [])):
            return False, "earnings filing: " + it.get("title", "")[:80]
    return True, "no blocking news"


def fresh_price(symbol):
    """Latest quote, also written to trader/prices.json so the risk check sizes off it."""
    code, doc = run_json("quote", "trader-alerts/alerts.py", ["quote", symbol, "--json", "--write"])
    q = ((doc or {}).get("quotes") or {}).get(symbol) or {}
    return q.get("price")


def is_fresh(hit, now, settings):
    """Only take a breakout whose trigger bar closed recently (not one from an hour ago)."""
    try:
        h, m = map(int, hit["bar_start"].split(":"))
    except (KeyError, ValueError):
        return True
    bar_close = now.replace(hour=h, minute=m, second=0, microsecond=0) + dt.timedelta(minutes=5)
    return (now - bar_close).total_seconds() <= (settings["check_every_minutes"] + 10) * 60


def plan(hit, settings, entry):
    code, out = run_skill("exit", "trader-exit/exit.py",
                          ["plan", "--symbol", hit["symbol"], "--entry", "%.2f" % entry,
                           "--or-high", str(hit["or_high"]), "--or-low", str(hit["or_low"]),
                           "--stop", settings["stop_rule"], "--target", "%gR" % settings["target_r"],
                           "--write"])
    p = read_json(os.path.join(TRADER, "plan.json"))
    if code != 0 or not p or p.get("symbol") != hit["symbol"]:
        return None
    return p


def time_machine(p, hit, cfg, settings):
    code, doc = run_json("timemachine", "trader-timemachine/timemachine.py",
                         ["run", "--symbol", p["symbol"], "--date", p["date"],
                          "--entry", str(p["entry"]), "--stop", str(p["stop"]),
                          "--target", str(p["target"]), "--entry-time", hit["at"],
                          "--until", cfg.get("flat_by", "15:55"), "--json", "--write"])
    if not doc:
        return (not settings["require_edge"]), "time machine unavailable", {}
    mc, hist = doc.get("mc", {}), doc.get("history", {})
    stats = {"tm_p_target": mc.get("p_target"), "tm_p_stop": mc.get("p_stop"),
             "tm_avg_R": mc.get("avg_R"), "hist_triggered": hist.get("triggered"),
             "hist_avg_R": hist.get("avg_R"), "verdict": doc.get("verdict", "")}
    why = "sim avgR %+.2f, history %s trades avgR %s | %s" % (
        mc.get("avg_R") or 0, hist.get("triggered"), hist.get("avg_R"), doc.get("verdict", ""))
    if not settings["require_edge"]:
        return True, why, stats
    edge = (mc.get("avg_R") or 0) > 0
    if (hist.get("triggered") or 0) >= settings["min_history_trades"]:
        edge = edge and (hist.get("avg_R") or 0) > 0
    return edge, why, stats


def size_and_check(now_str):
    code, doc = run_json("size", "trader-budget/size.py", ["--write", "--json"])
    s = (doc or {}).get("sizing", {})
    if not doc or s.get("refusal") or not s.get("qty"):
        return None, "sizing refused: %s" % (s.get("refusal") or "no shares fit")
    code, risk = run_json("risk", "trader-risk/check.py", ["--json", "--now", now_str])
    r = ((risk or {}).get("results") or [{}])[0]
    if r.get("verdict") != "PASS":
        return None, "risk BLOCK: %s" % "; ".join(r.get("reasons") or ["unknown"])
    return s, "risk PASS"


def record_placed(s, day, order_id):
    path = os.path.join(TRADER, "state", "broker.json")
    st = read_json(path, {"orders": []})
    st.setdefault("orders", []).append({
        "status": "placed", "date": day, "symbol": s["symbol"], "side": "buy",
        "kind": "entry", "qty": s["qty"], "max_loss": s["max_loss"],
        "broker_id": order_id, "by": "autopilot (paper)"})
    write_json(path, st)


def journal(row):
    new = not os.path.exists(JOURNAL)
    cols = ["date", "time_ny", "symbol", "action", "qty", "entry", "stop", "target",
            "max_loss", "reason", "tm_p_target", "tm_p_stop", "tm_avg_R",
            "hist_triggered", "hist_avg_R", "pnl"]
    with open(JOURNAL, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def day_pnl(broker, day):
    by = {}
    for f in broker.fills_today(day):
        q, px = float(f["qty"]), float(f["price"])
        by[f["symbol"]] = by.get(f["symbol"], 0.0) + (q * px if f["side"] == "sell" else -q * px)
    return {k: round(v, 2) for k, v in by.items()}


# ------------------------------------------------------------------ the session
def session(broker, clock, cfg, settings, dry_run=False, no_flatten=False):
    day = clock.now_ny().date().isoformat()
    open_ny = (clock.next_open + clock.offset).replace(tzinfo=None) if not clock.is_open else None
    if open_ny and open_ny.date() != clock.now_ny().date():
        log("Market is closed today (next open %s). Nothing to do." % open_ny.strftime("%a %d %b %H:%M ET"))
        return
    first_check = max(cfg.get("no_entries_before", "09:45"), "09:50")
    last_entry = cfg.get("no_entries_after", "12:00")
    flat_by = cfg.get("flat_by", "15:55")
    acct = broker.account()
    log("PAPER account %s: equity $%s. Window %s-%s ET, flat by %s ET, max %s trade(s)/day%s."
        % (acct.get("account_number"), acct.get("equity"), first_check, last_entry, flat_by,
           cfg.get("max_trades_per_day"), " [DRY RUN]" if dry_run else ""))
    clock.sleep_until(first_check)

    seen, trades = set(), 0
    while clock.now_ny().strftime("%H:%M") <= last_entry and trades < cfg.get("max_trades_per_day", 1):
        now = clock.now_ny()
        now_str = now.strftime("%Y-%m-%d %H:%M")
        hits = [h for h in scan(settings, now_str) if h["symbol"] not in seen]
        log("%s ET scan: %d new breakout(s)%s" % (now.strftime("%H:%M"), len(hits),
            (": " + ", ".join(h["symbol"] for h in hits)) if hits else ""))
        for hit in hits:
            seen.add(hit["symbol"])
            row = {"date": day, "time_ny": now.strftime("%H:%M"), "symbol": hit["symbol"]}
            if not is_fresh(hit, now, settings):
                why = "stale breakout (triggered %s ET)" % hit.get("at")
                log("  %s skip - %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason=why)); continue
            ok, why = news_ok(hit["symbol"], settings, cfg)
            if not ok:
                log("  %s skip - %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason=why)); continue
            px = fresh_price(hit["symbol"]) or hit["price"]
            if px <= hit["or_high"]:
                why = "price %.2f fell back under the range high %.2f" % (px, hit["or_high"])
                log("  %s skip - %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason=why)); continue
            p = plan(hit, settings, max(px, hit["price"]))
            if not p:
                why = "no valid exit plan"
                log("  %s skip - %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason=why)); continue
            row.update(entry=p["entry"], stop=p["stop"], target=p["target"])
            ok, why, stats = time_machine(p, hit, cfg, settings)
            row.update(stats)
            if not ok:
                log("  %s skip - no edge: %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason="no edge: " + why)); continue
            s, why = size_and_check(now_str)
            if not s:
                log("  %s skip - %s" % (hit["symbol"], why)); journal(dict(row, action="skip", reason=why)); continue
            row.update(qty=s["qty"], max_loss=s["max_loss"])
            if dry_run:
                log("  %s WOULD BUY %s @ ~%s, stop %s, target %s (max loss $%s) [dry run]"
                    % (s["symbol"], s["qty"], s["entry"], s["stop"], s["target"], s["max_loss"]))
                journal(dict(row, action="dry-run buy", reason=why))
            else:
                o = broker.bracket(s["symbol"], s["qty"], "%.2f" % s["target"], "%.2f" % s["stop"])
                record_placed(s, day, o.get("id"))
                log("  %s PAPER BUY %s, stop %s, target %s (max loss $%s) - order %s"
                    % (s["symbol"], s["qty"], s["stop"], s["target"], s["max_loss"], o.get("id")))
                journal(dict(row, action="paper buy", reason=why))
            trades += 1
            if trades >= cfg.get("max_trades_per_day", 1):
                break
        if trades >= cfg.get("max_trades_per_day", 1):
            break
        time.sleep(settings["check_every_minutes"] * 60)

    log("Entry window over (%d trade(s)).%s" % (trades, "" if no_flatten else " Waiting to flatten at %s ET." % flat_by))
    if not no_flatten and not dry_run:
        close_out(broker, clock, cfg, day)
    log("Session done.")


def close_out(broker, clock, cfg, day):
    """Wait for flat_by, close every paper position, journal the day's P&L."""
    clock.sleep_until(cfg.get("flat_by", "15:55"))
    broker.flatten()
    log("Flattened all paper positions at %s ET." % clock.now_ny().strftime("%H:%M"))
    time.sleep(60)
    pnl = day_pnl(broker, day)
    for sym, v in pnl.items():
        journal({"date": day, "symbol": sym, "action": "day result", "pnl": v})
    if not pnl:
        journal({"date": day, "symbol": "-", "action": "day result", "pnl": 0, "reason": "no trade"})
    log("Day P&L (paper, USD): %s" % (pnl or "no trade today"))


def market_day(clock):
    """True if NYSE trades today (New York date)."""
    if clock.is_open:
        return True
    return (clock.next_open + clock.offset).date() == clock.now_ny().date()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper-only trading autopilot")
    ap.add_argument("--dry-run", action="store_true", help="do everything except send orders")
    ap.add_argument("--status", action="store_true", help="check keys, account and clock, then stop")
    ap.add_argument("--no-flatten", action="store_true",
                    help="end after the entry window; a separate --flatten run closes positions")
    ap.add_argument("--flatten", action="store_true", help="only: wait for flat_by, close all, journal P&L")
    a = ap.parse_args(argv)
    try:
        _main(a)
    except SystemExit:
        raise
    except Exception as e:
        log("ERROR: %s" % e)
        journal({"date": dt.date.today().isoformat(), "symbol": "-", "action": "error", "reason": str(e)[:300]})
        if QUIET:
            print("autopilot: error (details in the private journal log)", flush=True)
            sys.exit(1)
        raise
    if QUIET:
        print("autopilot: ok", flush=True)


def _main(a):
    load_env()
    cfg, settings = ensure_configs()
    broker = AlpacaPaper()
    clock = MarketClock(broker)
    if a.status:
        acct = broker.account()
        log("Connected to Alpaca PAPER. Account %s, status %s, equity $%s, buying power $%s."
            % (acct.get("account_number"), acct.get("status"), acct.get("equity"), acct.get("buying_power")))
        log("Market open now: %s. Next open: %s (New York time)." % (clock.is_open, (clock.next_open + clock.offset).strftime("%a %d %b %H:%M")))
        log("Limits: budget $%s, max loss/trade $%s, max daily loss $%s, %s trade(s)/day, %sR target."
            % (cfg["budget"], cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"],
               cfg["max_trades_per_day"], settings["target_r"]))
        return
    if a.flatten:
        if not market_day(clock):
            log("Market closed today - nothing to flatten.")
            return
        close_out(broker, clock, cfg, clock.now_ny().date().isoformat())
        return
    try:
        session(broker, clock, cfg, settings, dry_run=a.dry_run, no_flatten=a.no_flatten)
    except Exception:  # never leave a paper position open because of a crash
        if not a.dry_run and not a.no_flatten:
            try:
                close_out(broker, clock, cfg, clock.now_ny().date().isoformat())
            except Exception as e2:
                log("Could not flatten: %s - check Alpaca." % e2)
        raise


if __name__ == "__main__":
    main()
