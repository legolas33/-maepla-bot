"""
========================================
Line Bot สำหรับควบคุม แม่ปลาปากกาเขียว
========================================
คำสั่งใน Line:
  /start     - เริ่ม Bot
  /stop      - หยุด Bot
  /status    - ดูสถานะ + Order ที่เปิดอยู่
  /sl 5      - ปรับ Stop Loss เป็น $5
  /tp 5      - ปรับ Take Profit เป็น $5
  /lot 0.01  - ปรับ Lot size
  /closeall  - ปิด Order ทั้งหมด
  /help      - ดูคำสั่งทั้งหมด
========================================
"""

import os
import asyncio
import threading
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from metaapi_cloud_sdk import MetaApi

# ─────────────────────────────────────
#  ⚙️  ตั้งค่า (ใส่ใน .env หรือแก้ตรงนี้)
# ─────────────────────────────────────
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "ใส่ Channel Secret ของคุณ")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ใส่ Channel Access Token ของคุณ")
META_API_TOKEN            = os.getenv("META_API_TOKEN", "ใส่ MetaAPI Token ของคุณ")
ACCOUNT_ID                = os.getenv("ACCOUNT_ID", "ใส่ MT5 Account ID ของคุณ")

SYMBOL    = "XAUUSD"
LOT_SIZE  = 0.01
SL_DOLLAR = 3.0
TP_DOLLAR = 3.0

# ─────────────────────────────────────
#  🌐 State ของ Bot
# ─────────────────────────────────────
bot_state = {
    "running": False,
    "connection": None,
    "sl": SL_DOLLAR,
    "tp": TP_DOLLAR,
    "lot": LOT_SIZE,
    "user_id": None,   # Line User ID ที่สั่ง Start
    "trades_today": 0,
    "profit_today": 0.0,
}

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ─────────────────────────────────────
#  📤 ส่งข้อความกลับ Line
# ─────────────────────────────────────
def send_line(user_id: str, msg: str):
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Line send error: {e}")

# ─────────────────────────────────────
#  📊 PA Detection (จากระบบปากกาเขียว)
# ─────────────────────────────────────
def is_nayya_level(price):
    return round(price) % 5 == 0

def detect_pa_buy(candles):
    if len(candles) < 3:
        return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    def body(c): return abs(c['close'] - c['open'])
    def lower_wick(c): return min(c['open'], c['close']) - c['low']
    def midpoint(c): return (c['high'] + c['low']) / 2
    def is_red(c): return c['close'] < c['open']
    def is_green(c): return c['close'] > c['open']

    if is_green(c3) and lower_wick(c3) >= body(c3) * 2:
        return 'Pat1'
    if is_red(c2) and is_green(c3) and c3['close'] >= midpoint(c2):
        return 'Pat2'
    if is_red(c1) and is_green(c3) and body(c2) <= body(c1) * 0.3:
        if c3['close'] >= midpoint(c1):
            return 'Pat3'
    return None

def detect_pa_sell(candles):
    if len(candles) < 3:
        return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    def body(c): return abs(c['close'] - c['open'])
    def upper_wick(c): return c['high'] - max(c['open'], c['close'])
    def midpoint(c): return (c['high'] + c['low']) / 2
    def is_red(c): return c['close'] < c['open']
    def is_green(c): return c['close'] > c['open']

    if is_red(c3) and upper_wick(c3) >= body(c3) * 2:
        return 'Pat1'
    if is_green(c2) and is_red(c3) and c3['close'] <= midpoint(c2):
        return 'Pat2'
    if is_green(c1) and is_red(c3) and body(c2) <= body(c1) * 0.3:
        if c3['close'] <= midpoint(c1):
            return 'Pat3'
    return None

def detect_m5_buy(candles):
    if len(candles) < 5:
        return False
    lows = [c['low'] for c in candles[-5:]]
    closes = [c['close'] for c in candles[-5:]]
    if abs(lows[-1] - lows[-3]) <= 0.5 and closes[-1] > closes[-2]:
        return True
    if lows[-1] > lows[-2] > lows[-3] and closes[-1] > closes[-2]:
        return True
    return False

def detect_m5_sell(candles):
    if len(candles) < 5:
        return False
    highs = [c['high'] for c in candles[-5:]]
    closes = [c['close'] for c in candles[-5:]]
    if abs(highs[-1] - highs[-3]) <= 0.5 and closes[-1] < closes[-2]:
        return True
    if highs[-1] < highs[-2] < highs[-3] and closes[-1] < closes[-2]:
        return True
    return False

# ─────────────────────────────────────
#  🔄 Bot Loop หลัก
# ─────────────────────────────────────
async def bot_loop():
    api = MetaApi(META_API_TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    await account.deploy()
    await account.wait_connected()
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    bot_state["connection"] = connection

    user_id = bot_state["user_id"]
    send_line(user_id, "✅ เชื่อมต่อ MT5 สำเร็จ!\n🐟 แม่ปลาปากกาเขียว BOT เริ่มทำงานแล้ว")

    while bot_state["running"]:
        try:
            conn = bot_state["connection"]

            # ── Monitor positions ──
            positions = await conn.get_positions()
            for pos in positions:
                if pos.get('symbol') != SYMBOL:
                    continue
                profit = pos.get('profit', 0)
                pos_id = pos.get('id')
                sl = bot_state["sl"]
                tp = bot_state["tp"]

                if profit <= -sl:
                    await conn.close_position(pos_id)
                    bot_state["profit_today"] += profit
                    send_line(user_id,
                        f"🔴 SL HIT!\n"
                        f"ขาดทุน: ${abs(profit):.2f}\n"
                        f"ปิด Order {pos_id} แล้ว")

                elif profit >= tp:
                    await conn.close_position(pos_id)
                    bot_state["profit_today"] += profit
                    bot_state["trades_today"] += 1
                    send_line(user_id,
                        f"🟢 TP HIT!\n"
                        f"กำไร: ${profit:.2f}\n"
                        f"ปิด Order {pos_id} แล้ว")

            # ── ตรวจสัญญาณ H4 ──
            candles_h4 = await conn.get_historical_candles(
                SYMBOL, '4h', datetime.utcnow(), 10)
            current_price = candles_h4[-1]['close'] if candles_h4 else 0

            open_positions = [p for p in positions if p.get('symbol') == SYMBOL]
            if len(open_positions) < 3:   # จำกัด max 3 order พร้อมกัน

                sig_buy = detect_pa_buy(candles_h4)
                if sig_buy and is_nayya_level(current_price):
                    candles_m5 = await conn.get_historical_candles(
                        SYMBOL, '5m', datetime.utcnow(), 10)
                    if detect_m5_buy(candles_m5):
                        result = await conn.create_market_buy_order(
                            SYMBOL, bot_state["lot"])
                        send_line(user_id,
                            f"🟢 เปิด BUY!\n"
                            f"สัญญาณ: Sig Buy {sig_buy}\n"
                            f"ราคา: {current_price:.2f}\n"
                            f"Lot: {bot_state['lot']}\n"
                            f"SL: ${bot_state['sl']} | TP: ${bot_state['tp']}")

                sig_sell = detect_pa_sell(candles_h4)
                if sig_sell and is_nayya_level(current_price):
                    candles_m5 = await conn.get_historical_candles(
                        SYMBOL, '5m', datetime.utcnow(), 10)
                    if detect_m5_sell(candles_m5):
                        result = await conn.create_market_sell_order(
                            SYMBOL, bot_state["lot"])
                        send_line(user_id,
                            f"🔴 เปิด SELL!\n"
                            f"สัญญาณ: Sig Sell {sig_sell}\n"
                            f"ราคา: {current_price:.2f}\n"
                            f"Lot: {bot_state['lot']}\n"
                            f"SL: ${bot_state['sl']} | TP: ${bot_state['tp']}")

        except Exception as e:
            print(f"Bot loop error: {e}")

        await asyncio.sleep(2)

def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot_loop())

# ─────────────────────────────────────
#  💬 จัดการคำสั่ง Line
# ─────────────────────────────────────
def handle_command(user_id: str, text: str):
    text = text.strip().lower()
    parts = text.split()
    cmd = parts[0]

    # /start
    if cmd == "/start":
        if bot_state["running"]:
            return "⚠️ Bot กำลังทำงานอยู่แล้วครับ"
        bot_state["running"] = True
        bot_state["user_id"] = user_id
        bot_state["trades_today"] = 0
        bot_state["profit_today"] = 0.0
        t = threading.Thread(target=start_bot_thread, daemon=True)
        t.start()
        return "🔄 กำลังเชื่อมต่อ MT5...\nรอสักครู่นะครับ"

    # /stop
    elif cmd == "/stop":
        if not bot_state["running"]:
            return "⚠️ Bot ไม่ได้ทำงานอยู่ครับ"
        bot_state["running"] = False
        bot_state["connection"] = None
        return (f"🛑 หยุด Bot แล้วครับ\n"
                f"📊 สรุปวันนี้:\n"
                f"  เทรด: {bot_state['trades_today']} รอบ\n"
                f"  กำไร/ขาดทุน: ${bot_state['profit_today']:.2f}")

    # /status
    elif cmd == "/status":
        status = "🟢 กำลังทำงาน" if bot_state["running"] else "🔴 หยุดทำงาน"
        conn = bot_state.get("connection")
        order_text = ""
        if conn and bot_state["running"]:
            try:
                loop = asyncio.new_event_loop()
                positions = loop.run_until_complete(conn.get_positions())
                xau_pos = [p for p in positions if p.get('symbol') == SYMBOL]
                if xau_pos:
                    order_text = "\n\n📋 Order ที่เปิดอยู่:"
                    for p in xau_pos:
                        direction = "BUY 🟢" if p.get('type') == 'POSITION_TYPE_BUY' else "SELL 🔴"
                        profit = p.get('profit', 0)
                        emoji = "💰" if profit >= 0 else "💸"
                        order_text += (f"\n{direction} | {p.get('symbol')}"
                                      f"\n  {emoji} กำไร/ขาดทุน: ${profit:.2f}"
                                      f"\n  ราคาเข้า: {p.get('openPrice', 0):.2f}")
                else:
                    order_text = "\n\n📋 ไม่มี Order เปิดอยู่"
            except:
                order_text = "\n\n⚠️ ดึงข้อมูล Order ไม่ได้"

        return (f"🤖 สถานะ Bot: {status}\n"
                f"━━━━━━━━━━━━\n"
                f"⚙️ การตั้งค่า:\n"
                f"  Lot: {bot_state['lot']}\n"
                f"  SL: ${bot_state['sl']}\n"
                f"  TP: ${bot_state['tp']}\n"
                f"━━━━━━━━━━━━\n"
                f"📈 วันนี้:\n"
                f"  เทรด: {bot_state['trades_today']} รอบ\n"
                f"  กำไร/ขาดทุน: ${bot_state['profit_today']:.2f}"
                f"{order_text}")

    # /sl [number]
    elif cmd == "/sl":
        if len(parts) < 2:
            return "❌ ใช้: /sl 5  (ตัวอย่าง: SL = $5)"
        try:
            val = float(parts[1])
            bot_state["sl"] = val
            return f"✅ ตั้ง Stop Loss = ${val} แล้วครับ"
        except:
            return "❌ ตัวเลขไม่ถูกต้อง เช่น /sl 5"

    # /tp [number]
    elif cmd == "/tp":
        if len(parts) < 2:
            return "❌ ใช้: /tp 5  (ตัวอย่าง: TP = $5)"
        try:
            val = float(parts[1])
            bot_state["tp"] = val
            return f"✅ ตั้ง Take Profit = ${val} แล้วครับ"
        except:
            return "❌ ตัวเลขไม่ถูกต้อง เช่น /tp 5"

    # /lot [number]
    elif cmd == "/lot":
        if len(parts) < 2:
            return "❌ ใช้: /lot 0.01"
        try:
            val = float(parts[1])
            bot_state["lot"] = val
            return f"✅ ตั้ง Lot Size = {val} แล้วครับ"
        except:
            return "❌ ตัวเลขไม่ถูกต้อง เช่น /lot 0.01"

    # /closeall
    elif cmd == "/closeall":
        conn = bot_state.get("connection")
        if not conn:
            return "⚠️ Bot ยังไม่ได้เชื่อมต่อครับ"
        try:
            loop = asyncio.new_event_loop()
            positions = loop.run_until_complete(conn.get_positions())
            xau_pos = [p for p in positions if p.get('symbol') == SYMBOL]
            if not xau_pos:
                return "📋 ไม่มี Order ที่เปิดอยู่ครับ"
            for p in xau_pos:
                loop.run_until_complete(conn.close_position(p['id']))
            return f"✅ ปิด Order ทั้งหมด {len(xau_pos)} รายการแล้วครับ"
        except Exception as e:
            return f"❌ ปิด Order ไม่สำเร็จ: {e}"

    # /help
    elif cmd == "/help":
        return ("🐟 แม่ปลาปากกาเขียว BOT\n"
                "━━━━━━━━━━━━━━━━\n"
                "คำสั่งทั้งหมด:\n\n"
                "▶️ /start — เริ่ม Bot\n"
                "⏹ /stop — หยุด Bot\n"
                "📊 /status — ดูสถานะ + Order\n"
                "━━━━━━━━━━━━━━━━\n"
                "⚙️ ปรับค่า:\n"
                "/sl 3 — ตั้ง Stop Loss ($)\n"
                "/tp 3 — ตั้ง Take Profit ($)\n"
                "/lot 0.01 — ตั้ง Lot size\n"
                "━━━━━━━━━━━━━━━━\n"
                "🛑 /closeall — ปิด Order ทั้งหมด\n"
                "❓ /help — ดูคำสั่ง")

    else:
        return "❓ ไม่รู้จักคำสั่งนี้ครับ พิมพ์ /help เพื่อดูคำสั่งทั้งหมด"

# ─────────────────────────────────────
#  🌐 Flask Webhook
# ─────────────────────────────────────
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    if text.startswith('/'):
        reply = handle_command(user_id, text)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

if __name__ == "__main__":
    print("🚀 Line Bot Server เริ่มทำงานที่ port 5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
