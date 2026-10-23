"""
Strategy research: daily shadow tracking and backtests. Paper/simulation only,
never places orders.

  python autopilot/research.py backtest [--days 60]   # replay every tracked strategy
  python autopilot/research.py day                     # score today's session (after the close)

Prices: Alpaca market data (5-minute bars), read with the same paper keys.
Writes to AUTOPILOT_DATA (the private journal):
  research/backtest.json, research/backtest_trades.csv
  research/signals.csv  (every tracked signal, one row per strategy/stock/day)
  research/tracked.json (comparison since tracking started)
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap          # paths, settings, config, logging
import engine
import strategies

NY = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
RESEARCH = os.path.join(ap.DATA, "research")
SIGNALS = os.path.join(RESEARCH, "signals.csv")
SIG_COLS = ["date", "strategy", "symbol", "time_ny", "entry", "stop", "target", "r", "outcome", "why"]

DEFAULT_RESEARCH = {
    "track": ["orb", "orb_filtered"],
    "combos": {},                        # e.g. {"ORB + Strategy 2": ["orb_filtered", "s2"]}
    "alone_max_trades_per_day": 1,
    "combo_max_trades_per_day": 2,
}


# ------------------------------------------------------------------ data
def fetch_bars(symbols, start, end):
    """{symbol: {date: [bars]}} for regular-session 5-minute bars, New York time."""
    key, secret = os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", "")
    out = {s: {} for s in symbols}
    for feed in ("sip", "iex"):
        try:
            token, raw = None, {}
            while True:
                q = {"symbols": ",".join(symbols), "timeframe": "5Min", "start": start, "end": end,
                     "limit": 10000, "adjustment": "raw", "feed": feed, "sort": "asc"}
                if token:
                    q["page_token"] = token
                req = urllib.request.Request(DATA_URL + "?" + urllib.parse.urlencode(q), headers={
                    "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
                with urllib.request.urlopen(req, timeout=60) as r:
                    doc = json.loads(r.read())
                for sym, bars in (doc.get("bars") or {}).items():
                    raw.setdefault(sym, []).extend(bars)
                token = doc.get("next_page_token")
                if not token:
                    break
            break
        except urllib.error.HTTPError as e:
            if feed == "sip" and e.code in (401, 403, 422):
                ap.log("SIP data not available on this plan (%s); using IEX." % e.code)
                continue
            raise
    for sym, bars in raw.items():
        for b in bars:
            t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY).replace(tzinfo=None)
            m = t.hour * 60 + t.minute
            if 9 * 60 + 30 <= m < 16 * 60:
                out[sym].setdefault(t.date().isoformat(), []).append(
                    {"t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    ap.log("Fetched bars for %d symbols (%s feed)." % (sum(1 for s in out if out[s]), feed))
    return out


# ------------------------------------------------------------------ core
def candidates_for_day(day, bars, strats):
    """All signals every strategy gives on `day`, each with its simulated R."""
    out = []
    for sym, by_day in bars.items():
        today = by_day.get(day)
        if not today:
            continue
        prior = [by_day[d] for d in sorted(by_day) if d < day]
        for s in strats:
            for sig in s.signals(today, prior, engine.simulate):
                r = engine.simulate(sig, today)
                out.append({"date": day, "strategy": s.name, "symbol": sym, "sig": sig, "r": r,
                            "outcome": engine.outcome_label(r, getattr(s, "target_r", 2.0)),
                            "rank": (sig["entry"] - sig["stop"]) / sig["entry"]})
    return out


def compare(cands_by_day, rs, cfg, fx):
    """Results for each strategy alone and each combo, applying the same limits."""
    groups = [(n, strategies.REGISTRY[n].label, [n], rs["alone_max_trades_per_day"], "strategy")
              for n in rs["track"] if n in strategies.REGISTRY]
    groups += [(k, k, v, rs["combo_max_trades_per_day"], "combo") for k, v in rs["combos"].items()]
    results, equity, taken_all = [], [], []
    for key, label, members, max_t, kind in groups:
        cum, taken = 0.0, []
        for day in sorted(cands_by_day):
            t = engine.pick_day([c for c in cands_by_day[day] if c["strategy"] in members], cfg, max_t)
            taken += t
            cum += sum(x["pnl_usd"] for x in t) / fx
            equity.append({"date": day, "name": key, "cum_gbp": round(cum, 2)})
        results.append(dict(engine.score(taken, fx), name=key, label=label, kind=kind,
                            members=" + ".join(members)))
        taken_all += [dict(x, group=key) for x in taken]
    return results, equity, taken_all


def settings_research():
    _, settings = ap.ensure_configs()
    rs = dict(DEFAULT_RESEARCH)
    rs.update(settings.get("research") or {})
    if "research" not in settings:
        settings["research"] = rs
        ap.write_json(ap.SETTINGS, settings)
    return rs, float(settings.get("usd_per_gbp", 1.34)), settings


# ------------------------------------------------------------------ commands
def backtest(days):
    cfg, _ = ap.ensure_configs()
    rs, fx, settings = settings_research()
    strats = strategies.get(rs["track"])
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    start = end - dt.timedelta(days=int(days * 7 / 5) + 100)      # test window + 60-session history
    bars = fetch_bars(settings["symbols"], start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    all_days = sorted({d for by in bars.values() for d in by})
    now_ny = end.astimezone(NY)
    done_today = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 5     # today's session is complete
    test_days = [d for d in all_days if d < now_ny.date().isoformat()
                 or (done_today and d == now_ny.date().isoformat())][-days:]
    cands = {d: candidates_for_day(d, bars, strats) for d in test_days}
    results, equity, taken = compare(cands, rs, cfg, fx)
    os.makedirs(RESEARCH, exist_ok=True)
    ap.write_json(os.path.join(RESEARCH, "backtest.json"), {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "from": test_days[0] if test_days else None, "to": test_days[-1] if test_days else None,
        "sessions": len(test_days), "symbols": len(settings["symbols"]), "usd_per_gbp": fx,
        "notes": "Simulated fills at bar closes, stop assumed first when a bar touches both, "
                 "no news filter, edge filter uses history only (no 10,000-run simulation).",
        "results": results, "equity": equity})
    with open(os.path.join(RESEARCH, "backtest_trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "date", "strategy", "symbol", "time_ny", "entry", "stop", "target", "qty", "r", "outcome", "pnl_gbp"])
        for t in taken:
            s = t["sig"]
            w.writerow([t["group"], t["date"], t["strategy"], t["symbol"], s["time"].strftime("%H:%M"),
                        s["entry"], s["stop"], s["target"], t["qty"], t["r"], t["outcome"], round(t["pnl_usd"] / fx, 2)])
    for r in results:
        ap.log("Backtest %s: %s trades, win %s, avg %sR, total £%s" % (
            r["name"], r["trades"], r["win_rate"], r["avg_r"], r["total_gbp"]))


def day(today=None):
    cfg, _ = ap.ensure_configs()
    rs, fx, settings = settings_research()
    strats = strategies.get(rs["track"])
    today = today or dt.datetime.now(NY).date().isoformat()
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
    start = end - dt.timedelta(days=100)
    bars = fetch_bars(settings["symbols"], start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not any(today in by for by in bars.values()):
        ap.log("No session data for %s (market closed?) - nothing to track." % today)
        return
    cands = candidates_for_day(today, bars, strats)
    os.makedirs(RESEARCH, exist_ok=True)
    rows = []
    if os.path.exists(SIGNALS):
        with open(SIGNALS, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != today]
    for c in cands:
        s = c["sig"]
        rows.append({"date": today, "strategy": c["strategy"], "symbol": c["symbol"],
                     "time_ny": s["time"].strftime("%H:%M"), "entry": s["entry"], "stop": s["stop"],
                     "target": s["target"], "r": c["r"], "outcome": c["outcome"], "why": s["why"]})
    with open(SIGNALS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIG_COLS)
        w.writeheader()
        w.writerows(rows)
    # rebuild the comparison since tracking started from the saved signals
    by_day = {}
    for r in rows:
        sig = {"time": dt.datetime.fromisoformat(r["date"] + "T" + r["time_ny"]), "entry": float(r["entry"]),
               "stop": float(r["stop"]), "target": float(r["target"])}
        by_day.setdefault(r["date"], []).append({"date": r["date"], "strategy": r["strategy"], "symbol": r["symbol"],
                                                 "sig": sig, "r": float(r["r"]), "outcome": r["outcome"],
                                                 "rank": (sig["entry"] - sig["stop"]) / sig["entry"]})
    prev = ap.read_json(os.path.join(RESEARCH, "tracked.json"), {}) or {}
    for d in set(prev.get("days", [])) | {today}:
        by_day.setdefault(d, [])
    results, equity, _ = compare(by_day, rs, cfg, fx)
    ap.write_json(os.path.join(RESEARCH, "tracked.json"), {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "from": min(by_day) if by_day else None, "to": max(by_day) if by_day else None,
        "sessions": len(by_day), "days": sorted(by_day), "usd_per_gbp": fx,
        "results": results, "equity": equity})
    ap.log("Tracked %d signal(s) today across %d strategies." % (len(cands), len(strats)))


def main(argv=None):
    p = argparse.ArgumentParser(description="Strategy tracking and backtests (simulation only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=60)
    sub.add_parser("day")
    a = p.parse_args(argv)
    ap.load_env()
    try:
        backtest(a.days) if a.cmd == "backtest" else day()
    except Exception as e:
        ap.log("RESEARCH ERROR: %s" % e)
        if ap.QUIET:
            print("research: error (details in the private journal log)")
            sys.exit(1)
        raise
    if ap.QUIET:
        print("research: ok")


if __name__ == "__main__":
    main()
