import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8924294980:AAH1dBKT-w5wY1RpUcd-QHZ-_Ad-UPPynQ8")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET = os.environ.get("BINANCE_API_SECRET", "")
CHAT_ID_FILE = "chat_id.json"

RISK_PER_TRADE = 0.05

# Smart Exit thresholds — no fixed % targets
BUY_EXIT_RSI  = 75   # exit BUY when RSI climbs this high (overbought reversal)
SELL_EXIT_RSI = 25   # exit SELL when RSI drops this low (oversold reversal)

SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
    'ADA/USDT', 'DOGE/USDT', 'DOT/USDT', 'POL/USDT', 'LINK/USDT',
    'AVAX/USDT', 'UNI/USDT', 'ATOM/USDT', 'LTC/USDT', 'FIL/USDT',
    'NEAR/USDT', 'APT/USDT', 'OP/USDT', 'ARB/USDT', 'SUI/USDT',
]

# One entry per symbol — prevents duplicate trades on the same pair
open_trades = {}   # { 'BTC/USDT': { symbol, side, entry, size, sl, tp, opened_at }, ... }
chat_id = None
_lock = threading.Lock()

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
})


def load_chat_id():
    global chat_id
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE) as f:
            chat_id = json.load(f).get('chat_id')


def save_chat_id(cid):
    global chat_id
    chat_id = cid
    with open(CHAT_ID_FILE, 'w') as f:
        json.dump({'chat_id': cid}, f)
    print(f"[INFO] Chat ID saved: {cid}")


def main_menu():
    return {
        'inline_keyboard': [
            [
                {'text': '📊 Current Prices',   'callback_data': 'prices'},
                {'text': '💼 Portfolio Status', 'callback_data': 'portfolio'},
            ],
            [
                {'text': '🔴 Force Close All Trades', 'callback_data': 'close_trade'},
            ],
        ]
    }


def tg_send(text, reply_markup=None):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
    except Exception as e:
        print(f"[TG ERROR] {e}")


def tg_answer_callback(callback_query_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={'callback_query_id': callback_query_id},
            timeout=5,
        )
    except Exception:
        pass


def get_balance():
    try:
        balance = exchange.fetch_balance()
        return float(balance['USDT']['free'])
    except Exception:
        return 0.0


def get_signal(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=60)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])

        df['rsi'] = ta.rsi(df['close'], length=14)
        bb = ta.bbands(df['close'], length=20, std=2)
        df['ema50'] = ta.ema(df['close'], length=50)
        bb_lower_col = [c for c in bb.columns if c.startswith('BBL')][0]
        bb_upper_col = [c for c in bb.columns if c.startswith('BBU')][0]
        df['bb_lower'] = bb[bb_lower_col]
        df['bb_upper'] = bb[bb_upper_col]

        row = df.iloc[-1]
        price    = float(row['close'])
        rsi      = float(row['rsi'])
        ema50    = float(row['ema50'])
        bb_lower = float(row['bb_lower'])
        bb_upper = float(row['bb_upper'])

        bb_width  = bb_upper - bb_lower
        buy_zone  = bb_lower + bb_width * 0.02
        sell_zone = bb_upper - bb_width * 0.02

        if rsi < 30 and price <= buy_zone and price < ema50:
            return 'BUY', price, rsi
        if rsi > 70 and price >= sell_zone and price > ema50:
            return 'SELL', price, rsi
        return 'WAIT', price, rsi
    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")
        return 'ERROR', 0.0, 0.0


def handle_signal(symbol, signal, price, rsi):
    with _lock:
        if symbol in open_trades:
            return

        balance    = get_balance()
        usable     = balance if balance > 0 else 1000.0
        allocation = usable * RISK_PER_TRADE
        size       = round(allocation / price, 6)
        opened_at  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        open_trades[symbol] = {
            'symbol': symbol, 'side': signal,
            'entry': price, 'size': size,
            'opened_at': opened_at,
        }

    emoji = '🟢' if signal == 'BUY' else '🔴'
    if signal == 'BUY':
        exit_rule = f"RSI ≥ {BUY_EXIT_RSI} or price touches upper Bollinger Band"
    else:
        exit_rule = f"RSI ≤ {SELL_EXIT_RSI} or price touches lower Bollinger Band"

    msg = (
        f"{emoji} <b>NEW SIGNAL — {signal}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Pair:        <b>{symbol}</b>\n"
        f"💰 Entry:       <b>${price:,.6f}</b>\n"
        f"📊 RSI:         <b>{rsi:.1f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 Smart Exit:  <i>{exit_rule}</i>\n"
        f"📦 Size:        <b>{size} units</b>\n"
        f"💵 Allocated:   <b>${allocation:,.2f}</b>  (5% of balance)\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {opened_at}"
    )
    print(f"[SIGNAL] {symbol}: {signal} @ ${price} | Smart Exit active")
    tg_send(msg, reply_markup=main_menu())


def check_smart_exit():
    """Smart Exit: close trades using live RSI and Bollinger Band reversal signals."""
    with _lock:
        snapshot = dict(open_trades)

    for symbol, t in snapshot.items():
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=60)
            df   = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])

            df['rsi'] = ta.rsi(df['close'], length=14)
            bb = ta.bbands(df['close'], length=20, std=2)
            bb_lower_col = [c for c in bb.columns if c.startswith('BBL')][0]
            bb_upper_col = [c for c in bb.columns if c.startswith('BBU')][0]

            row      = df.iloc[-1]
            price    = float(row['close'])
            rsi      = float(row['rsi'])
            bb_lower = float(bb[bb_lower_col].iloc[-1])
            bb_upper = float(bb[bb_upper_col].iloc[-1])

            pnl_pct = (
                (price - t['entry']) / t['entry'] * 100
                if t['side'] == 'BUY'
                else (t['entry'] - price) / t['entry'] * 100
            )
            pnl_str = f"{'🟢' if pnl_pct >= 0 else '🔴'} {pnl_pct:+.2f}%"

            # BUY exit: RSI overbought OR price reached upper band
            if t['side'] == 'BUY' and (rsi >= BUY_EXIT_RSI or price >= bb_upper):
                reason = f"RSI={rsi:.1f} ≥ {BUY_EXIT_RSI}" if rsi >= BUY_EXIT_RSI \
                         else f"Price touched upper BB (${bb_upper:,.6f})"
                with _lock:
                    open_trades.pop(symbol, None)
                tg_send(
                    f"🏁 <b>Smart Exit — BUY Closed</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📌 {symbol}\n"
                    f"📤 Exit Price: <b>${price:,.6f}</b>\n"
                    f"📊 P&L: <b>{pnl_str}</b>\n"
                    f"🧠 Reason: {reason}",
                    reply_markup=main_menu(),
                )
                print(f"[SMART EXIT] {symbol} BUY closed — {reason}")

            # SELL exit: RSI oversold OR price reached lower band
            elif t['side'] == 'SELL' and (rsi <= SELL_EXIT_RSI or price <= bb_lower):
                reason = f"RSI={rsi:.1f} ≤ {SELL_EXIT_RSI}" if rsi <= SELL_EXIT_RSI \
                         else f"Price touched lower BB (${bb_lower:,.6f})"
                with _lock:
                    open_trades.pop(symbol, None)
                tg_send(
                    f"🏁 <b>Smart Exit — SELL Closed</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📌 {symbol}\n"
                    f"📤 Exit Price: <b>${price:,.6f}</b>\n"
                    f"📊 P&L: <b>{pnl_str}</b>\n"
                    f"🧠 Reason: {reason}",
                    reply_markup=main_menu(),
                )
                print(f"[SMART EXIT] {symbol} SELL closed — {reason}")

        except Exception as e:
            print(f"[SMART EXIT ERROR] {symbol}: {e}")


def handle_callback(data):
    if data == 'prices':
        lines = []
        for s in SYMBOLS[:10]:
            try:
                ticker = exchange.fetch_ticker(s)
                lines.append(f"• {s}: <b>${ticker['last']:,.4f}</b>")
            except Exception:
                lines.append(f"• {s}: N/A")
        tg_send("📊 <b>Live Prices (Top 10)</b>\n" + "\n".join(lines), reply_markup=main_menu())

    elif data == 'portfolio':
        with _lock:
            snapshot = dict(open_trades)

        if snapshot:
            lines = [f"💼 <b>Open Trades ({len(snapshot)})</b>\n━━━━━━━━━━━━━━━━"]
            for symbol, t in snapshot.items():
                try:
                    current = float(exchange.fetch_ticker(symbol)['last'])
                    pnl_pct = (
                        (current - t['entry']) / t['entry'] * 100
                        if t['side'] == 'BUY'
                        else (t['entry'] - current) / t['entry'] * 100
                    )
                    pnl = pnl_pct * t['size'] * t['entry'] / 100
                    pnl_str = f"{'🟢' if pnl >= 0 else '🔴'} ${pnl:,.4f} ({pnl_pct:.2f}%)"
                except Exception:
                    current = t['entry']
                    pnl_str = "N/A"
                exit_rule = (
                    f"RSI ≥ {BUY_EXIT_RSI} or upper BB"
                    if t['side'] == 'BUY'
                    else f"RSI ≤ {SELL_EXIT_RSI} or lower BB"
                )
                lines.append(
                    f"\n📌 <b>{symbol}</b> — {t['side']}\n"
                    f"   Entry: ${t['entry']:,.6f} | Now: ${current:,.6f}\n"
                    f"   P&L: {pnl_str}\n"
                    f"   🧠 Smart Exit: {exit_rule}"
                )
            tg_send("\n".join(lines), reply_markup=main_menu())
        else:
            balance = get_balance()
            tg_send(
                f"💼 <b>Portfolio Status</b>\n━━━━━━━━━━━━━━━━\n"
                f"✅ No open trades\n💵 Free Balance: <b>${balance:,.2f}</b>",
                reply_markup=main_menu(),
            )

    elif data == 'close_trade':
        with _lock:
            snapshot = dict(open_trades)
            open_trades.clear()

        if snapshot:
            symbols_closed = ', '.join(snapshot.keys())
            tg_send(
                f"🔴 <b>All Trades Force Closed</b>\n{symbols_closed}",
                reply_markup=main_menu(),
            )
        else:
            tg_send("ℹ️ No open trades to close.", reply_markup=main_menu())


def telegram_poll():
    offset = 0
    print("[TG] Polling for updates...")
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={'offset': offset, 'timeout': 10},
                timeout=15,
            )
            for update in resp.json().get('result', []):
                offset = update['update_id'] + 1
                msg = update.get('message')
                cb  = update.get('callback_query')

                if msg and not chat_id:
                    save_chat_id(msg['chat']['id'])
                    tg_send(
                        "✅ <b>Sniper Bot Connected!</b>\n\n"
                        "Your Chat ID has been saved automatically.\n"
                        "I will send all trade signals directly here.",
                        reply_markup=main_menu(),
                    )

                if cb:
                    if not chat_id:
                        save_chat_id(cb['message']['chat']['id'])
                    tg_answer_callback(cb['id'])
                    handle_callback(cb['data'])

        except Exception as e:
            print(f"[TG POLL ERROR] {e}")
        time.sleep(2)


def market_loop():
    global _scan_count
    print(f"🔍 Monitoring {len(SYMBOLS)} pairs — scanning every ~15s, 1 trade per pair max")
    while True:
        _scan_count += 1
        now = datetime.now().strftime('%H:%M:%S')
        print(f"[SCAN #{_scan_count} @ {now}] Checking all {len(SYMBOLS)} pairs...")

        for symbol in SYMBOLS:
            with _lock:
                already_open = symbol in open_trades
            if already_open:
                continue   # pair already has a trade — skip, never duplicate

            signal, price, rsi = get_signal(symbol)
            if signal in ('BUY', 'SELL'):
                handle_signal(symbol, signal, price, rsi)
            time.sleep(0.5)   # reduced from 1s — respects rate limits, faster cycle

        check_smart_exit()
        time.sleep(15)   # full re-scan every ~15 seconds


_start_time = datetime.now()
_scan_count  = 0   # incremented by market_loop


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            trades = dict(open_trades)
        uptime  = str(datetime.now() - _start_time).split('.')[0]
        body = (
            f"✅ Sniper Bot — ALIVE\n"
            f"Uptime:      {uptime}\n"
            f"Scans done:  {_scan_count}\n"
            f"Open trades: {len(trades)} / {', '.join(trades.keys()) or 'none'}\n"
            f"Pairs:       {len(SYMBOLS)}\n"
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass   # silence default HTTP access logs


def health_server():
    port = int(os.environ.get('PORT', 3000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"[HEALTH] Keep-alive server listening on port {port}")
    server.serve_forever()


if __name__ == '__main__':
    if not BINANCE_API_KEY:
        print("[WARNING] BINANCE_API_KEY not set — running in signal-only mode.")

    load_chat_id()
    if chat_id:
        print(f"[INFO] Chat ID loaded: {chat_id}")
    else:
        print("[INFO] Send any message to your Telegram bot to register your Chat ID.")

    # Keep-alive HTTP server — must bind before market_loop starts
    threading.Thread(target=health_server, daemon=True).start()
    time.sleep(1)   # give the socket time to bind before the startup check

    threading.Thread(target=telegram_poll, daemon=True).start()

    print("🚀 Professional Sniper Bot running...")
    print(f"📋 Pairs: {', '.join(SYMBOLS)}")
    print("─" * 60)

    market_loop()
