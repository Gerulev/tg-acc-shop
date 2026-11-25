import asyncio
import html
import json
import os
import re
import secrets
import time
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CRYPTO_WALLET = os.getenv(
    "CRYPTO_WALLET",
    "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux",
)
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SUPPORT_USERNAME = "gerulev"
OWNER_ID = int(os.getenv("OWNER_ID", "622034883") or "622034883")
REVIEWS_CHANNEL_URL = "https://t.me/+7N5DvqV20UM3Nzll"

RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6") or "1.6")
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "115.0") or "115.0")

CATALOG: dict[str, dict] = {}
ADMIN_IDS: set[int] = set()
WAITING_INPUT: dict[int, str] = {}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[dict]] = {}
USER_RUB_BALANCE: dict[int, float] = {}
LAST_API_ACTION: dict[tuple[int, str], float] = {}
TOTAL_SOLD_COUNT: int = 0
RECENT_PURCHASES: list[dict] = []
REVIEWS_LIST: list[dict] = []
SUPPORT_MESSAGES: list[dict] = []

DB_POOL: asyncpg.Pool | None = None
_SAVE_LOCK = asyncio.Lock()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def init_db() -> None:
    global DB_POOL
    if not DATABASE_URL:
        print("ÐÐÐÐÐÐÐÐ: DATABASE_URL Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ â Ð´Ð°Ð½Ð½ÑÐµ Ð½Ðµ Ð±ÑÐ´ÑÑ ÑÐ¾ÑÑÐ°Ð½ÑÑÑÑÑ Ð² PostgreSQL.")
        return

    DB_POOL = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY,
                payload JSONB NOT NULL
            )
            """
        )


async def load_data() -> None:
    global CATALOG, ADMIN_IDS, PURCHASE_HISTORY, USER_RUB_BALANCE, TOTAL_SOLD_COUNT, RECENT_PURCHASES, REVIEWS_LIST, SUPPORT_MESSAGES
    if not DB_POOL:
        return
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow("SELECT payload FROM bot_state WHERE id = 1")
            if not row or not row["payload"]:
                return
            raw = row["payload"]
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)

            CATALOG = data.get("catalog", {})
            ADMIN_IDS = set(int(x) for x in data.get("admin_ids", []))

            raw_history = data.get("purchase_history", {})
            PURCHASE_HISTORY = {int(k): v for k, v in raw_history.items()}

            raw_balances = data.get("user_balances", {})
            USER_RUB_BALANCE = {int(k): float(v) for k, v in raw_balances.items()}

            TOTAL_SOLD_COUNT = int(data.get("total_sold_count", 0))
            RECENT_PURCHASES = data.get("recent_purchases", [])
            REVIEWS_LIST = data.get("reviews", [])
            SUPPORT_MESSAGES = data.get("support_messages", [])
    except Exception as e:
        print(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð·Ð°Ð³ÑÑÐ·ÐºÐµ Ð´Ð°Ð½Ð½ÑÑ Ð¸Ð· PostgreSQL: {e}")


async def save_data() -> None:
    if not DB_POOL:
        return
    payload = {
        "catalog": CATALOG,
        "admin_ids": list(ADMIN_IDS),
        "purchase_history": PURCHASE_HISTORY,
        "user_balances": USER_RUB_BALANCE,
        "total_sold_count": TOTAL_SOLD_COUNT,
        "recent_purchases": RECENT_PURCHASES,
        "reviews": REVIEWS_LIST,
        "support_messages": SUPPORT_MESSAGES,
    }
    try:
        async with _SAVE_LOCK:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_state (id, payload)
                    VALUES (1, $1::jsonb)
                    ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload
                    """,
                    json.dumps(payload, ensure_ascii=False),
                )
    except Exception as e:
        print(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð¸Ð¸ Ð´Ð°Ð½Ð½ÑÑ Ð² PostgreSQL: {e}")


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def check_rate_limit(user_id: int, action: str, min_gap_sec: float = 1.5) -> bool:
    now = time.monotonic()
    key = (user_id, action)
    prev = LAST_API_ACTION.get(key, 0.0)
    if now - prev < min_gap_sec:
        return False
    LAST_API_ACTION[key] = now
    return True


def get_rub_balance(user_id: int) -> float:
    return round(float(USER_RUB_BALANCE.get(user_id, 0.0)), 2)


async def add_rub_balance(user_id: int, amount_rub: float) -> float:
    nxt = round(max(0.0, get_rub_balance(user_id) + float(amount_rub)), 2)
    USER_RUB_BALANCE[user_id] = nxt
    await save_data()
    return nxt


async def deduct_rub_balance(user_id: int, amount_rub: float) -> bool:
    amt = round(float(amount_rub), 2)
    cur = get_rub_balance(user_id)
    if cur < amt:
        return False
    USER_RUB_BALANCE[user_id] = round(cur - amt, 2)
    await save_data()
    return True


async def notify_owner(bot_instance: Bot, text: str) -> None:
    if not OWNER_ID:
        return
    try:
        await bot_instance.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
    except Exception:
        pass


def _parse_account_secret(raw_secret: str) -> tuple[str, str]:
    session_str = ""
    two_fa = ""
    if ":" in raw_secret:
        left, right = raw_secret.split(":", 1)
        if len(left) > 40:
            session_str, two_fa = left.strip(), right.strip()
        elif len(right) > 40:
            two_fa, session_str = left.strip(), right.strip()
        else:
            two_fa = raw_secret.strip()
    else:
        if len(raw_secret) > 40:
            session_str = raw_secret.strip()
        else:
            two_fa = raw_secret.strip()
    return session_str, two_fa


async def fetch_tg_login_code(session_str: str) -> str | None:
    if not TG_API_ID or not TG_API_HASH or not session_str:
        return None
    client = TelegramClient(StringSession(session_str), TG_API_ID, TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        messages = await client.get_messages(777000, limit=5)
        for msg in messages:
            match = re.search(r"(\d{5})", msg.message or "")
            if match:
                return match.group(1)
        return None
    except Exception:
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def build_state_payload(user_id: int) -> dict:
    items_out = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        items_out.append({
            "key": key,
            "title": item["title"],
            "description": item.get("description", ""),
            "price_rub": round(float(item["price_rub"]), 2),
            "count": cnt,
        })

    user_chat = [m for m in SUPPORT_MESSAGES if m.get("user_id") == user_id]

    return {
        "ok": True,
        "user_id": user_id,
        "balance_rub": get_rub_balance(user_id),
        "rub_per_star": RUB_PER_STAR,
        "rub_per_ton": RUB_PER_TON,
        "crypto_wallet": CRYPTO_WALLET,
        "orders": PURCHASE_HISTORY.get(user_id, []),
        "items": items_out,
        "total_sold_count": TOTAL_SOLD_COUNT,
        "recent_purchases": RECENT_PURCHASES[:15],
        "reviews": REVIEWS_LIST,
        "chat": user_chat,
        "reviews_channel": REVIEWS_CHANNEL_URL,
    }


def _format_user_label(user) -> str:
    if not user:
        return "Ð´ÑÑÐ³"
    if getattr(user, "username", None):
        return f"@{html.escape(user.username)}"
    full_name = " ".join(
        part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
    ).strip()
    return html.escape(full_name) if full_name else "Ð´ÑÑÐ³"


def get_welcome_text(user=None) -> str:
    user_label = _format_user_label(user)
    return (
        f"ð <b>ÐÑÐ¸Ð²ÐµÑ, {user_label} â Ð´Ð¾Ð±ÑÐ¾ Ð¿Ð¾Ð¶Ð°Ð»Ð¾Ð²Ð°ÑÑ Ð² Dark Store.</b>

"
        "ÐÐ´ÐµÑÑ ÑÐ¾Ð±ÑÐ°Ð½Ñ Ð°ÐºÑÑÐ°Ð»ÑÐ½ÑÐµ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¸, Ð±ÑÑÑÑÐ°Ñ Ð²ÑÐ´Ð°ÑÐ° ÑÑÐ°Ð·Ñ Ð¿Ð¾ÑÐ»Ðµ Ð¾ÑÐ¾ÑÐ¼Ð»ÐµÐ½Ð¸Ñ "
        "Ð¸ Ð¿ÑÑÐ¼Ð¾Ð¹ ÐºÐ¾Ð½ÑÐ°ÐºÑ Ñ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐ¾Ð¹ Ð±ÐµÐ· Ð»Ð¸ÑÐ½ÐµÐ¹ ÑÑÐµÑÑ. "
        "ÐÑÐºÑÑÐ²Ð°Ð¹ ÐºÐ°ÑÐ°Ð»Ð¾Ð³, ÑÐ¼Ð¾ÑÑÐ¸ Ð½Ð°Ð»Ð¸ÑÐ¸Ðµ Ð¸ Ð·Ð°Ð±Ð¸ÑÐ°Ð¹ Ð½ÑÐ¶Ð½ÑÐ¹ Ð²Ð°ÑÐ¸Ð°Ð½Ñ Ð² Ð¿Ð°ÑÑ ÐºÐ»Ð¸ÐºÐ¾Ð².

"
        f"ð <b>ÐÑÐ¾Ð´Ð°Ð½Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð²:</b> {TOTAL_SOLD_COUNT}"
    )


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="ð ÐÐÐ¢ÐÐÐÐ / ÐÐÐÐÐÐÐ", callback_data="menu_catalog")],
        [
            InlineKeyboardButton(text="ð¤ ÐÑÐ¾ÑÐ¸Ð»Ñ", callback_data="menu_profile"),
            InlineKeyboardButton(text="ð³ ÐÐ¾Ð¿Ð¾Ð»Ð½Ð¸ÑÑ", callback_data="menu_deposit"),
        ],
        [
            InlineKeyboardButton(text="â­ ÐÑÐ·ÑÐ²Ñ", url=REVIEWS_CHANNEL_URL),
            InlineKeyboardButton(text="ð ÐÑÐ°Ð²Ð¸Ð»Ð°", callback_data="menu_rules"),
        ],
        [InlineKeyboardButton(text="ð ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton(text="ð ÐÐ°Ð½ÐµÐ»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", callback_data="owner_panel")])
    elif is_admin(user_id):
        rows.append([InlineKeyboardButton(text="ð  ÐÐ´Ð¼Ð¸Ð½ÐºÐ°", callback_data="staff_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"â ÐÐ°Ð»Ð¸ÑÑ Ð² {item['title']} ({cnt} ÑÑ.)",
                callback_data=f"staff_add:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="ð ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ", callback_data="owner_new_cat")])
    rows.append([InlineKeyboardButton(text="ð° ÐÑÐ´Ð°ÑÑ Ð±Ð°Ð»Ð°Ð½Ñ (â½)", callback_data="owner_give_bal")])
    rows.append([
        InlineKeyboardButton(text="â ÐÑÐ´Ð°ÑÑ Ð°Ð´Ð¼Ð¸Ð½ÐºÑ", callback_data="owner_add_adm"),
        InlineKeyboardButton(text="â Ð¡Ð½ÑÑÑ Ð°Ð´Ð¼Ð¸Ð½ÐºÑ", callback_data="owner_del_adm"),
    ])
    rows.append([InlineKeyboardButton(text="ð Ð¡Ð¼Ð¾ÑÑÐµÑÑ Ð±Ð°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"â ÐÐ°Ð»Ð¸ÑÑ Ð² {item['title']} ({cnt} ÑÑ.)",
                callback_data=f"staff_add:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="ð Ð¡Ð¼Ð¾ÑÑÐµÑÑ Ð±Ð°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_items_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"{item['title']} â {item['price_rub']:.0f} â½ ({cnt} ÑÑ.)",
                callback_data=f"item_view:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_view_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="â ÐÑÐ¿Ð¸ÑÑ Ð·Ð° â½", callback_data=f"buy_acc:{key}")],
            [InlineKeyboardButton(text="â¬ï¸ Ð ÐºÐ°ÑÐ°Ð»Ð¾Ð³", callback_data="menu_catalog")],
        ]
    )


def issued_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ð© ÐÐ¾Ð»ÑÑÐ¸ÑÑ / Ð·Ð°Ð¿ÑÐ¾ÑÐ¸ÑÑ ÐºÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°", callback_data="req_code")],
            [InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
        ]
    )


def deposit_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="â­ ÐÐ²ÑÐ·Ð´Ñ â â½", callback_data="dep_method:stars"),
                InlineKeyboardButton(text="ð TON â â½", callback_data="dep_method:ton"),
            ],
            [InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
        ]
    )


def back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ð ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton(text="â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
        ]
    )


if not BOT_TOKEN:
    print("ÐÐÐÐÐÐÐÐ: BOT_TOKEN Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ Ð² Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÑ Ð¾ÐºÑÑÐ¶ÐµÐ½Ð¸Ñ.")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    WAITING_INPUT.pop(message.from_user.id, None)
    await message.answer(
        get_welcome_text(message.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>/reply USER_ID Ð¢ÐÐÐ¡Ð¢_ÐÐ¢ÐÐÐ¢Ð</code>", parse_mode="HTML")
        return

    uid = int(parts[1])
    text = parts[2].strip()

    SUPPORT_MESSAGES.append({
        "id": f"MSG-{secrets.token_hex(3)}",
        "user_id": uid,
        "sender": "admin",
        "text": text,
        "time": time.strftime("%H:%M"),
    })
    await save_data()

    try:
        await message.bot.send_message(
            chat_id=uid,
            text=f"ð¬ <b>ÐÑÐ²ÐµÑ Ð¾Ñ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐ¸:</b>
{html.escape(text)}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await message.answer(f"â ÐÑÐ²ÐµÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{uid}</code>", parse_mode="HTML")


@dp.message(Command("code"))
async def cmd_code(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>/code USER_ID ÐÐÐ</code>", parse_mode="HTML")
        return

    uid = int(parts[1])
    code = parts[2].strip()
    for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
        if isinstance(rec, dict) and not rec.get("code"):
            rec["code"] = code
            break
    if LAST_ISSUED_ACCOUNT.get(uid):
        LAST_ISSUED_ACCOUNT[uid]["code"] = code
    await save_data()

    acc = LAST_ISSUED_ACCOUNT.get(uid) or {}
    phone = acc.get("phone", "")
    two_fa = acc.get("two_fa") or ""
    phone_line = f"
<b>ÐÐ¾Ð¼ÐµÑ:</b> <code>{phone}</code>" if phone else ""
    two_fa_line = f"
ð <b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""

    await message.bot.send_message(
        chat_id=uid,
        text=f"ð <b>ÐÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°:</b> <code>{code}</code>{phone_line}{two_fa_line}",
        parse_mode="HTML",
    )
    await message.answer(f"â ÐÐ¾Ð´ <code>{code}</code> Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ <code>{uid}</code>", parse_mode="HTML")


@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    uid = message.from_user.id
    stars_paid = int(sp.total_amount or 0)
    rub_add = round(stars_paid * RUB_PER_STAR, 2)
    new_bal = await add_rub_balance(uid, rub_add)
    await message.answer(
        f"â ÐÐ¿Ð»Ð°ÑÐµÐ½Ð¾ <b>{stars_paid} â­</b>! ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¾ <b>+{rub_add:.2f} â½</b>.
"
        f"Ð¢ÐµÐºÑÑÐ¸Ð¹ Ð±Ð°Ð»Ð°Ð½Ñ: <b>{new_bal:.2f} â½</b>.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await notify_owner(
        message.bot,
        f"â­ ÐÐ¿Ð»Ð°ÑÐ° Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ Ð¾Ñ <code>{uid}</code>: {stars_paid} â­ -> +{rub_add:.2f} â½ (Ð±Ð°Ð»Ð°Ð½Ñ: {new_bal:.2f} â½)",
    )


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        get_welcome_text(callback.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_rules")
async def cb_menu_rules(callback: CallbackQuery):
    await callback.message.edit_text(
        f"ð <b>ÐÑÐ°Ð²Ð¸Ð»Ð° Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½Ð°</b>

"
        "1. ÐÑÐ¾Ð²ÐµÑÑÐ¹ÑÐµ Ð°ÐºÐºÐ°ÑÐ½ÑÑ ÑÑÐ°Ð·Ñ Ð¿Ð¾ÑÐ»Ðµ Ð²ÑÐ´Ð°ÑÐ¸.
"
        "2. ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ Ð¸ TON Ð·Ð°ÑÐ¸ÑÐ»ÑÐµÑÑÑ Ð½Ð° ÐµÐ´Ð¸Ð½ÑÐ¹ Ð±Ð°Ð»Ð°Ð½Ñ Ð² â½.
"
        f"3. ÐÐ¾ Ð²ÑÐµÐ¼ Ð²Ð¾Ð¿ÑÐ¾ÑÐ°Ð¼ Ð¾Ð±ÑÐ°ÑÐ°Ð¹ÑÐµÑÑ Ð² Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÑ: @{SUPPORT_USERNAME}",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_panel")
async def cb_owner_panel(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    adm_list = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "Ð½ÐµÑ"
    lines = [
        "ð <b>ÐÐ°Ð½ÐµÐ»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°</b>",
        f"ÐÐ´Ð¼Ð¸Ð½Ñ: <code>{adm_list}</code>",
        f"ÐÑÐµÐ³Ð¾ Ð¿ÑÐ¾Ð´Ð°Ð½Ð¾: <b>{TOTAL_SOLD_COUNT} ÑÑ.</b>
",
        "<b>ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸:</b>",
    ]
    if not CATALOG:
        lines.append("â¢ ÐÐ¾Ð·Ð¸ÑÐ¸Ð¹ Ð¿Ð¾ÐºÐ° Ð½ÐµÑ â ÑÐ¾Ð·Ð´Ð°Ð¹ ÑÐµÑÐµÐ· Â«ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸ÑÂ».")
    else:
        for k, v in CATALOG.items():
            lines.append(
                f"â¢ <b>{v['title']}</b> (<code>{k}</code>): <b>{len(v.get('accounts', []))} ÑÑ.</b> â {v['price_rub']:.0f} â½"
            )
    lines.append("
<b>ÐÑÐ´Ð°ÑÐ° ÐºÐ¾Ð´Ð°:</b> <code>/code &lt;user_id&gt; &lt;ÐÐÐ&gt;</code>")
    lines.append("<b>ÐÑÐ²ÐµÑ Ð² Ð§Ð°Ñ-ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÑ:</b> <code>/reply &lt;user_id&gt; &lt;Ð¢ÐÐÐ¡Ð¢&gt;</code>")
    await callback.message.edit_text(
        "
".join(lines),
        parse_mode="HTML",
        reply_markup=owner_panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "staff_panel")
async def cb_staff_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    lines = [
        "ð  <b>ÐÐ´Ð¼Ð¸Ð½ÐºÐ°</b>",
        "ÐÐ¾ÑÑÑÐ¿Ð½Ð¾: Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ Ð±Ð°Ð»Ð°Ð½ÑÐ° Ð¿Ð¾ ID Ð¸ Ð·Ð°Ð»Ð¸Ð² Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð².

<b>ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸:</b>",
    ]
    if not CATALOG:
        lines.append("â¢ ÐÐ¾Ð·Ð¸ÑÐ¸Ð¹ Ð¿Ð¾ÐºÐ° Ð½ÐµÑ.")
    else:
        for k, v in CATALOG.items():
            lines.append(
                f"â¢ <b>{v['title']}</b>: <b>{len(v.get('accounts', []))} ÑÑ.</b> â {v['price_rub']:.0f} â½"
            )
    lines.append("
<b>ÐÑÐ²ÐµÑ Ð² Ð§Ð°Ñ-ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÑ:</b> <code>/reply &lt;user_id&gt; &lt;Ð¢ÐÐÐ¡Ð¢&gt;</code>")
    await callback.message.edit_text(
        "
".join(lines),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_new_cat")
async def cb_owner_new_cat(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_new_cat"
    await callback.message.edit_text(
        "ð <b>ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ</b>

"
        "Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐºÐ»ÑÑ|ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ|Ð¦ÐµÐ½Ð°_Ð²_ÑÑÐ±Ð»ÑÑ|ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ</code>
"
        "ÐÑÐ¸Ð¼ÐµÑ: <code>de_acc|DE +49|190|ÐÐºÐºÐ°ÑÐ½Ñ ÐÐµÑÐ¼Ð°Ð½Ð¸Ð¸</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_give_bal")
async def cb_owner_give_bal(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_give_bal"
    await callback.message.edit_text(
        "ð° <b>ÐÑÐ´Ð°ÑÐ° Ð±Ð°Ð»Ð°Ð½ÑÐ° (Ð² â½)</b>

"
        "Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>USER_ID Ð¡Ð£ÐÐÐ_Ð Ð£ÐÐÐÐ</code>
"
        "ÐÑÐ¸Ð¼ÐµÑ: <code>123456789 500</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_add_adm")
async def cb_owner_add_adm(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_add_adm"
    await callback.message.edit_text(
        "â <b>ÐÑÐ´Ð°ÑÐ° Ð°Ð´Ð¼Ð¸Ð½ÐºÐ¸</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_del_adm")
async def cb_owner_del_adm(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_del_adm"
    await callback.message.edit_text(
        "â <b>Ð¡Ð½ÑÑÐ¸Ðµ Ð°Ð´Ð¼Ð¸Ð½ÐºÐ¸</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "staff_check_bal")
async def cb_staff_check_bal(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "staff_check_bal"
    await callback.message.edit_text(
        "ð <b>ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("staff_add:"))
async def cb_staff_add(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"staff_add:{key}"
    await callback.message.edit_text(
        f"â <b>ÐÐ°Ð»Ð¸Ð² Ð² {item['title']}</b>

"
        "ÐÑÐ¿ÑÐ°Ð²Ñ Ð°ÐºÐºÐ°ÑÐ½ÑÑ (ÐºÐ°Ð¶Ð´ÑÐ¹ Ñ Ð½Ð¾Ð²Ð¾Ð¹ ÑÑÑÐ¾ÐºÐ¸):
"
        "<code>+12025550199:StringSession:2FA</code>
"
        "Ð¸Ð»Ð¸ <code>+12025550199:2FA</code> / <code>+12025550199</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    if not CATALOG:
        await callback.message.edit_text(
            "ð <b>ÐÐ°ÑÐ°Ð»Ð¾Ð³</b>

Ð¡ÐµÐ¹ÑÐ°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¹ Ð½ÐµÑ.",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "ð <b>ÐÐ°ÑÐ°Ð»Ð¾Ð³</b>

ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ:",
        parse_mode="HTML",
        reply_markup=catalog_items_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("item_view:"))
async def cb_item_view(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("ÐÐµÑ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸", show_alert=True)
        return
    text = (
        f"ð¦ <b>{item['title']}</b>

"
        f"{item.get('description', '')}

"
        f"ð° <b>Ð¦ÐµÐ½Ð°:</b> {item['price_rub']:.2f} â½
"
        f"ð <b>Ð Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸:</b> {len(item['accounts'])} ÑÑ."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=item_view_keyboard(key),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_acc:"))
async def cb_buy_acc(callback: CallbackQuery):
    global TOTAL_SOLD_COUNT
    uid = callback.from_user.id
    if not check_rate_limit(uid, "buy", 1.2):
        await callback.answer("ÐÐµ ÑÐ°Ðº Ð±ÑÑÑÑÐ¾", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("ÐÐµÑ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸", show_alert=True)
        return

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        await callback.answer(
            f"ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÑÐµÐ´ÑÑÐ² ({price:.2f} â½). ÐÐ¾Ð¿Ð¾Ð»Ð½Ð¸ Ð±Ð°Ð»Ð°Ð½Ñ!",
            show_alert=True,
        )
        return

    acc = item["accounts"].pop(0)
    if not await deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        await callback.answer("ÐÑÐ¸Ð±ÐºÐ° ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ñ", show_alert=True)
        return

    TOTAL_SOLD_COUNT += 1

    order_id = f"ORD-{secrets.token_hex(3).upper()}"
    phone = acc.get("phone", "")
    session_str, two_fa = _parse_account_secret(acc.get("secret", ""))

    rec = {
        "order_id": order_id,
        "title": item["title"],
        "phone": phone,
        "session_string": session_str,
        "two_fa": two_fa,
        "code": "",
    }
    LAST_ISSUED_ACCOUNT[uid] = rec
    PURCHASE_HISTORY.setdefault(uid, []).append(rec)

    RECENT_PURCHASES.insert(0, {
        "title": item["title"],
        "price": price,
        "time": time.strftime("%H:%M"),
    })

    await save_data()

    two_fa_line = f"
ð <b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""
    await callback.message.edit_text(
        f"â <b>ÐÐºÐºÐ°ÑÐ½Ñ Ð²ÑÐ´Ð°Ð½!</b>

"
        f"<b>ÐÐ°ÐºÐ°Ð·:</b> <code>{order_id}</code>
"
        f"<b>ÐÐ¾Ð·Ð¸ÑÐ¸Ñ:</b> {item['title']}
"
        f"<b>ÐÐ¾Ð¼ÐµÑ:</b> <code>{phone}</code>{two_fa_line}

"
        "ÐÐ°Ð¶Ð¼Ð¸ <b>Â«ÐÐ¾Ð»ÑÑÐ¸ÑÑ / Ð·Ð°Ð¿ÑÐ¾ÑÐ¸ÑÑ ÐºÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°Â»</b> Ð´Ð»Ñ Ð²ÑÐ¾Ð´Ð°.",
        parse_mode="HTML",
        reply_markup=issued_account_keyboard(),
    )
    await callback.answer("ÐÑÐ´Ð°Ð½Ð¾!")

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "Ð±ÐµÐ· ÑÐ·ÐµÑÐ½ÐµÐ¹Ð¼Ð°"
    await notify_owner(
        callback.bot,
        f"ð <b>ÐÐ¾ÐºÑÐ¿ÐºÐ°!</b>
"
        f"Ð®Ð·ÐµÑ: {uname} (<code>{uid}</code>)
"
        f"ÐÐ°ÐºÐ°Ð·: <code>{order_id}</code>
"
        f"ÐÐ¾Ð·Ð¸ÑÐ¸Ñ: {item['title']} â <b>{price:.2f} â½</b>
"
        f"ÐÐ¾Ð¼ÐµÑ: <code>{phone}</code>
"
        f"ÐÑÑÐ°ÑÐ¾Ðº: <b>{len(item['accounts'])} ÑÑ.</b>",
    )


@dp.callback_query(F.data == "req_code")
async def cb_req_code(callback: CallbackQuery):
    uid = callback.from_user.id
    if not check_rate_limit(uid, "code", 2.0):
        await callback.answer("ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ Ð¿Ð°ÑÑ ÑÐµÐºÑÐ½Ð´", show_alert=True)
        return
    acc = LAST_ISSUED_ACCOUNT.get(uid)
    if not acc:
        await callback.answer("ÐÐµÑ Ð²ÑÐ´Ð°Ð½Ð½Ð¾Ð³Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ°", show_alert=True)
        return

    code = await fetch_tg_login_code(acc.get("session_string", ""))
    if code:
        acc["code"] = code
        for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
            if isinstance(rec, dict) and rec.get("order_id") == acc.get("order_id"):
                rec["code"] = code
                break
        await save_data()
        await callback.message.answer(
            f"ð <b>ÐÐ¾Ð´ Ð²ÑÐ¾Ð´Ð° Ð´Ð»Ñ</b> <code>{acc['phone']}</code>: <code>{code}</code>",
            parse_mode="HTML",
        )
        await callback.answer("ÐÐ¾Ð´ Ð¿Ð¾Ð»ÑÑÐµÐ½!")
        return

    await callback.message.answer(
        f"â³ ÐÐ°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¿Ð¾ Ð½Ð¾Ð¼ÐµÑÑ <code>{acc['phone']}</code> Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½.",
        parse_mode="HTML",
    )
    await callback.answer("ÐÐ°Ð¿ÑÐ¾Ñ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½")
    await notify_owner(
        callback.bot,
        f"ð© ÐÐ°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¾Ñ <code>{uid}</code> Ð¿Ð¾ Ð½Ð¾Ð¼ÐµÑÑ <code>{acc['phone']}</code>.
"
        f"ÐÑÐ¿ÑÐ°Ð²Ñ: <code>/code {uid} ÐÐÐ</code>",
    )


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    uid = callback.from_user.id
    role = "ÐÐ»Ð°Ð´ÐµÐ»ÐµÑ" if is_owner(uid) else ("ÐÐ´Ð¼Ð¸Ð½" if is_admin(uid) else "ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»Ñ")
    cnt = len(PURCHASE_HISTORY.get(uid, []))
    await callback.message.edit_text(
        f"ð¤ <b>ÐÑÐ¾ÑÐ¸Ð»Ñ</b>

"
        f"Ð Ð¾Ð»Ñ: <b>{role}</b>
"
        f"ID: <code>{uid}</code>
"
        f"ÐÐ°Ð»Ð°Ð½Ñ: <b>{get_rub_balance(uid):.2f} â½</b>
"
        f"ÐÐ¾ÐºÑÐ¿Ð¾Ðº: <b>{cnt}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_deposit")
async def cb_menu_deposit(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.edit_text(
        f"ð³ <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° (Ð² â½)</b>

"
        f"Ð¢ÐµÐºÑÑÐ¸Ð¹ Ð±Ð°Ð»Ð°Ð½Ñ: <b>{get_rub_balance(uid):.2f} â½</b>
"
        f"ÐÑÑÑÑ: 1 â­ = {RUB_PER_STAR} â½ â¢ 1 TON = {RUB_PER_TON} â½",
        parse_mode="HTML",
        reply_markup=deposit_choice_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dep_method:"))
async def cb_dep_method(callback: CallbackQuery):
    m = callback.data.split(":", 1)[1]
    WAITING_INPUT[callback.from_user.id] = f"dep_amt:{m}"
    if m == "stars":
        await callback.message.edit_text(
            f"â­ <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ (Ð² â½)</b>

"
            f"ÐÑÑÑ: <b>1 â­ = {RUB_PER_STAR} â½</b>
"
            f"ÐÐ°Ð¿Ð¸ÑÐ¸ ÑÐµÐ»Ð¾Ðµ ÑÐ¸ÑÐ»Ð¾ Ð·Ð²ÑÐ·Ð´ Ð´Ð»Ñ Ð²ÑÑÑÐ°Ð²Ð»ÐµÐ½Ð¸Ñ ÑÑÑÑÐ°:",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"ð <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ TON (Ð² â½)</b>

"
            f"ÐÑÑÑ: <b>1 TON = {RUB_PER_TON} â½</b>
"
            f"ÐÐ¾ÑÐµÐ»ÑÐº: <code>{CRYPTO_WALLET}</code>

"
            f"ÐÐ°Ð¿Ð¸ÑÐ¸ ÑÑÐ¼Ð¼Ñ Ð² TON â Ð·Ð°ÑÐ²ÐºÐ° ÑÐ¹Ð´ÑÑ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÑ Ð½Ð° Ð¿ÑÐ¾Ð²ÐµÑÐºÑ Ð¿ÐµÑÐµÐ²Ð¾Ð´Ð°:",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
    await callback.answer()


@dp.message(F.text)
async def handle_text_inputs(message: Message):
    uid = message.from_user.id
    wait = WAITING_INPUT.get(uid)
    txt = (message.text or "").strip()
    if not wait:
        return

    if wait == "dep_amt:stars":
        if not txt.isdigit() or int(txt) <= 0:
            await message.answer("ÐÐ²ÐµÐ´Ð¸ ÑÐµÐ»Ð¾Ðµ ÑÐ¸ÑÐ»Ð¾ Ð·Ð²ÑÐ·Ð´ Ð±Ð¾Ð»ÑÑÐµ Ð½ÑÐ»Ñ.")
            return
        stars_cnt = int(txt)
        WAITING_INPUT.pop(uid, None)
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        await message.bot.send_invoice(
            chat_id=uid,
            title=f"ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° ({rub_preview:.2f} â½)",
            description=f"ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ {rub_preview:.2f} â½ Ð½Ð° Ð±Ð°Ð»Ð°Ð½Ñ Ð·Ð° {stars_cnt} â­",
            payload=f"rub_stars:{uid}:{stars_cnt}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars_cnt} â­", amount=stars_cnt)],
        )
        return

    if wait == "dep_amt:ton":
        try:
            ton_amt = float(txt.replace(",", "."))
        except ValueError:
            await message.answer("ÐÐ²ÐµÐ´Ð¸ ÑÑÐ¼Ð¼Ñ Ð² TON Ð±Ð¾Ð»ÑÑÐµ Ð½ÑÐ»Ñ.")
            return
        if ton_amt <= 0:
            await message.answer("Ð¡ÑÐ¼Ð¼Ð° Ð² TON Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ Ð±Ð¾Ð»ÑÑÐµ Ð½ÑÐ»Ñ.")
            return
        WAITING_INPUT.pop(uid, None)
        rub_est = round(ton_amt * RUB_PER_TON, 2)
        await message.answer(
            f"ð <b>ÐÐ°ÑÐ²ÐºÐ° Ð½Ð° Ð¿Ð¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ TON Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½Ð°</b>

"
            f"Ð¡ÑÐ¼Ð¼Ð°: <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} â½</b>)
"
            f"ÐÐ¾ÑÐµÐ»ÑÐº: <code>{CRYPTO_WALLET}</code>
"
            "ÐÐ°Ð»Ð°Ð½Ñ Ð±ÑÐ´ÐµÑ Ð·Ð°ÑÐ¸ÑÐ»ÐµÐ½ Ð¿Ð¾ÑÐ»Ðµ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ¸ Ð¿ÐµÑÐµÐ²Ð¾Ð´Ð°.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        await notify_owner(
            message.bot,
            f"ð <b>ÐÐ°ÑÐ²ÐºÐ° TON</b> Ð¾Ñ <code>{uid}</code>: <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} â½</b>)
"
            f"ÐÑÐ´Ð°ÑÑ ÑÐµÑÐµÐ· Ð¿Ð°Ð½ÐµÐ»Ñ: <code>{uid} {rub_est}</code>",
        )
        return

    if wait == "staff_check_bal" and is_admin(uid):
        if not txt.isdigit():
            await message.answer("ÐÑÐ¶ÐµÐ½ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ ID.")
            return
        WAITING_INPUT.pop(uid, None)
        target_id = int(txt)
        await message.answer(
            f"ð° ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{target_id}</code>: <b>{get_rub_balance(target_id):.2f} â½</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait.startswith("staff_add:") and is_admin(uid):
        key = wait.split(":", 1)[1]
        item = CATALOG.get(key)
        if not item:
            WAITING_INPUT.pop(uid, None)
            return
        added = 0
        for line in txt.splitlines():
            clean = line.strip()
            if not clean:
                continue
            parts = clean.split(":", 1)
            phone = parts[0].strip()
            secret = parts[1].strip() if len(parts) > 1 else ""
            item.setdefault("accounts", []).append({"phone": phone, "secret": secret})
            added += 1
        WAITING_INPUT.pop(uid, None)
        await save_data()
        await message.answer(
            f"â ÐÐ°Ð»Ð¸ÑÐ¾ <b>{added} ÑÑ.</b> Ð² <b>{item['title']}</b> (Ð²ÑÐµÐ³Ð¾: {len(item['accounts'])} ÑÑ.).",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if not is_owner(uid):
        return

    if wait == "owner_new_cat":
        parts = [p.strip() for p in txt.split("|", 3)]
        if len(parts) < 3:
            await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐºÐ»ÑÑ|ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ|Ð¦ÐµÐ½Ð°_Ð²_ÑÑÐ±Ð»ÑÑ|ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ</code>", parse_mode="HTML")
            return
        key, title, price_raw = parts[0], parts[1], parts[2]
        desc = parts[3] if len(parts) > 3 else ""
        try:
            price_rub = float(price_raw.replace(",", "."))
        except ValueError:
            await message.answer("Ð¦ÐµÐ½Ð° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ ÑÐ¸ÑÐ»Ð¾Ð¼.")
            return
        WAITING_INPUT.pop(uid, None)
        CATALOG[key] = {
            "title": title,
            "price_rub": price_rub,
            "description": desc,
            "accounts": CATALOG.get(key, {}).get("accounts", []),
        }
        await save_data()
        await message.answer(
            f"â ÐÐ¾Ð·Ð¸ÑÐ¸Ñ <b>{title}</b> (<code>{key}</code>) ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð° â {price_rub:.0f} â½.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait == "owner_give_bal":
        parts = txt.split()
        if len(parts) != 2 or not parts[0].isdigit():
            await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>USER_ID Ð¡Ð£ÐÐÐ_Ð Ð£ÐÐÐÐ</code>", parse_mode="HTML")
            return
        target_id = int(parts[0])
        try:
            rub_amt = float(parts[1].replace(",", "."))
        except ValueError:
            await message.answer("Ð¡ÑÐ¼Ð¼Ð° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ ÑÐ¸ÑÐ»Ð¾Ð¼.")
            return
        WAITING_INPUT.pop(uid, None)
        new_bal = await add_rub_balance(target_id, rub_amt)
        await message.answer(
            f"â ÐÑÐ´Ð°Ð½Ð¾ <b>{rub_amt:.2f} â½</b> Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{target_id}</code>. ÐÐ°Ð»Ð°Ð½Ñ: <b>{new_bal:.2f} â½</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait == "owner_add_adm":
        if not txt.isdigit():
            await message.answer("ÐÑÐ¶ÐµÐ½ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        ADMIN_IDS.add(adm_id)
        await save_data()
        await message.answer(
            f"â ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{adm_id}</code> Ð½Ð°Ð·Ð½Ð°ÑÐµÐ½ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð¼.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait == "owner_del_adm":
        if not txt.isdigit():
            await message.answer("ÐÑÐ¶ÐµÐ½ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        ADMIN_IDS.discard(adm_id)
        await save_data()
        await message.answer(
            f"â ÐÐ´Ð¼Ð¸Ð½ÐºÐ° Ñ <code>{adm_id}</code> ÑÐ½ÑÑÐ°.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )


def _cors_response(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=CORS_HEADERS)


async def api_options(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def api_state(request: web.Request) -> web.Response:
    uid_raw = request.query.get("user_id", "").strip()
    if not uid_raw.isdigit():
        return _cors_response({"ok": False, "error": "Invalid user_id"}, status=400)
    return _cors_response(build_state_payload(int(uid_raw)))


async def api_buy(request: web.Request) -> web.Response:
    global TOTAL_SOLD_COUNT
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    item_key = str(data.get("item_key") or "").strip()
    if uid <= 0 or not item_key:
        return _cors_response({"ok": False, "error": "Missing user_id or item_key"}, status=400)

    if not check_rate_limit(uid, "api_buy", 1.2):
        return _cors_response({"ok": False, "error": "Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ ÑÐ°ÑÑÑÐµ Ð·Ð°Ð¿ÑÐ¾ÑÑ"}, status=429)

    item = CATALOG.get(item_key)
    if not item or not item.get("accounts"):
        return _cors_response({"ok": False, "error": "ÐÐµÑ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸"}, status=400)

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        return _cors_response({"ok": False, "error": "ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÑÐµÐ´ÑÑÐ²"}, status=400)

    acc = item["accounts"].pop(0)
    if not await deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        return _cors_response({"ok": False, "error": "ÐÑÐ¸Ð±ÐºÐ° ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ñ"}, status=400)

    TOTAL_SOLD_COUNT += 1

    order_id = f"ORD-{secrets.token_hex(3).upper()}"
    phone = acc.get("phone", "")
    session_str, two_fa = _parse_account_secret(acc.get("secret", ""))
    rec = {
        "order_id": order_id,
        "title": item["title"],
        "phone": phone,
        "session_string": session_str,
        "two_fa": two_fa,
        "code": "",
    }
    LAST_ISSUED_ACCOUNT[uid] = rec
    PURCHASE_HISTORY.setdefault(uid, []).append(rec)

    RECENT_PURCHASES.insert(0, {
        "title": item["title"],
        "price": price,
        "time": time.strftime("%H:%M"),
    })

    await save_data()

    if bot:
        await notify_owner(bot, f"ð MiniApp Ð¿Ð¾ÐºÑÐ¿ÐºÐ° Ð¾Ñ <code>{uid}</code> â¢ <code>{order_id}</code> â¢ <code>{phone}</code>")
    return _cors_response({
        "ok": True,
        "order_id": order_id,
        "phone": phone,
        "two_fa": two_fa,
        "code": "",
        "balance_rub": get_rub_balance(uid),
    })


async def api_deposit(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "ÐÐµÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¹ JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    method = str(data.get("method") or "").strip().lower()
    send_to_bot = bool(data.get("send_to_bot", True))
    try:
        amount = float(str(data.get("amount") or "0").replace(",", "."))
    except (ValueError, TypeError):
        amount = 0.0

    if uid <= 0 or amount <= 0 or method not in ("stars", "ton"):
        return _cors_response({"ok": False, "error": "ÐÑÐ¾Ð²ÐµÑÑÑÐµ ÑÑÐ¼Ð¼Ñ Ð¸ ÑÐ¿Ð¾ÑÐ¾Ð± Ð¾Ð¿Ð»Ð°ÑÑ"}, status=400)

    if not check_rate_limit(uid, "api_dep", 1.0):
        return _cors_response({"ok": False, "error": "ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ÑÐµ ÑÐµÐºÑÐ½Ð´Ñ Ð¸ Ð¿Ð¾Ð¿ÑÐ¾Ð±ÑÐ¹ÑÐµ ÑÐ½Ð¾Ð²Ð°"}, status=429)

    if method == "stars":
        stars_cnt = max(1, int(round(amount)))
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        if not bot:
            return _cors_response({"ok": False, "error": "BOT_TOKEN Ð½Ðµ Ð½Ð°ÑÑÑÐ¾ÐµÐ½ Ð½Ð° ÑÐµÑÐ²ÐµÑÐµ"}, status=500)
        try:
            if send_to_bot:
                await bot.send_invoice(
                    chat_id=uid,
                    title=f"ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° ({rub_preview:.2f} â½)",
                    description=f"ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ {rub_preview:.2f} â½ Ð½Ð° Ð±Ð°Ð»Ð°Ð½Ñ Ð·Ð° {stars_cnt} â­",
                    payload=f"rub_stars:{uid}:{stars_cnt}",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"{stars_cnt} â­", amount=stars_cnt)],
                )
                return _cors_response({
                    "ok": True,
                    "pending_payment": True,
                    "sent_to_bot": True,
                    "balance_rub": get_rub_balance(uid),
                })

            invoice_link = await bot.create_invoice_link(
                title=f"ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° ({rub_preview:.2f} â½)",
                description=f"ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ {rub_preview:.2f} â½ Ð·Ð° {stars_cnt} â­",
                payload=f"rub_stars:{uid}:{stars_cnt}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{stars_cnt} â­", amount=stars_cnt)],
            )
            return _cors_response({
                "ok": True,
                "pending_payment": True,
                "invoice_link": invoice_link,
                "balance_rub": get_rub_balance(uid),
            })
        except Exception as e:
            return _cors_response({"ok": False, "error": f"Telegram Stars: {e}"}, status=400)

    rub_est = round(amount * RUB_PER_TON, 2)
    if bot:
        await notify_owner(
            bot,
            f"ð <b>ÐÐ°ÑÐ²ÐºÐ° TON Ð¸Ð· DarkHub</b> Ð¾Ñ <code>{uid}</code>: <b>{amount} TON</b> (~<b>{rub_est:.2f} â½</b>)
"
            f"ÐÑÐ´Ð°ÑÑ: <code>{uid} {rub_est}</code>",
        )
    return _cors_response({
        "ok": True,
        "pending_payment": True,
        "wallet": CRYPTO_WALLET,
        "estimated_rub": rub_est,
        "balance_rub": get_rub_balance(uid),
    })


async def api_code(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    order_id = str(data.get("order_id") or "").strip()
    if uid <= 0:
        return _cors_response({"ok": False, "error": "Invalid user_id"}, status=400)

    if not check_rate_limit(uid, "api_code", 2.0):
        return _cors_response({"ok": False, "error": "Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ ÑÐ°ÑÑÑÐµ Ð·Ð°Ð¿ÑÐ¾ÑÑ ÐºÐ¾Ð´Ð°"}, status=429)

    target = None
    for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
        if isinstance(rec, dict) and (not order_id or rec.get("order_id") == order_id):
            target = rec
            break

    if not target:
        target = LAST_ISSUED_ACCOUNT.get(uid)

    if not target:
        return _cors_response({"ok": False, "error": "ÐÐ°ÐºÐ°Ð· Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½"}, status=404)

    if target.get("code"):
        return _cors_response({"ok": True, "code": target["code"]})

    fetched = await fetch_tg_login_code(target.get("session_string", ""))
    if fetched:
        target["code"] = fetched
        await save_data()
        return _cors_response({"ok": True, "code": fetched})

    if bot:
        await notify_owner(
            bot,
            f"ð© MiniApp Ð·Ð°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¾Ñ <code>{uid}</code> (<code>{target.get('phone')}</code>).
"
            f"ÐÑÐ¿ÑÐ°Ð²Ñ: <code>/code {uid} ÐÐÐ</code>",
        )
    return _cors_response({"ok": False, "error": "ÐÐ°Ð¿ÑÐ¾Ñ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½, ÐºÐ¾Ð´ ÑÐºÐ¾ÑÐ¾ Ð¿ÑÐ¸Ð´ÑÑ"})


async def api_chat_send(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    text = str(data.get("text") or "").strip()

    if uid <= 0 or not text:
        return _cors_response({"ok": False, "error": "ÐÐ²ÐµÐ´Ð¸ÑÐµ ÑÐµÐºÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ"}, status=400)

    msg_obj = {
        "id": f"MSG-{secrets.token_hex(3)}",
        "user_id": uid,
        "sender": "user",
        "text": text,
        "time": time.strftime("%H:%M"),
    }
    SUPPORT_MESSAGES.append(msg_obj)
    await save_data()

    if bot:
        await notify_owner(
            bot,
            f"ð¬ <b>Ð¡Ð¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ð² ÑÐ°Ñ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐ¸!</b>
"
            f"ÐÑ: <code>{uid}</code>
"
            f"Ð¢ÐµÐºÑÑ: {html.escape(text)}

"
            f"ÐÑÐ²ÐµÑÐ¸ÑÑ: <code>/reply {uid} Ð¢ÐÐÐ¡Ð¢</code>",
        )

    user_chat = [m for m in SUPPORT_MESSAGES if m.get("user_id") == uid]
    return _cors_response({"ok": True, "chat": user_chat})


async def api_review_add(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    rating = int(data.get("rating") or 5)
    text = str(data.get("text") or "").strip()
    username = str(data.get("username") or "Ð°Ð½Ð¾Ð½Ð¸Ð¼").strip()

    if uid <= 0:
        return _cors_response({"ok": False, "error": "Invalid user_id"}, status=400)

    stars_str = "â" * max(1, min(5, rating))
    rev_obj = {
        "id": f"REV-{secrets.token_hex(3)}",
        "user_id": uid,
        "username": f"@{username}" if username and not username.startswith("@") else username,
        "rating": stars_str,
        "text": text or "Ð±ÐµÐ· ÐºÐ¾Ð¼Ð¼ÐµÐ½ÑÐ°ÑÐ¸Ñ",
        "time": time.strftime("%d.%m %H:%M"),
    }

    REVIEWS_LIST.insert(0, rev_obj)
    await save_data()

    if bot:
        await notify_owner(
            bot,
            f"â­ <b>ÐÐ¾Ð²ÑÐ¹ Ð¾ÑÐ·ÑÐ² Ð² MiniApp!</b>
"
            f"ÐÑ: {rev_obj['username']} (<code>{uid}</code>)
"
            f"ÐÑÐµÐ½ÐºÐ°: {stars_str}
"
            f"ÐÑÐ·ÑÐ²: {html.escape(rev_obj['text'])}",
        )