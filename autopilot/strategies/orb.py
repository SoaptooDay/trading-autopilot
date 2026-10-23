"""Strategy 1: opening-range breakout (the first-candle rule)."""
import datetime as dt
import math


def _hm(t):
    return t.hour * 60 + t.minute


class OpeningRangeBreakout:
    """
    Opening range = high/low of the first `or_minutes` (default 15) of the session.
    Buy on the first 5-minute bar that CLOSES above the range high, between
    `first_entry` and `last_entry` (New York time). Stop at the range midpoint
    (rounded down to the cent), target at `target_r` x the risk.

    With edge_filter=True it only trades a stock if the same rule, replayed on
    that stock's previous sessions (up to 60), averaged a positive result in R
    (when it fired at least `min_history` times). This is the history half of
    the live Time Machine check.
    """

    def __init__(self, name="orb", edge_filter=False, or_minutes=15, target_r=2.0,
                 first_entry="09:45", last_entry="12:00", min_history=10, lookback=60):
        self.name = name
        self.label = "Opening-range breakout" + (" (edge filter)" if edge_filter else "")
        self.edge_filter = edge_filter
        self.or_minutes = or_minutes
        self.target_r = target_r
        self.first_entry = first_entry
        self.last_entry = last_entry
        self.min_history = min_history
        self.lookback = lookback

    # -- the rule on one session ---------------------------------------
    def raw_signal(self, bars):
        if not bars:
            return None
        start = _hm(bars[0]["t"])
        if start != 9 * 60 + 30:
            return None                        # session data starts late: skip the day
        or_end = start + self.or_minutes
        rng = [b for b in bars if _hm(b["t"]) < or_end]
        if len(rng) < self.or_minutes // 5:
            return None
        hi = max(b["h"] for b in rng)
        lo = min(b["l"] for b in rng)
        fe = sum(int(x) * m for x, m in zip(self.first_entry.split(":"), (60, 1)))
        le = sum(int(x) * m for x, m in zip(self.last_entry.split(":"), (60, 1)))
        for b in bars:
            close_at = _hm(b["t"]) + 5
            if _hm(b["t"]) < or_end or close_at <= fe:
                continue
            if close_at > le:
                break
            if b["c"] > hi:
                entry = b["c"]
                stop = math.floor(((hi + lo) / 2) * 100) / 100
                if stop >= entry:
                    return None
                risk = round(entry - stop, 2)
                target = round(entry + self.target_r * risk + 1e-9, 2)
                return {"time": b["t"] + dt.timedelta(minutes=5), "entry": round(entry, 2),
                        "stop": stop, "target": target,
                        "why": "closed %.2f above range high %.2f" % (entry, hi)}
        return None

    def signals(self, bars, prior_sessions, simulate):
        """bars: today's session; prior_sessions: list of earlier sessions (oldest first);
        simulate(signal, bars) -> R. Returns a list with 0 or 1 signal."""
        sig = self.raw_signal(bars)
        if not sig:
            return []
        if self.edge_filter:
            rs = []
            for past in prior_sessions[-self.lookback:]:
                ps = self.raw_signal(past)
                if ps:
                    rs.append(simulate(ps, past))
            if len(rs) >= self.min_history and sum(rs) / len(rs) <= 0:
                return []
            if rs:
                sig["why"] += "; history avg %+.2fR over %d" % (sum(rs) / len(rs), len(rs))
        return [sig]
