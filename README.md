# trading-autopilot

Paper-only opening-range-breakout autopilot that runs on GitHub Actions.

Each weekday it scans ~50 large US stocks for opening-range breakouts, filters
by news and by the Time Machine's historical edge, sizes the trade to a fixed
max loss, runs a risk check, and places a bracket order on an **Alpaca paper**
account. Everything is closed at 15:55 New York time.

- The broker URL is hard-coded to Alpaca's paper endpoint. There is no live mode.
- Trades, config and logs are written to a separate **private** repo; the public
  Actions logs print only `ok` / `error`.
- Keys live in GitHub Secrets: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`,
  `JOURNAL_TOKEN` (fine-grained token with Contents read/write on the journal repo only).

Workflows: `session.yml` (08:45 NY, waits for the open), `flatten.yml` (15:15 NY,
waits for 15:55), `status.yml` (manual connection check).

`skills/` contains the scripts from
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT, see `skills/LICENSE`).

Not financial advice. Paper trading only.

## Strategies

Strategies live in `autopilot/strategies/` (one class each, listed in `REGISTRY`).
Every strategy listed under `research.track` in the private `settings.json` is
tracked every day after the close (`research.py day`), and can be replayed on
past data with the **Backtest strategies** workflow. `research.combos` defines
mixes ("both") that are scored with the same per-trade and daily limits.

- `orb` – opening-range breakout (first 15 minutes, 2R target, stop at the midpoint)
- `orb_filtered` – the same, only when it averaged a positive R on that stock over the last 60 sessions

Live paper trading currently runs the filtered opening-range breakout.
