# Stock Put Alert Monitor — Telegram Bot

Control your stock drop alerts entirely through Telegram.
No browser or dashboard needed. Runs 24/7 in the cloud.

## Bot Commands

| Command | What it does |
|---|---|
| /start | Show help |
| /watch AAPL TSLA NVDA | Add stocks to watchlist |
| /unwatch AAPL | Remove a stock |
| /list | Show watchlist with live prices |
| /threshold 5 | Set drop alert % (default 5%) |
| /interval 3 | Set check interval in minutes |
| /resume | Start monitoring |
| /pause | Pause monitoring |
| /check | Force immediate price check |
| /status | Show current settings |

## Deploy on Railway (free, recommended)

1. Go to https://railway.app → sign up free
2. New Project → Deploy from GitHub repo
   (push these files to a GitHub repo first)
3. Add Environment Variables:

| Variable | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | from @BotFather |
| ANTHROPIC_API_KEY | from console.anthropic.com |
| WATCHLIST | AAPL,NVDA,TSLA (optional default) |
| DROP_THRESHOLD_PCT | 5.0 (optional default) |
| POLL_INTERVAL_MIN | 3 (optional default) |

Note: No TELEGRAM_CHAT_ID needed — the bot auto-detects
who it's talking to and manages each user separately.

4. Deploy → open Telegram → message your bot /start

## Get your Bot Token

1. Open Telegram → search @BotFather
2. Send /newbot → follow prompts → copy the token
3. Search your new bot's username → tap Start → send /start
