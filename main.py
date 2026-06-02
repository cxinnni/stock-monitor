"""
Stock Drop Put Alert Monitor — Telegram Bot
Control everything via Telegram commands. Runs 24/7 in the cloud.

Commands:
  /start        - Welcome message
  /watch AAPL TSLA NVDA  - Add stocks to watchlist
  /unwatch AAPL - Remove a stock
  /list         - Show current watchlist + live prices
  /threshold 5  - Set drop alert threshold (%)
  /interval 3   - Set polling interval (minutes)
  /status       - Show monitor status
  /pause        - Pause monitoring
  /resume       - Resume monitoring
  /check        - Force an immediate price check now
"""

import os
import time
import threading
import logging
import yfinance as yf
import anthropic
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
# Optional defaults (overridable via Telegram commands)
DEFAULT_WATCHLIST  = os.environ.get("WATCHLIST", "").split(",")
DEFAULT_THRESHOLD  = float(os.environ.get("DROP_THRESHOLD_PCT", "5.0"))
DEFAULT_INTERVAL   = int(os.environ.get("POLL_INTERVAL_MIN", "3"))
# ─────────────────────────────────────────────────────────────────────────────

TG_API = f"https://api.telegram.org/bot{TOKEN}"

# ── STATE (per chat_id) ───────────────────────────────────────────────────────
chats: dict[str, dict] = {}

def get_chat(chat_id: str) -> dict:
    if chat_id not in chats:
        chats[chat_id] = {
            "watchlist":   [s for s in DEFAULT_WATCHLIST if s.strip()],
            "threshold":   DEFAULT_THRESHOLD,
            "interval":    DEFAULT_INTERVAL,
            "monitoring":  False,
            "sent_alerts": set(),
            "chat_id":     chat_id,
        }
    return chats[chat_id]

# ── TELEGRAM HELPERS ──────────────────────────────────────────────────────────
def send(chat_id: str, text: str, parse_mode="HTML"):
    try:
        requests.post(f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10)
    except Exception as e:
        log.error(f"send error: {e}")

def send_all(text: str):
    for chat_id in list(chats):
        if chats[chat_id].get("monitoring"):
            send(chat_id, text)

# ── STOCK + AI ────────────────────────────────────────────────────────────────
def get_price(symbol: str):
    try:
        info = yf.Ticker(symbol).fast_info
        return info.last_price, info.previous_close
    except:
        return None, None

def get_reason(symbol, price, prev_close, drop_pct) -> str:
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"Stock {symbol} dropped {abs(drop_pct):.1f}% today "
                f"(${prev_close:.2f} → ${price:.2f}). "
                f"Search latest news. Explain in 2-3 sentences why it fell. "
                f"Add one line on put-selling considerations (IV rank, earnings, support). "
                f"Today: {datetime.now().strftime('%B %d, %Y')}."
            }]
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return f"Could not fetch reason: {e}"

def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=25) <= now <= now.replace(hour=16, minute=5)

# ── MONITOR LOOP (one thread per chat) ───────────────────────────────────────
def monitor_loop(chat_id: str):
    c = get_chat(chat_id)
    log.info(f"Monitor started for {chat_id}")
    while c["monitoring"]:
        if not is_market_open():
            log.info(f"[{chat_id}] Market closed, sleeping 10m")
            time.sleep(600)
            continue

        log.info(f"[{chat_id}] Checking {c['watchlist']}")
        for sym in list(c["watchlist"]):
            if not c["monitoring"]:
                break
            price, prev_close = get_price(sym)
            if not price or not prev_close:
                continue
            pct = ((price - prev_close) / prev_close) * 100
            log.info(f"  {sym} ${price:.2f} {pct:+.2f}%")

            if pct <= -c["threshold"]:
                key = f"{sym}-{round(price, 1)}"
                if key not in c["sent_alerts"]:
                    c["sent_alerts"].add(key)
                    send(chat_id,
                        f"📉 <b>{sym} dropped {abs(pct):.2f}%</b>\n\n"
                        f"💰 ${prev_close:.2f} → ${price:.2f}\n"
                        f"🕐 {datetime.now(ZoneInfo('Asia/Singapore')).strftime('%H:%M SGT')}\n\n"
                        f"⏳ Fetching analysis...")
                    reason = get_reason(sym, price, prev_close, pct)
                    send(chat_id,
                        f"🔍 <b>{sym} Analysis</b>\n\n"
                        f"<b>Why it fell:</b>\n{reason}\n\n"
                        f"<b>Put note:</b> Sell 5–10% OTM put, 2–4 weeks expiry. "
                        f"Verify IV rank is elevated.\n\n"
                        f"📌 Ref price: ${price:.2f}")
            time.sleep(1)

        for _ in range(c["interval"] * 60):
            if not c["monitoring"]:
                break
            time.sleep(1)

    log.info(f"Monitor stopped for {chat_id}")

def start_monitor(chat_id: str):
    c = get_chat(chat_id)
    if c["monitoring"]:
        return False
    c["monitoring"] = True
    c["sent_alerts"] = set()
    t = threading.Thread(target=monitor_loop, args=(chat_id,), daemon=True)
    t.start()
    return True

def stop_monitor(chat_id: str):
    c = get_chat(chat_id)
    if not c["monitoring"]:
        return False
    c["monitoring"] = False
    return True

# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────
def handle(chat_id: str, text: str):
    c = get_chat(chat_id)
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]   # strip @botname if present
    args = parts[1:]

    if cmd == "/start":
        send(chat_id,
            "👋 <b>Put Alert Monitor Bot</b>\n\n"
            "I monitor US stocks and alert you when they drop so you can sell puts.\n\n"
            "<b>Commands:</b>\n"
            "/watch AAPL TSLA NVDA — add to watchlist\n"
            "/unwatch AAPL — remove from watchlist\n"
            "/list — show watchlist + live prices\n"
            "/threshold 5 — set drop % alert level\n"
            "/interval 3 — set check interval (minutes)\n"
            "/resume — start monitoring\n"
            "/pause — stop monitoring\n"
            "/check — force immediate price check\n"
            "/status — show current settings\n\n"
            "Start by adding tickers: /watch AAPL NVDA TSLA")

    elif cmd == "/watch":
        if not args:
            send(chat_id, "Usage: /watch AAPL TSLA NVDA")
            return
        added = []
        for sym in args:
            sym = sym.upper().strip()
            if sym and sym not in c["watchlist"]:
                c["watchlist"].append(sym)
                added.append(sym)
        if added:
            send(chat_id, f"✅ Added to watchlist: {', '.join(added)}\n\nWatchlist: {', '.join(c['watchlist'])}")
        else:
            send(chat_id, "Those tickers are already on your watchlist.")

    elif cmd == "/unwatch":
        if not args:
            send(chat_id, "Usage: /unwatch AAPL")
            return
        removed = []
        for sym in args:
            sym = sym.upper().strip()
            if sym in c["watchlist"]:
                c["watchlist"].remove(sym)
                removed.append(sym)
        if removed:
            send(chat_id, f"🗑 Removed: {', '.join(removed)}\n\nWatchlist: {', '.join(c['watchlist']) or 'empty'}")
        else:
            send(chat_id, "Those tickers were not in your watchlist.")

    elif cmd == "/list":
        if not c["watchlist"]:
            send(chat_id, "Your watchlist is empty. Add tickers: /watch AAPL TSLA")
            return
        send(chat_id, "⏳ Fetching live prices...")
        lines = []
        for sym in c["watchlist"]:
            price, prev_close = get_price(sym)
            if price and prev_close:
                pct = ((price - prev_close) / prev_close) * 100
                arrow = "📉" if pct < -c["threshold"] else ("🟢" if pct >= 0 else "🔴")
                lines.append(f"{arrow} <b>{sym}</b>  ${price:.2f}  {pct:+.2f}%")
            else:
                lines.append(f"❓ <b>{sym}</b>  no data")
        status = "🟢 Monitoring" if c["monitoring"] else "⏸ Paused"
        send(chat_id, f"<b>Watchlist</b> ({status})\n\n" + "\n".join(lines) +
             f"\n\nThreshold: -{c['threshold']}%  |  Interval: {c['interval']}m")

    elif cmd == "/threshold":
        if not args:
            send(chat_id, f"Current threshold: -{c['threshold']}%\nUsage: /threshold 5")
            return
        try:
            val = float(args[0])
            if not 0.5 <= val <= 30:
                raise ValueError
            c["threshold"] = val
            c["sent_alerts"] = set()   # reset so re-alerts can fire at new level
            send(chat_id, f"✅ Alert threshold set to -{val}%\n(Existing alerts reset)")
        except:
            send(chat_id, "Invalid value. Use a number between 0.5 and 30, e.g. /threshold 5")

    elif cmd == "/interval":
        if not args:
            send(chat_id, f"Current interval: {c['interval']} minutes\nUsage: /interval 3")
            return
        try:
            val = int(args[0])
            if not 1 <= val <= 60:
                raise ValueError
            c["interval"] = val
            send(chat_id, f"✅ Poll interval set to {val} minute(s)")
        except:
            send(chat_id, "Invalid value. Use a whole number between 1 and 60, e.g. /interval 3")

    elif cmd == "/resume":
        if not c["watchlist"]:
            send(chat_id, "Add tickers first: /watch AAPL TSLA")
            return
        if start_monitor(chat_id):
            send(chat_id,
                f"▶️ <b>Monitoring started</b>\n\n"
                f"📋 Watching: {', '.join(c['watchlist'])}\n"
                f"📉 Threshold: -{c['threshold']}%\n"
                f"⏱ Interval: {c['interval']} min\n\n"
                f"I'll message you when a stock drops {c['threshold']}%+.")
        else:
            send(chat_id, "Already monitoring. Use /pause to stop.")

    elif cmd == "/pause":
        if stop_monitor(chat_id):
            send(chat_id, "⏸ Monitoring paused. Send /resume to restart.")
        else:
            send(chat_id, "Not currently monitoring. Send /resume to start.")

    elif cmd == "/check":
        if not c["watchlist"]:
            send(chat_id, "Add tickers first: /watch AAPL TSLA")
            return
        send(chat_id, "⏳ Checking prices now...")
        lines = []
        for sym in c["watchlist"]:
            price, prev_close = get_price(sym)
            if price and prev_close:
                pct = ((price - prev_close) / prev_close) * 100
                flag = " ⚠️ ALERT" if pct <= -c["threshold"] else ""
                lines.append(f"{'📉' if pct<0 else '🟢'} <b>{sym}</b>  ${price:.2f}  {pct:+.2f}%{flag}")
            else:
                lines.append(f"❓ <b>{sym}</b>  no data")
        send(chat_id, "<b>Live Check</b>\n\n" + "\n".join(lines))

    elif cmd == "/status":
        status = "🟢 Active" if c["monitoring"] else "⏸ Paused"
        market = "🟢 Open" if is_market_open() else "🔴 Closed"
        send(chat_id,
            f"<b>Monitor Status</b>\n\n"
            f"Status:    {status}\n"
            f"Market:    {market}\n"
            f"Watchlist: {', '.join(c['watchlist']) or 'empty'}\n"
            f"Threshold: -{c['threshold']}%\n"
            f"Interval:  {c['interval']} min\n"
            f"Alerts sent today: {len(c['sent_alerts'])}")

    else:
        send(chat_id, "Unknown command. Send /start to see all commands.")

# ── POLLING LOOP ──────────────────────────────────────────────────────────────
def poll():
    offset = None
    log.info("Bot polling started")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=40)
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and chat_id and text.startswith("/"):
                    log.info(f"CMD {chat_id}: {text}")
                    threading.Thread(
                        target=handle, args=(chat_id, text), daemon=True
                    ).start()
        except Exception as e:
            log.error(f"Poll error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    log.info("Starting Put Alert Monitor Bot...")
    poll()
