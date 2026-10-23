"""
Strategy plug-ins.

A strategy looks at one stock's 5-minute bars for one session and says where it
would buy. The engine (engine.py) does everything else the same way for every
strategy: simulating the outcome bar by bar, sizing to the max loss, applying
the daily limits, and scoring the results. So adding a strategy means adding
one small class here and listing it in REGISTRY.

Bars are dicts: {"t": datetime (New York, naive, bar START time),
                 "o": open, "h": high, "l": low, "c": close, "v": volume}
covering the regular session only (09:30-16:00).

A signal is a dict: {"time": datetime of the bar CLOSE the decision was made on,
                     "entry": price, "stop": price below entry,
                     "target": price above entry, "why": short text}
Long only for now (stop < entry < target).
"""
from .orb import OpeningRangeBreakout

REGISTRY = {
    # key            -> strategy object
    "orb": OpeningRangeBreakout(name="orb", edge_filter=False),
    "orb_filtered": OpeningRangeBreakout(name="orb_filtered", edge_filter=True),
}


def get(names):
    return [REGISTRY[n] for n in names if n in REGISTRY]
