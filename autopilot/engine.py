"""
Shared engine for tracking and backtesting strategies: outcome simulation,
position sizing, daily limits and scoring. The same rules apply to every
strategy, so results are comparable.
"""
import math

FLAT_BY_MIN = 15 * 60 + 55        # close everything at 15:55 New York


def _hm(t):
    return t.hour * 60 + t.minute


def simulate(sig, bars, flat_by=FLAT_BY_MIN):
    """Walk the bars after the signal: stop first if a bar touches both (conservative).
    Returns the result in R (1R = entry - stop)."""
    risk = sig["entry"] - sig["stop"]
    if risk <= 0:
        return 0.0
    last = sig["entry"]
    for b in bars:
        if b["t"] < sig["time"]:
            continue
        if _hm(b["t"]) + 5 > flat_by:
            break
        if b["l"] <= sig["stop"]:
            return -1.0
        if b["h"] >= sig["target"]:
            return round((sig["target"] - sig["entry"]) / risk, 3)
        last = b["c"]
    return round((last - sig["entry"]) / risk, 3)


def outcome_label(r, target_r):
    if r <= -0.999:
        return "stop"
    if r >= target_r - 0.001:
        return "target"
    return "close"


def size(sig, cfg):
    """Whole shares inside max loss, max order and cash after the buffer."""
    risk = sig["entry"] - sig["stop"]
    max_loss = float(cfg.get("max_loss_per_trade_usd", 27))
    budget = float(cfg.get("budget", 2680))
    cash = budget * (1 - float(cfg.get("cash_buffer_pct", 10)) / 100)
    cap = min(float(cfg.get("max_order_usd", budget)), budget * float(cfg.get("max_position_pct", 75)) / 100, cash)
    qty = min(math.floor(max_loss / risk), math.floor(cap / sig["entry"]))
    return max(qty, 0)


def pick_day(candidates, cfg, max_trades):
    """candidates: [{"symbol","strategy","sig","r",...}] for one day.
    Take them in time order (ties: smallest move over the level first),
    one per symbol, up to max_trades and inside the daily loss limit."""
    daily_cap = float(cfg.get("max_daily_loss_usd", 54))
    taken, used, symbols = [], 0.0, set()
    for c in sorted(candidates, key=lambda c: (c["sig"]["time"], c.get("rank", 0))):
        if len(taken) >= max_trades or c["symbol"] in symbols:
            continue
        qty = size(c["sig"], cfg)
        if qty < 1:
            continue
        risk_usd = qty * (c["sig"]["entry"] - c["sig"]["stop"])
        if used + risk_usd > daily_cap + 0.01:
            continue
        used += risk_usd
        symbols.add(c["symbol"])
        taken.append(dict(c, qty=qty, risk_usd=round(risk_usd, 2), pnl_usd=round(c["r"] * risk_usd, 2)))
    return taken


def score(trades, fx):
    """Summary of a list of taken trades (any strategy or mix)."""
    n = len(trades)
    pnl = [t["pnl_usd"] / fx for t in trades]
    rs = [t["r"] for t in trades]
    eq, peak, dd, streak, worst_streak = 0.0, 0.0, 0.0, 0, 0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if p < 0 else 0
        worst_streak = max(worst_streak, streak)
    wins = sum(1 for p in pnl if p > 0)
    return {"trades": n, "wins": wins, "win_rate": round(wins / n, 4) if n else None,
            "avg_r": round(sum(rs) / n, 3) if n else None,
            "total_gbp": round(sum(pnl), 2), "max_drawdown_gbp": round(dd, 2),
            "worst_losing_streak": worst_streak}
