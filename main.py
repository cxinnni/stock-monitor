"""
Stock Drop Put Alert Monitor — Telegram Bot
Includes: 52-week high/low, 6-month high/low, price position, news analysis.

Commands:
  /start            - Welcome + help
  /watch AAPL TSLA  - Add stocks
  /unwatch AAPL     - Remove stock
  /list             - Watchlist + live prices + ranges
  /info AAPL        - Full snapshot: price, ranges, news
  /threshold 5      - Set drop alert %
  /interval 3       - Set poll interval (minutes)
  /resume           - Start monitoring
  /pause            - Pause monitoring
  /check            - Force immediate price check
  /status           - Show settings
"""

import os
import time
import threading
import logging
import yfinance as yf
import anthropic
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── ENV ───────────────────────────────────────────────────────────────────────
TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
DEFAULT_WATCHLIST = os.environ.get("WATCHLIST", "").split(",")
DEFAULT_THRESHOLD = float(os.environ.get("DROP_THRESHOLD_PCT", "5.0"))
DEFAULT_INTERVAL  = int(os.environ.get("POLL_INTERVAL_MIN", "3"))
# ─────────────────────────────────────────────────────────────────────────────

TG_API = f"https://api.telegram.org/bot{TOKEN}"
chats: dict[str, dict] = {}


def get_chat(chat_id: str) -> dict:
    if chat_id not in chats:
        chats[chat_id] = {
            "watchlist":  [s for s in DEFAULT_WATCHLIST if s.strip()],
            "threshold":  DEFAULT_THRESHOLD,
            "interval":   DEFAULT_INTERVAL,
            "monitoring": False,
            "sent_alerts": set(),
            "chat_id":    chat_id,
        }
    return chats[chat_id]


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send(chat_id: str, text: str):
    try:
        requests.post(f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        log.error(f"send error: {e}")


# ── STOCK DATA ────────────────────────────────────────────────────────────────
def get_stock_data(symbol: str) -> dict | None:
    """Fetch price, 52-week range, 6-month range, and volume."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info

        price      = info.last_price
        prev_close = info.previous_close
        if not price or not prev_close:
            return None

        # 52-week high/low
        week52_high = getattr(info, "year_high", None)
        week52_low  = getattr(info, "year_low",  None)

        # 6-month high/low from historical data
        end   = datetime.now()
        start = end - timedelta(days=182)
        hist  = ticker.history(start=start.strftime("%Y-%m-%d"),
                               end=end.strftime("%Y-%m-%d"))
        six_month_high = float(hist["High"].max())  if not hist.empty else None
        six_month_low  = float(hist["Low"].min())   if not hist.empty else None

        # Volume
        volume     = getattr(info, "last_volume",   None)
        avg_volume = getattr(info, "three_month_average_volume", None)

        change_pct = ((price - prev_close) / prev_close) * 100
        change_amt = price - prev_close

        # Price position within 52w range (0% = at low, 100% = at high)
        pos_52w = None
        if week52_high and week52_low and week52_high != week52_low:
            pos_52w = ((price - week52_low) / (week52_high - week52_low)) * 100

        pos_6m = None
        if six_month_high and six_month_low and six_month_high != six_month_low:
            pos_6m = ((price - six_month_low) / (six_month_high - six_month_low)) * 100

        return {
            "symbol":         symbol,
            "price":          price,
            "prev_close":     prev_close,
            "change_pct":     change_pct,
            "change_amt":     change_amt,
            "week52_high":    week52_high,
            "week52_low":     week52_low,
            "six_month_high": six_month_high,
            "six_month_low":  six_month_low,
            "pos_52w":        pos_52w,
            "pos_6m":         pos_6m,
            "volume":         volume,
            "avg_volume":     avg_volume,
        }
    except Exception as e:
        log.error(f"get_stock_data {symbol}: {e}")
        return None


def fmt_price(v) -> str:
    return f"${v:,.2f}" if v else "N/A"

def fmt_pct(v) -> str:
    return f"{v:.1f}%" if v is not None else "N/A"

def fmt_vol(v) -> str:
    if not v:
        return "N/A"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return str(int(v))

def position_bar(pct: float, width=10) -> str:
    """Visual bar showing where price sits in a range."""
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:.0f}%"

def range_summary(d: dict) -> str:
    lines = []

    if d["week52_high"] and d["week52_low"]:
        bar = position_bar(d["pos_52w"]) if d["pos_52w"] is not None else ""
        pct_from_high = ((d["price"] - d["week52_high"]) / d["week52_high"]) * 100
        pct_from_low  = ((d["price"] - d["week52_low"])  / d["week52_low"])  * 100
        lines.append(
            f"📊 <b>52-Week Range</b>\n"
            f"   Low:  {fmt_price(d['week52_low'])}  ({pct_from_low:+.1f}% from here)\n"
            f"   High: {fmt_price(d['week52_high'])}  ({pct_from_high:+.1f}% from here)\n"
            f"   Position: {bar}"
        )

    if d["six_month_high"] and d["six_month_low"]:
        bar = position_bar(d["pos_6m"]) if d["pos_6m"] is not None else ""
        pct_from_high = ((d["price"] - d["six_month_high"]) / d["six_month_high"]) * 100
        pct_from_low  = ((d["price"] - d["six_month_low"])  / d["six_month_low"])  * 100
        lines.append(
            f"📅 <b>6-Month Range</b>\n"
            f"   Low:  {fmt_price(d['six_month_low'])}  ({pct_from_low:+.1f}% from here)\n"
            f"   High: {fmt_price(d['six_month_high'])}  ({pct_from_high:+.1f}% from here)\n"
            f"   Position: {bar}"
        )

    if d["volume"] and d["avg_volume"]:
        vol_ratio = (d["volume"] / d["avg_volume"]) * 100
        vol_flag  = " 🔥 High volume!" if vol_ratio > 150 else ""
        lines.append(
            f"📦 <b>Volume</b>\n"
            f"   Today: {fmt_vol(d['volume'])}  |  3M avg: {fmt_vol(d['avg_volume'])}\n"
            f"   vs Average: {vol_ratio:.0f}%{vol_flag}"
        )

    return "\n\n".join(lines)


# ── AI ANALYSIS ───────────────────────────────────────────────────────────────
def get_analysis(d: dict, mode="alert") -> str:
    """
    mode='alert'  — why it dropped + put selling note
    mode='info'   — general snapshot including recent news
    """
    sym        = d["symbol"]
    price      = d["price"]
    prev_close = d["prev_close"]
    drop_pct   = d["change_pct"]

    context = (
        f"Stock: {sym}\n"
        f"Current price: ${price:.2f}\n"
        f"Previous close: ${prev_close:.2f}\n"
        f"Change today: {drop_pct:+.2f}%\n"
        f"52-week high: {fmt_price(d['week52_high'])}\n"
        f"52-week low:  {fmt_price(d['week52_low'])}\n"
        f"6-month high: {fmt_price(d['six_month_high'])}\n"
        f"6-month low:  {fmt_price(d['six_month_low'])}\n"
        f"Position in 52w range: {fmt_pct(d['pos_52w'])}\n"
        f"Volume vs avg: {(d['volume']/d['avg_volume']*100):.0f}% " if d['volume'] and d['avg_volume'] else ""
        f"Today: {datetime.now().strftime('%B %d, %Y')}\n"
    )

    if mode == "alert":
        prompt = (
            f"{context}\n"
            f"This stock just dropped {abs(drop_pct):.1f}% triggering a put-selling alert.\n\n"
            f"1. Search for the latest news driving this drop. Explain in 2-3 sentences.\n"
            f"2. Given the price is at {fmt_pct(d['pos_52w'])} of its 52-week range, "
            f"comment on whether this is near a support level.\n"
            f"3. Give one specific put-selling recommendation: strike price relative to "
            f"current price, expiry timeframe, and whether IV is likely elevated."
        )
    else:
        prompt = (
            f"{context}\n"
            f"Give a brief snapshot of this stock:\n"
            f"1. Search for the 2-3 most recent news items affecting this stock.\n"
            f"2. Comment on where the price sits relative to its 52-week and 6-month ranges.\n"
            f"3. Any put-selling opportunities worth watching?"
        )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return f"Could not fetch analysis: {e}"


# ── MARKET HOURS ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=25) <= now <= now.replace(hour=16, minute=5)


# ── MONITOR LOOP ──────────────────────────────────────────────────────────────
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
            d = get_stock_data(sym)
            if not d:
                continue

            log.info(f"  {sym} ${d['price']:.2f} {d['change_pct']:+.2f}%")

            if d["change_pct"] <= -c["threshold"]:
                key = f"{sym}-{round(d['price'], 1)}"
                if key not in c["sent_alerts"]:
                    c["sent_alerts"].add(key)

                    # Immediate alert
                    sgt = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%H:%M SGT")
                    send(chat_id,
                        f"🚨 <b>{sym} ALERT — dropped {abs(d['change_pct']):.2f}%</b>\n\n"
                        f"💰 {fmt_price(d['prev_close'])} → {fmt_price(d['price'])}  "
                        f"({d['change_amt']:+.2f})\n"
                        f"🕐 {sgt}\n\n"
                        + range_summary(d) +
                        "\n\n⏳ Fetching news + analysis...")

                    # AI analysis
                    analysis = get_analysis(d, mode="alert")
                    send(chat_id,
                        f"🔍 <b>{sym} — Drop Analysis</b>\n\n"
                        f"{analysis}\n\n"
                        f"📌 Ref price: {fmt_price(d['price'])}")
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
    threading.Thread(target=monitor_loop, args=(chat_id,), daemon=True).start()
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
    cmd  = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd == "/start":
        send(chat_id,
            "👋 <b>Put Alert Monitor Bot</b>\n\n"
            "Monitors stocks 24/7. Alerts you on drops with price ranges, "
            "volume, news, and put-selling recommendations.\n\n"
            "<b>Commands:</b>\n"
            "/watch AAPL TSLA NVDA — add tickers\n"
            "/unwatch AAPL — remove ticker\n"
            "/list — watchlist with live prices\n"
            "/info AAPL — full snapshot + news\n"
            "/threshold 5 — alert drop % (default 5%)\n"
            "/interval 3 — check interval in minutes\n"
            "/resume — start monitoring\n"
            "/pause — stop monitoring\n"
            "/check — immediate price check\n"
            "/status — show settings\n\n"
            "Start: /watch AAPL NVDA TSLA then /resume")

    elif cmd == "/watch":
        if not args:
            send(chat_id, "Usage: /watch AAPL TSLA NVDA"); return
        added = []
        for sym in args:
            sym = sym.upper().strip()
            if sym and sym not in c["watchlist"]:
                c["watchlist"].append(sym); added.append(sym)
        if added:
            send(chat_id, f"✅ Added: {', '.join(added)}\n\nWatchlist: {', '.join(c['watchlist'])}")
        else:
            send(chat_id, "Already on watchlist.")

    elif cmd == "/unwatch":
        if not args:
            send(chat_id, "Usage: /unwatch AAPL"); return
        removed = []
        for sym in args:
            sym = sym.upper().strip()
            if sym in c["watchlist"]:
                c["watchlist"].remove(sym); removed.append(sym)
        if removed:
            send(chat_id, f"🗑 Removed: {', '.join(removed)}\n\nWatchlist: {', '.join(c['watchlist']) or 'empty'}")
        else:
            send(chat_id, "Not found in watchlist.")

    elif cmd == "/list":
        if not c["watchlist"]:
            send(chat_id, "Watchlist empty. Add tickers: /watch AAPL TSLA"); return
        send(chat_id, "⏳ Fetching live data...")
        lines = []
        for sym in c["watchlist"]:
            d = get_stock_data(sym)
            if d:
                arrow = "📉" if d["change_pct"] <= -c["threshold"] else ("🟢" if d["change_pct"] >= 0 else "🔴")
                pos   = f" | 52w pos: {fmt_pct(d['pos_52w'])}" if d["pos_52w"] is not None else ""
                lines.append(
                    f"{arrow} <b>{sym}</b>  {fmt_price(d['price'])}  {d['change_pct']:+.2f}%{pos}"
                )
            else:
                lines.append(f"❓ <b>{sym}</b>  no data")
        status = "🟢 Monitoring" if c["monitoring"] else "⏸ Paused"
        send(chat_id,
            f"<b>Watchlist</b> ({status})\n\n" + "\n".join(lines) +
            f"\n\nThreshold: -{c['threshold']}%  |  Interval: {c['interval']}m\n"
            f"Use /info TICKER for full ranges + news")

    elif cmd == "/info":
        if not args:
            send(chat_id, "Usage: /info AAPL"); return
        sym = args[0].upper()
        send(chat_id, f"⏳ Fetching full data for {sym}...")
        d = get_stock_data(sym)
        if not d:
            send(chat_id, f"Could not fetch data for {sym}."); return
        arrow = "🟢" if d["change_pct"] >= 0 else "🔴"
        send(chat_id,
            f"{arrow} <b>{sym}</b>  {fmt_price(d['price'])}\n"
            f"Change: {d['change_pct']:+.2f}% ({d['change_amt']:+.2f})\n"
            f"Prev close: {fmt_price(d['prev_close'])}\n\n"
            + range_summary(d) +
            "\n\n⏳ Fetching news...")
        analysis = get_analysis(d, mode="info")
        send(chat_id, f"📰 <b>{sym} News & Analysis</b>\n\n{analysis}")

    elif cmd == "/threshold":
        if not args:
            send(chat_id, f"Current: -{c['threshold']}%\nUsage: /threshold 5"); return
        try:
            val = float(args[0])
            if not 0.5 <= val <= 30: raise ValueError
            c["threshold"] = val
            c["sent_alerts"] = set()
            send(chat_id, f"✅ Threshold set to -{val}%\n(Alert history reset)")
        except:
            send(chat_id, "Use a number between 0.5–30, e.g. /threshold 5")

    elif cmd == "/interval":
        if not args:
            send(chat_id, f"Current: {c['interval']}m\nUsage: /interval 3"); return
        try:
            val = int(args[0])
            if not 1 <= val <= 60: raise ValueError
            c["interval"] = val
            send(chat_id, f"✅ Interval set to {val} minute(s)")
        except:
            send(chat_id, "Use a whole number between 1–60, e.g. /interval 3")

    elif cmd == "/resume":
        if not c["watchlist"]:
            send(chat_id, "Add tickers first: /watch AAPL TSLA"); return
        if start_monitor(chat_id):
            send(chat_id,
                f"▶️ <b>Monitoring started</b>\n\n"
                f"📋 Watching: {', '.join(c['watchlist'])}\n"
                f"📉 Threshold: -{c['threshold']}%\n"
                f"⏱ Interval: {c['interval']} min\n"
                f"📊 Includes: 52w range, 6m range, volume, news\n\n"
                f"I'll alert you when any stock drops {c['threshold']}%+")
        else:
            send(chat_id, "Already monitoring. Use /pause to stop.")

    elif cmd == "/pause":
        if stop_monitor(chat_id):
            send(chat_id, "⏸ Monitoring paused. Send /resume to restart.")
        else:
            send(chat_id, "Not currently monitoring.")

    elif cmd == "/check":
        if not c["watchlist"]:
            send(chat_id, "Add tickers first: /watch AAPL TSLA"); return
        send(chat_id, "⏳ Checking prices now...")
        lines = []
        for sym in c["watchlist"]:
            d = get_stock_data(sym)
            if d:
                flag  = " ⚠️ ALERT" if d["change_pct"] <= -c["threshold"] else ""
                pos52 = f" | 52w: {fmt_pct(d['pos_52w'])}" if d["pos_52w"] is not None else ""
                pos6m = f" | 6m: {fmt_pct(d['pos_6m'])}" if d["pos_6m"] is not None else ""
                lines.append(
                    f"{'📉' if d['change_pct']<0 else '🟢'} <b>{sym}</b>  "
                    f"{fmt_price(d['price'])}  {d['change_pct']:+.2f}%{flag}{pos52}{pos6m}"
                )
            else:
                lines.append(f"❓ <b>{sym}</b>  no data")
        send(chat_id, "<b>Live Check</b>\n\n" + "\n".join(lines))

    elif cmd == "/status":
        status = "🟢 Active" if c["monitoring"] else "⏸ Paused"
        market = "🟢 Open" if is_market_open() else "🔴 Closed"
        send(chat_id,
            f"<b>Monitor Status</b>\n\n"
            f"Status:     {status}\n"
            f"Market:     {market}\n"
            f"Watchlist:  {', '.join(c['watchlist']) or 'empty'}\n"
            f"Threshold:  -{c['threshold']}%\n"
            f"Interval:   {c['interval']} min\n"
            f"Alerts today: {len(c['sent_alerts'])}")

    else:
        send(chat_id, "Unknown command. Send /start to see all commands.")


# ── POLLING ───────────────────────────────────────────────────────────────────
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
                time.sleep(5); continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and chat_id and text.startswith("/"):
                    log.info(f"CMD {chat_id}: {text}")
                    threading.Thread(target=handle, args=(chat_id, text), daemon=True).start()
        except Exception as e:
            log.error(f"Poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    log.info("Starting Put Alert Monitor Bot...")
    poll()
