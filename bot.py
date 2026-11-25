import asyncio, html, json, os, re, secrets, time, asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CRYPTO_WALLET = os.getenv("CRYPTO_WALLET", "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux")
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SUPPORT_USERNAME = "gerulev"
OWNER_ID = int(os.getenv("OWNER_ID", "622034883") or "622034883")
REVIEWS_CHANNEL_URL = "https://t.me/+7N5DvqV20UM3Nzll"
RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6") or "1.6")
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "115.0") or "115.0")

CATALOG, ADMIN_IDS, WAITING_INPUT = {}, set(), {}
LAST_ISSUED_ACCOUNT, PURCHASE_HISTORY, USER_RUB_BALANCE = {}, {}, {}
LAST_API_ACTION, TOTAL_SOLD_COUNT, REVIEWS, SUPPORT_MESSAGES = {}, 0, [], {}

DB_POOL = None
_SAVE_LOCK = asyncio.Lock()
CORS_HEADERS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}

async def init_db():
    global DB_POOL
    if not DATABASE_URL: return
    DB_POOL = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    async with DB_POOL.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, payload JSONB NOT NULL)")

async def load_data():
    global CATALOG, ADMIN_IDS, PURCHASE_HISTORY, USER_RUB_BALANCE, TOTAL_SOLD_COUNT, REVIEWS, SUPPORT_MESSAGES
    if not DB_POOL: return
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow("SELECT payload FROM bot_state WHERE id = 1")
            if not row or not row["payload"]: return
            raw = row["payload"]
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
            CATALOG, ADMIN_IDS = data.get("catalog", {}), set(int(x) for x in data.get("admin_ids", []))
            PURCHASE_HISTORY = {int(k): v for k, v in data.get("purchase_history", {}).items()}
            raw_balances = data.get("user_balances", {})
            USER_RUB_BALANCE = {int(k): float(v) for k, v in raw_balances.items() if float(v) < 1000000}
            TOTAL_SOLD_COUNT, REVIEWS = int(data.get("total_sold_count", 0)), data.get("reviews", [])
            SUPPORT_MESSAGES = {int(k): v for k, v in data.get("support_messages", {}).items()}
    except Exception as e: print(f"Load err: {e}")

async def save_data():
    if not DB_POOL: return
    payload = {"catalog": CATALOG, "admin_ids": list(ADMIN_IDS), "purchase_history": PURCHASE_HISTORY, "user_balances": USER_RUB_BALANCE, "total_sold_count": TOTAL_SOLD_COUNT, "reviews": REVIEWS, "support_messages": SUPPORT_MESSAGES}
    try:
        async with _SAVE_LOCK:
            async with DB_POOL.acquire() as conn:
                await conn.execute("INSERT INTO bot_state (id, payload) VALUES (1, $1::jsonb) ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload", json.dumps(payload, ensure_ascii=False))
    except Exception as e: print(f"Save err: {e}")

def is_owner(uid): return uid == OWNER_ID
def is_admin(uid): return uid == OWNER_ID or uid in ADMIN_IDS
def check_rate_limit(uid, act, gap=1.5):
    now = time.monotonic(); key = (uid, act); prev = LAST_API_ACTION.get(key, 0.0)
    if now - prev < gap: return False
    LAST_API_ACTION[key] = now; return True
def get_rub_balance(uid): return round(float(USER_RUB_BALANCE.get(uid, 0.0)), 2)
async def add_rub_balance(uid, amt):
    nxt = round(max(0.0, get_rub_balance(uid) + float(amt)), 2)
    USER_RUB_BALANCE[uid] = nxt; await save_data(); return nxt
async def deduct_rub_balance(uid, amt):
    amt, cur = round(float(amt), 2), get_rub_balance(uid)
    if cur < amt: return False
    USER_RUB_BALANCE[uid] = round(cur - amt, 2); await save_data(); return True
async def notify_owner(b, txt):
    if not OWNER_ID: return
    try: await b.send_message(chat_id=OWNER_ID, text=txt, parse_mode="HTML")
    except: pass
def _parse_account_secret(raw):
    if ":" in raw:
        left, right = raw.split(":", 1)
        if len(left) > 40: return left.strip(), right.strip()
        elif len(right) > 40: return right.strip(), left.strip()
        return "", raw.strip()
    if len(raw) > 40: return raw.strip(), ""
    return "", raw.strip()
async def fetch_tg_login_code(sess):
    if not TG_API_ID or not TG_API_HASH or not sess: return None
    client = TelegramClient(StringSession(sess), TG_API_ID, TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized(): return None
        msgs = await client.get_messages(777000, limit=5)
        for m in msgs:
            match = re.search(r"\b(\d{5})\b", m.message or "")
            if match: return match.group(1)
    except: return None
    finally: await client.disconnect()
def build_state_payload(uid):
    items = [{"key": k, "title": v["title"], "description": v.get("description", ""), "price_rub": round(float(v["price_rub"]), 2), "count": len(v.get("accounts", []))} for k, v in CATALOG.items() if v.get("accounts")]
    recent = [{"title": r.get("title", "Acc"), "phone": r.get("phone", "")} for h in PURCHASE_HISTORY.values() for r in h[-2:]][-5:]
    sorted_revs = sorted(REVIEWS, key=lambda x: x.get("timestamp", 0), reverse=True)[:10]
    return {"ok": True, "user_id": uid, "balance_rub": get_rub_balance(uid), "rub_per_star": RUB_PER_STAR, "rub_per_ton": RUB_PER_TON, "crypto_wallet": CRYPTO_WALLET, "orders": PURCHASE_HISTORY.get(uid, []), "items": items, "total_sold_count": TOTAL_SOLD_COUNT, "recent_sales": recent, "reviews": sorted_revs, "support_history": SUPPORT_MESSAGES.get(uid, [])}

# === HANDLERS ===
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
@dp.message(CommandStart())
async def cmd_start(m: Message):
    WAITING_INPUT.pop(m.from_user.id, None)
    await m.answer("Бот работает. Откройте мини-приложение для покупок.")
@dp.message(Command("reply"))
async def cmd_reply(m: Message):
    if not is_admin(m.from_user.id): return
    p = (m.text or "").split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit(): return await m.answer("Формат: /reply ID ТЕКСТ")
    uid, txt = int(p[1]), p[2].strip()
    SUPPORT_MESSAGES.setdefault(uid, []).append({"from": "admin", "text": txt, "time": time.time()})
    await save_data()
    await m.answer(f"✅ Отправлено {uid}")
    try: await bot.send_message(uid, f"💬 <b>Ответ поддержки:</b>\n{txt}", parse_mode="HTML")
    except: pass
# === API ===
def _cors(p, s=200): return web.json_response(p, status=s, headers=CORS_HEADERS)
async def api_options(r): return web.Response(status=204, headers=CORS_HEADERS)
async def api_state(r):
    uid = r.query.get("user_id", "").strip()
    return _cors(build_state_payload(int(uid))) if uid.isdigit() else _cors({"ok": False}, 400)
async def api_buy(r):
    global TOTAL_SOLD_COUNT
    data = await r.json(); uid, key = int(data.get("user_id") or 0), str(data.get("item_key") or "").strip()
    if not uid or not key: return _cors({"ok": False, "error": "Bad req"}, 400)
    if not check_rate_limit(uid, "buy", 1.2): return _cors({"ok": False, "error": "Slow down"}, 429)
    item = CATALOG.get(key)
    if not item or not item.get("accounts"): return _cors({"ok": False, "error": "Out of stock"}, 400)
    price = float(item["price_rub"])
    if get_rub_balance(uid) < price: return _cors({"ok": False, "error": "No money"}, 400)
    acc = item["accounts"].pop(0)
    if not await deduct_rub_balance(uid, price): item["accounts"].insert(0, acc); return _cors({"ok": False, "error": "Deduct err"}, 400)
    TOTAL_SOLD_COUNT += 1; oid = f"ORD-{secrets.token_hex(3).upper()}"
    phone, sess, tfa = acc.get("phone", ""), *_parse_account_secret(acc.get("secret", ""))
    rec = {"order_id": oid, "title": item["title"], "phone": phone, "session_string": sess, "two_fa": tfa, "code": ""}
    LAST_ISSUED_ACCOUNT[uid] = rec; PURCHASE_HISTORY.setdefault(uid, []).append(rec); await save_data()
    return _cors({"ok": True, "order_id": oid, "phone": phone, "two_fa": tfa, "code": "", "balance_rub": get_rub_balance(uid)})
async def api_code(r):
    data = await r.json(); uid, oid = int(data.get("user_id") or 0), str(data.get("order_id") or "").strip()
    if not check_rate_limit(uid, "code", 2.0): return _cors({"ok": False, "error": "Slow"}, 429)
    target = next((rec for rec in reversed(PURCHASE_HISTORY.get(uid, [])) if rec.get("order_id") == oid), None) or LAST_ISSUED_ACCOUNT.get(uid)
    if not target: return _cors({"ok": False, "error": "Not found"}, 404)
    if target.get("code"): return _cors({"ok": True, "code": target["code"]})
    code = await fetch_tg_login_code(target.get("session_string", ""))
    if code: target["code"] = code; await save_data(); return _cors({"ok": True, "code": code})
    if bot: await notify_owner(bot, f"📩 Запрос кода от {uid} ({target.get('phone')})")
    return _cors({"ok": False, "error": "Requested"})
async def api_support_send(r):
    data = await r.json(); uid, txt = int(data.get("user_id") or 0), str(data.get("text") or "").strip()
    SUPPORT_MESSAGES.setdefault(uid, []).append({"from": "user", "text": txt, "time": time.time()}); await save_data()
    if bot: await notify_owner(bot, f"🆘 Сообщение от {uid}:\n{txt}\n/reply {uid} ...")
    return _cors({"ok": True})
async def api_support_get(r):
    uid = r.query.get("user_id", "").strip(); return _cors({"ok": True, "messages": SUPPORT_MESSAGES.get(int(uid), [])}) if uid.isdigit() else _cors({"ok": False}, 400)
async def api_review_send(r):
    data = await r.json(); uid, txt, rat = int(data.get("user_id") or 0), str(data.get("text") or ""), int(data.get("rating") or 5)
    REVIEWS.append({"user_id": uid, "text": txt, "rating": max(1, min(5, rat)), "timestamp": time.time()}); await save_data()
    return _cors({"ok": True})
async def api_deposit(r):
    data = await r.json(); uid, method = int(data.get("user_id") or 0), str(data.get("method") or "").lower()
    amt = float(data.get("amount") or 0); s2b = bool(data.get("send_to_bot", True))
    if method == "stars":
        st = max(1, int(round(amt))); rub = round(st * RUB_PER_STAR, 2)
        if s2b:
            await bot.send_invoice(uid, f"Пополнение ({rub} ₽)", f"Зачисление {rub} ₽ за {st} ⭐", f"rub_stars:{uid}:{st}", "XTR", [LabeledPrice(label=f"{st} ⭐", amount=st)])
            return _cors({"ok": True, "sent_to_bot": True})
        return _cors({"ok": True, "invoice_link": await bot.create_invoice_link(f"Пополнение", f"Зачисление", f"rub_stars:{uid}:{st}", "XTR", [LabeledPrice(label=f"{st} ⭐", amount=st)])})
    rub = round(amt * RUB_PER_TON, 2)
    await notify_owner(bot, f"💎 TON от {uid}: {amt} TON (~{rub} ₽)\nВыдать: {uid} {rub}")
    return _cors({"ok": True, "wallet": CRYPTO_WALLET})

async def start_http_server():
    app = web.Application()
    paths = ("/api/state", "/api/buy", "/api/deposit", "/api/code", "/api/support/send", "/api/support/get", "/api/review/send")
    for p in paths: app.router.add_options(p, api_options)
    app.router.add_get("/api/state", api_state); app.router.add_post("/api/buy", api_buy); app.router.add_post("/api/deposit", api_deposit); app.router.add_post("/api/code", api_code); app.router.add_post("/api/support/send", api_support_send); app.router.add_get("/api/support/get", api_support_get); app.router.add_post("/api/review/send", api_review_send)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    return runner

async def main():
    await init_db(); await load_data(); runner = await start_http_server()
    try:
        if bot: await dp.start_polling(bot)
    finally: await runner.cleanup(); await DB_POOL.close() if DB_POOL else None

if __name__ == "__main__": asyncio.run(main())