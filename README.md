# IFVG paper bot (NQ levels, QQQ execution, Alpaca paper)

**Rules (New York time):** mark NQ Asia (20:00-23:59) and London (02:00-05:00) highs/lows; a level only
counts if untouched before 09:30. Between 09:30 and 11:00, wait for the first sweep of a level, then enter on
the first 1-min close back through the latest opposing fair value gap. Stop beyond the sweep extreme, target 2R,
one trade per day, flat 5 minutes before the close. Risk per trade: 0.5% of equity (`RISK_PCT`).

**How it runs:** GitHub Actions, no computer needed.
- `morning` starts ~09:20 NY, builds levels from NQ (Yahoo), watches QQQ 1-min bars (Alpaca IEX), places one
  bracket order (entry + stop + target) in your Alpaca paper account.
- `eod` flattens before the close and appends the day to `trades.csv`.
- Every step pings your phone through ntfy.

## Setup on the Mac

1. **Alpaca:** sign up at alpaca.markets, switch to the Paper account, generate API keys.
2. **ntfy:** install the ntfy app on your phone, subscribe to a long random topic name
   (e.g. `ifvg-yasen-8f3k2q`). Anyone who knows the name can read it, so keep it random.
3. **Test locally** (in Terminal, inside this folder):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   export APCA_API_KEY_ID=your_key APCA_API_SECRET_KEY=your_secret NTFY_TOPIC=your_topic
   python bot.py --check
   ```
   You should get a phone notification with yesterday's levels and what the strategy would have done.
4. **Push to GitHub** (needs Homebrew; `brew install gh` if you don't have the GitHub CLI):
   ```bash
   gh auth login
   git init && git add . && git commit -m "IFVG bot"
   gh repo create ifvg-bot --public --source=. --push
   gh secret set APCA_API_KEY_ID      # paste when prompted
   gh secret set APCA_API_SECRET_KEY
   gh secret set NTFY_TOPIC
   gh workflow run morning.yml        # check mode in the cloud
   ```
   Public repo = unlimited free Actions minutes. Keys live in Secrets and are never visible.

## 12-hour reports with Claude Code
Create a scheduled task at claude.ai/code/scheduled on this repo, every 12 hours, with this prompt:

> Read trades.csv and the latest runs of the morning and eod GitHub Actions workflows. Report: trades since
> the last report, total trades, win rate, average R, cumulative P&L, max drawdown in R, and current equity.
> Flag anything needing my attention: failed or skipped runs, missing days, repeated "missed signal" or
> "couldn't build levels" messages. Keep it short.

## Known limits
- Yahoo's NQ data is free but unofficial and delayed; it's only used for the overnight levels.
- IEX bars are a slice of total volume, so QQQ wicks can differ slightly from the consolidated tape.
- GitHub scheduled runs can start several minutes late; the bot skips a signal it noticed more than 3 minutes late.
