import asyncio
import hashlib
import hmac
import html
import json
import os
import random
import re
from datetime import datetime, timezone
import secrets
import time
from urllib.parse import parse_qsl
import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    WebAppInfo,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CRYPTO_WALLET = os.getenv(
    "CRYPTO_WALLET",
    "UQBKmNK9ynwmMR_ORKA7Ii2TunYn8RpWsVDEZHOTOZ89ajux",
)
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SUPPORT_USERNAME = "gerulev"
OWNER_ID = int(os.getenv("OWNER_ID", "622034883") or "622034883")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
WEBAPP_VER = os.getenv("WEBAPP_VER", "1").strip()
REVIEWS_CHANNEL_URL = "https://t.me/+7N5DvqV20UM3Nzll"

# ÐÐ°ÑÑÑÐ¾Ð¹ÐºÐ° Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð¾Ð³Ð¾ ÐºÐ°Ð½Ð°Ð»Ð° (ÐÐ)
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "https://t.me/+WFd6USOIgnYyZjFl").strip()

RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6") or "1.6")
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "115.0") or "115.0")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
MIN_TON = float(os.getenv("MIN_TON", "0.1") or "0.1")
PRICE_SPLIT = float(os.getenv("PRICE_SPLIT", "150") or "150")
GUARANTEE_HOURS = int(os.getenv("GUARANTEE_HOURS", "0") or "0")

CATALOG: dict[str, dict] = {}
ADMIN_IDS: set[int] = set()
WAITING_INPUT: dict[int, str] = {}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[dict]] = {}
USER_RUB_BALANCE: dict[int, float] = {}
LAST_API_ACTION: dict[tuple[int, str], float] = {}
TOTAL_SOLD_COUNT: int = 0
RECENT_SALES: list[dict] = []
REVIEWS: list[dict] = []
SUPPORT: dict[int, list[dict]] = {}
PENDING_TON: dict[str, dict] = {}
PROCESSED_TX: list[str] = []
LAST_SPIN: dict[int, int] = {}
ADMIN_LOG: list[dict] = []
KNOWN_USERS: set[int] = set()
USER_NAMES: dict[int, str] = {}
TX_LOG: dict[int, list[dict]] = {}
DEPOSITED: dict[int, float] = {}
REF_OF: dict[int, int] = {}
REF_PAID: list[int] = []
REF_EARNED: dict[int, float] = {}
PROMOS: dict[str, dict] = {}
BCAST_PENDING: dict[int, str] = {}
BOT_USERNAME = ""

REF_BONUS = float(os.getenv("REF_BONUS", "1") or "1")
REF_TRIGGER = os.getenv("REF_TRIGGER", "purchase")
ROULETTE_PRIZES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
ROULETTE_WEIGHTS = [28, 20, 15, 11, 8, 6, 5, 4, 2, 1]
MSK_OFFSET = 3 * 3600
_rng = random.SystemRandom()

DB_POOL: asyncpg.Pool | None = None
_SAVE_LOCK = asyncio.Lock()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Init-Data",
}

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


def _resolve_channel_chat_id(ch: str) -> str | int | None:
    s = (ch or "").strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{6,}", s):
        return int(s)
    if "t.me/" in s:
        tail = s.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if not tail or tail.startswith("+") or tail.startswith("joinchat/"):
            return None
        if "/" in tail:
            tail = tail.split("/", 1)[0]
        return "@" + tail.lstrip("@")
    if s.startswith("@"):
        return s
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", s):
        return "@" + s
    return None


async def check_user_subscription(user_id: int) -> bool:
    """ÐÑÐ¾Ð²ÐµÑÐºÐ° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð½Ð° Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐ¹ ÐºÐ°Ð½Ð°Ð»."""
    if not REQUIRED_CHANNEL:
        return True
    if not bot:
        return True
    target = _resolve_channel_chat_id(REQUIRED_CHANNEL)
    if target is None:
        return True
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=user_id)
        status = getattr(member, "status", "")
        if status in ("creator", "administrator", "member"):
            return True
        if status == "restricted" and getattr(member, "is_member", False):
            return True
        return False
    except Exception:
        return True


def sub_wall_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if REQUIRED_CHANNEL:
        url = REQUIRED_CHANNEL
        if not url.startswith("http"):
            clean = url.lstrip("@")
            url = f"https://t.me/{clean}"
        rows.append([InlineKeyboardButton(text="ð¢ ÐÐ¾Ð´Ð¿Ð¸ÑÐ°ÑÑÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð»", url=url)])
    rows.append([InlineKeyboardButton(text="ð Ð¯ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°Ð»ÑÑ / ÐÑÐ¾Ð²ÐµÑÐ¸ÑÑ", callback_data="check_sub_cb")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ensure_sub_message(message: Message) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    ok = await check_user_subscription(message.from_user.id)
    if ok:
        return True
    await message.answer(
        "ð <b>ÐÐ¾ÑÑÑÐ¿ Ð¾Ð³ÑÐ°Ð½Ð¸ÑÐµÐ½</b>

"
        "Ð§ÑÐ¾Ð±Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÑÑÑ Ð±Ð¾ÑÐ¾Ð¼ Ð¸ Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½Ð¾Ð¼ DarkHub, ÑÐ½Ð°ÑÐ°Ð»Ð° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ¸ÑÐµÑÑ Ð½Ð° Ð½Ð°Ñ Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐ¹ ÐºÐ°Ð½Ð°Ð», Ð·Ð°ÑÐµÐ¼ Ð½Ð°Ð¶Ð¼Ð¸ÑÐµ ÐºÐ½Ð¾Ð¿ÐºÑ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ¸.",
        parse_mode="HTML",
        reply_markup=sub_wall_keyboard(),
    )
    return False


async def ensure_sub_callback(callback: CallbackQuery) -> bool:
    if callback.data in ("check_sub_cb",):
        return True
    if not REQUIRED_CHANNEL:
        return True
    ok = await check_user_subscription(callback.from_user.id)
    if ok:
        return True
    await callback.answer("Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ¸ÑÐµÑÑ Ð½Ð° Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐ¹ ÐºÐ°Ð½Ð°Ð»!", show_alert=True)
    try:
        await callback.message.answer(
            "ð <b>ÐÐ¾ÑÑÑÐ¿ Ð¾Ð³ÑÐ°Ð½Ð¸ÑÐµÐ½</b>

"
            "ÐÐ»Ñ Ð¿ÑÐ¾Ð´Ð¾Ð»Ð¶ÐµÐ½Ð¸Ñ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ¸ÑÐµÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð» Ð¸ Ð½Ð°Ð¶Ð¼Ð¸ÑÐµ Â«Ð¯ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°Ð»ÑÑ / ÐÑÐ¾Ð²ÐµÑÐ¸ÑÑÂ».",
            parse_mode="HTML",
            reply_markup=sub_wall_keyboard(),
        )
    except Exception:
        pass
    return False


# ÐÐ
async def init_db() -> None:
    global DB_POOL
    if not DATABASE_URL:
        print("ÐÐÐÐÐÐÐÐ: DATABASE_URL Ð½Ðµ Ð·Ð°Ð´Ð°Ð½! ÐÐ°Ð½Ð½ÑÐµ Ð½Ðµ Ð±ÑÐ´ÑÑ ÑÐ¾ÑÑÐ°Ð½ÑÑÑÑÑ Ð² PostgreSQL.")
        return
    DB_POOL = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY,
                payload JSONB NOT NULL
            )
        """)


async def load_data() -> None:
    global CATALOG, ADMIN_IDS, PURCHASE_HISTORY, USER_RUB_BALANCE, TOTAL_SOLD_COUNT
    global RECENT_SALES, REVIEWS, SUPPORT, PENDING_TON, PROCESSED_TX, LAST_SPIN, ADMIN_LOG
    global KNOWN_USERS, USER_NAMES, TX_LOG, DEPOSITED, REF_OF, REF_PAID, REF_EARNED, PROMOS, REQUIRED_CHANNEL
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
            PURCHASE_HISTORY = {int(k): v for k, v in data.get("purchase_history", {}).items()}
            USER_RUB_BALANCE = {int(k): float(v) for k, v in data.get("user_balances", {}).items()}
            TOTAL_SOLD_COUNT = int(data.get("total_sold_count", 0))
            RECENT_SALES = list(data.get("recent_sales", []))
            REVIEWS = list(data.get("reviews", []))
            SUPPORT = {int(k): v for k, v in data.get("support", {}).items()}
            PENDING_TON = dict(data.get("pending_ton", {}))
            PROCESSED_TX = list(data.get("processed_tx", []))
            LAST_SPIN = {int(k): int(v) for k, v in data.get("last_spin", {}).items()}
            ADMIN_LOG = list(data.get("admin_log", []))
            USER_NAMES = {int(k): v for k, v in data.get("user_names", {}).items()}
            TX_LOG = {int(k): v for k, v in data.get("tx_log", {}).items()}
            DEPOSITED = {int(k): float(v) for k, v in data.get("deposited", {}).items()}
            REF_OF = {int(k): int(v) for k, v in data.get("ref_of", {}).items()}
            REF_PAID = [int(x) for x in data.get("ref_paid", [])]
            REF_EARNED = {int(k): float(v) for k, v in data.get("ref_earned", {}).items()}
            PROMOS = dict(data.get("promos", {}))
            if "required_channel" in data and data["required_channel"] is not None:
                REQUIRED_CHANNEL = str(data["required_channel"]).strip()
            KNOWN_USERS = set(int(x) for x in data.get("known_users", [])) | set(PURCHASE_HISTORY) | set(USER_RUB_BALANCE) | set(SUPPORT)
            ensure_account_ids()
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
        "recent_sales": RECENT_SALES,
        "reviews": REVIEWS,
        "support": SUPPORT,
        "pending_ton": PENDING_TON,
        "processed_tx": PROCESSED_TX[-1000:],
        "last_spin": LAST_SPIN,
        "admin_log": ADMIN_LOG[-300:],
        "known_users": list(KNOWN_USERS),
        "user_names": USER_NAMES,
        "tx_log": TX_LOG,
        "deposited": DEPOSITED,
        "ref_of": REF_OF,
        "ref_paid": REF_PAID,
        "ref_earned": REF_EARNED,
        "promos": PROMOS,
        "required_channel": REQUIRED_CHANNEL,
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


# Ð£ÑÐ¸Ð»Ð¸ÑÑ
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def check_rate_limit(user_id: int, action: str, min_gap_sec: float = 1.5) -> bool:
    now = time.monotonic()
    key = (user_id, action)
    if now - LAST_API_ACTION.get(key, 0.0) < min_gap_sec:
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


async def notify_owner(bot_instance: Bot, text: str) -> None:
    if not OWNER_ID:
        return
    try:
        await bot_instance.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
    except Exception:
        pass


async def notify_staff(bot_instance: Bot, text: str) -> None:
    for sid in {OWNER_ID, *ADMIN_IDS}:
        try:
            await bot_instance.send_message(chat_id=sid, text=text, parse_mode="HTML")
        except Exception:
            pass


def tx_add(uid: int, kind: str, amount: float, note: str = "") -> None:
    TX_LOG.setdefault(uid, []).append({
        "ts": int(time.time()),
        "kind": kind,
        "amount": round(float(amount), 2),
        "note": note,
    })
    del TX_LOG[uid][:-100]
    if kind.startswith("dep"):
        DEPOSITED[uid] = round(DEPOSITED.get(uid, 0.0) + float(amount), 2)


async def ref_try_pay(friend: int, event: str) -> None:
    inviter = REF_OF.get(friend)
    if not inviter or friend in REF_PAID or REF_TRIGGER != event:
        return
    REF_PAID.append(friend)
    REF_EARNED[inviter] = round(REF_EARNED.get(inviter, 0.0) + REF_BONUS, 2)
    tx_add(inviter, "ref", REF_BONUS, f"Ð´ÑÑÐ³ {USER_NAMES.get(friend, friend)}")
    await add_rub_balance(inviter, REF_BONUS)
    if bot:
        try:
            await bot.send_message(
                inviter,
                f"ð <b>+{REF_BONUS:.0f} â½</b> Ð½Ð° Ð±Ð°Ð»Ð°Ð½Ñ: ÑÐ²Ð¾Ð¹ Ð´ÑÑÐ³ {html.escape(str(USER_NAMES.get(friend, '')))} {'ÐºÑÐ¿Ð¸Ð» Ð°ÐºÐºÐ°ÑÐ½Ñ' if event == 'purchase' else 'Ð·Ð°ÑÑÐ» Ð¿Ð¾ ÑÐ²Ð¾ÐµÐ¹ ÑÑÑÐ»ÐºÐµ'}!",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def register_user(uid: int, name: str = "", ref: int | None = None) -> None:
    new = uid not in KNOWN_USERS
    KNOWN_USERS.add(uid)
    if name:
        USER_NAMES[uid] = name
    if new and ref and ref != uid and uid not in REF_OF and ref in KNOWN_USERS:
        REF_OF[uid] = ref
        await ref_try_pay(uid, "join")
    if new:
        await save_data()


def referral_payload(uid: int) -> dict:
    friends = [f for f, i in REF_OF.items() if i == uid]
    return {
        "link": f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else "",
        "invited": len(friends),
        "paid": len([f for f in friends if f in REF_PAID]),
        "earned": REF_EARNED.get(uid, 0.0),
        "bonus": REF_BONUS,
        "trigger": REF_TRIGGER,
    }


def leaders_payload(uid: int) -> dict:
    buyers = []
    for u, recs in PURCHASE_HISTORY.items():
        rr = [r for r in recs if isinstance(r, dict)]
        if rr:
            buyers.append({
                "uid": u,
                "name": USER_NAMES.get(u, "ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»Ñ"),
                "buys": len(rr),
                "spent": round(sum(float(r.get("price_rub") or 0) for r in rr), 2),
            })
    buyers.sort(key=lambda x: (-x["buys"], -x["spent"]))
    donors = sorted(
        ({"uid": u, "name": USER_NAMES.get(u, "ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»Ñ"), "dep": v} for u, v in DEPOSITED.items() if v > 0),
        key=lambda x: -x["dep"],
    )

    def pack(lst: list[dict]) -> list[dict]:
        return [{**{k: v for k, v in x.items() if k != "uid"}, "me": x["uid"] == uid} for x in lst[:10]]

    return {"buyers": pack(buyers), "donors": pack(donors)}


def all_user_ids() -> list[int]:
    return sorted(KNOWN_USERS | set(USER_RUB_BALANCE) | set(PURCHASE_HISTORY) | set(SUPPORT))


def stats_text() -> str:
    from collections import Counter
    now = time.time()
    recs = [r for v in PURCHASE_HISTORY.values() for r in v if isinstance(r, dict)]

    def sales(since: float) -> tuple[int, float]:
        rr = [r for r in recs if r.get("ts", 0) >= since]
        return len(rr), sum(float(r.get("price_rub") or 0) for r in rr)

    def deps(since: float) -> float:
        return sum(
            e["amount"]
            for v in TX_LOG.values()
            for e in v
            if e["kind"].startswith("dep") and e["ts"] >= since
        )

    lines = ["ð <b>Ð¡ÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ°</b>", "â"]
    for title, secs in (("24 ÑÐ°ÑÐ°", 86400), ("7 Ð´Ð½ÐµÐ¹", 7 * 86400), ("30 Ð´Ð½ÐµÐ¹", 30 * 86400)):
        n, rev = sales(now - secs)
        lines.append(f"<b>{title}</b>: Ð¿ÑÐ¾Ð´Ð°Ð¶ {n}, Ð²ÑÑÑÑÐºÐ° {rev:.0f} â½, Ð¿Ð¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ð¹ {deps(now - secs):.0f} â½")
    n_all, rev_all = len(recs), sum(float(r.get("price_rub") or 0) for r in recs)
    lines.append(f"<b>ÐÑÐµÐ³Ð¾</b>: Ð¿ÑÐ¾Ð´Ð°Ð¶ {n_all}, Ð²ÑÑÑÑÐºÐ° {rev_all:.0f} â½, Ð¿Ð¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ð¹ {sum(DEPOSITED.values()):.0f} â½")
    stock = sum(len(v.get("accounts", [])) for v in CATALOG.values())
    stock_val = sum(
        float(a.get("price_rub") or v["price_rub"])
        for v in CATALOG.values()
        for a in v.get("accounts", [])
    )
    bal = sum(b for u, b in USER_RUB_BALANCE.items() if u != OWNER_ID)
    lines += [
        "",
        f"ð¥ ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»ÐµÐ¹: <b>{len(KNOWN_USERS)}</b> (Ð¿Ð¾ÐºÑÐ¿Ð°Ð»Ð¸: <b>{sum(1 for v in PURCHASE_HISTORY.values() if v)}</b>)",
        f"ð³ ÐÐ°Ð»Ð°Ð½Ñ Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»ÐµÐ¹: <b>{bal:.0f} â½</b>",
        f"ð¦ ÐÐ° ÑÐºÐ»Ð°Ð´Ðµ: <b>{stock}</b> ÑÑ. Ð½Ð° <b>{stock_val:.0f} â½</b>",
    ]
    top_c = Counter(country_info(r.get("phone", ""))["name"] for r in recs).most_common(5)
    if top_c:
        lines += ["", "ð <b>Ð¢Ð¾Ð¿ ÑÑÑÐ°Ð½ Ð¿Ð¾ Ð¿ÑÐ¾Ð´Ð°Ð¶Ð°Ð¼</b>", ", ".join(f"{n}x{c}" for n, c in top_c)]
    lb = leaders_payload(0)["buyers"][:3]
    if lb:
        lines += ["", "ð <b>Ð¢Ð¾Ð¿ Ð¿Ð¾ÐºÑÐ¿Ð°ÑÐµÐ»ÐµÐ¹</b>"] + [
            f"{i + 1}. {html.escape(x['name'])} â {x['buys']} ÑÑ. ({x['spent']:.0f} â½)"
            for i, x in enumerate(lb)
        ]
    lines += ["", f"ð¤ Ð ÐµÑÐµÑÐ°Ð»Ð¾Ð²: <b>{len(REF_OF)}</b> (Ð²ÑÐ¿Ð»Ð°ÑÐµÐ½Ð¾ {sum(REF_EARNED.values()):.0f} â½)"]
    return "
".join(lines)


async def _run_broadcast(bot_instance: Bot, owner_id: int, text: str) -> None:
    ok = 0
    fail = 0
    for u in all_user_ids():
        try:
            await bot_instance.send_message(u, text, parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)
    await bot_instance.send_message(
        owner_id,
        f"ð£ <b>Ð Ð°ÑÑÑÐ»ÐºÐ° Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð°</b>
â ÐÐ¾ÑÑÐ°Ð²Ð»ÐµÐ½Ð¾: {ok}
â ÐÐµ Ð´Ð¾ÑÑÐ°Ð²Ð»ÐµÐ½Ð¾: {fail}",
        parse_mode="HTML",
    )


def who(user) -> str:
    if not user:
        return "?"
    return f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "first_name", None) or str(user.id))


async def log_action(bot_instance: Bot | None, actor_id: int, actor_name: str, text: str) -> None:
    if is_owner(actor_id):
        return
    ADMIN_LOG.append({"ts": int(time.time()), "uid": actor_id, "name": actor_name, "text": text})
    del ADMIN_LOG[:-300]
    await save_data()
    if bot_instance:
        await notify_owner(
            bot_instance,
            f"ð ï¸ <b>ÐÐµÐ¹ÑÑÐ²Ð¸Ðµ Ð°Ð´Ð¼Ð¸Ð½Ð°</b>
{html.escape(actor_name)} (<code>{actor_id}</code>)
{text}",
        )


def verify_init_data(init_data: str, max_age: int = 3 * 86400) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        check = "
".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
        user["_sp"] = pairs.get("start_param", "")
        return user if user.get("id") else None
    except Exception:
        return None


def display_name(user: dict) -> str:
    if user.get("username"):
        return f"@{user['username']}"
    return (user.get("first_name") or "ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»Ñ")[:24]


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
            session_str, two_fa = left.strip(), right.strip()
    else:
        s = raw_secret.strip()
        if len(s) > 40:
            session_str = s
        else:
            two_fa = s
    return session_str, two_fa


async def fetch_tg_login_code(session_str: str, max_age: int = 180) -> str | None:
    if not TG_API_ID or not TG_API_HASH or not session_str:
        return None
    client = TelegramClient(StringSession(session_str), TG_API_ID, TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        messages = await client.get_messages(777000, limit=5)
        for msg in messages:
            if msg.date and (datetime.now(timezone.utc) - msg.date).total_seconds() > max_age:
                continue
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


COUNTRIES = {
    "1": ("us", "Ð¡Ð¨Ð"), "7": ("ru", "Ð Ð¾ÑÑÐ¸Ñ"), "20": ("eg", "ÐÐ³Ð¸Ð¿ÐµÑ"), "27": ("za", "Ð®ÐÐ "),
    "30": ("gr", "ÐÑÐµÑÐ¸Ñ"), "31": ("nl", "ÐÐ¸Ð´ÐµÑÐ»Ð°Ð½Ð´Ñ"), "32": ("be", "ÐÐµÐ»ÑÐ³Ð¸Ñ"), "33": ("fr", "Ð¤ÑÐ°Ð½ÑÐ¸Ñ"),
    "34": ("es", "ÐÑÐ¿Ð°Ð½Ð¸Ñ"), "36": ("hu", "ÐÐµÐ½Ð³ÑÐ¸Ñ"), "39": ("it", "ÐÑÐ°Ð»Ð¸Ñ"), "40": ("ro", "Ð ÑÐ¼ÑÐ½Ð¸Ñ"),
    "41": ("ch", "Ð¨Ð²ÐµÐ¹ÑÐ°ÑÐ¸Ñ"), "43": ("at", "ÐÐ²ÑÑÑÐ¸Ñ"), "44": ("gb", "ÐÐµÐ»Ð¸ÐºÐ¾Ð±ÑÐ¸ÑÐ°Ð½Ð¸Ñ"), "45": ("dk", "ÐÐ°Ð½Ð¸Ñ"),
    "46": ("se", "Ð¨Ð²ÐµÑÐ¸Ñ"), "47": ("no", "ÐÐ¾ÑÐ²ÐµÐ³Ð¸Ñ"), "48": ("pl", "ÐÐ¾Ð»ÑÑÐ°"), "49": ("de", "ÐÐµÑÐ¼Ð°Ð½Ð¸Ñ"),
    "51": ("pe", "ÐÐµÑÑ"), "52": ("mx", "ÐÐµÐºÑÐ¸ÐºÐ°"), "54": ("ar", "ÐÑÐ³ÐµÐ½ÑÐ¸Ð½Ð°"), "55": ("br", "ÐÑÐ°Ð·Ð¸Ð»Ð¸Ñ"),
    "56": ("cl", "Ð§Ð¸Ð»Ð¸"), "57": ("co", "ÐÐ¾Ð»ÑÐ¼Ð±Ð¸Ñ"), "58": ("ve", "ÐÐµÐ½ÐµÑÑÑÐ»Ð°"), "60": ("my", "ÐÐ°Ð»Ð°Ð¹Ð·Ð¸Ñ"),
    "61": ("au", "ÐÐ²ÑÑÑÐ°Ð»Ð¸Ñ"), "62": ("id", "ÐÐ½Ð´Ð¾Ð½ÐµÐ·Ð¸Ñ"), "63": ("ph", "Ð¤Ð¸Ð»Ð¸Ð¿Ð¿Ð¸Ð½Ñ"), "64": ("nz", "ÐÐ¾Ð²Ð°Ñ ÐÐµÐ»Ð°Ð½Ð´Ð¸Ñ"),
    "65": ("sg", "Ð¡Ð¸Ð½Ð³Ð°Ð¿ÑÑ"), "66": ("th", "Ð¢Ð°Ð¸Ð»Ð°Ð½Ð´"), "81": ("jp", "Ð¯Ð¿Ð¾Ð½Ð¸Ñ"), "82": ("kr", "Ð®Ð¶Ð½Ð°Ñ ÐÐ¾ÑÐµÑ"),
    "84": ("vn", "ÐÑÐµÑÐ½Ð°Ð¼"), "86": ("cn", "ÐÐ¸ÑÐ°Ð¹"), "90": ("tr", "Ð¢ÑÑÑÐ¸Ñ"), "91": ("in", "ÐÐ½Ð´Ð¸Ñ"),
    "92": ("pk", "ÐÐ°ÐºÐ¸ÑÑÐ°Ð½"), "93": ("af", "ÐÑÐ³Ð°Ð½Ð¸ÑÑÐ°Ð½"), "94": ("lk", "Ð¨ÑÐ¸-ÐÐ°Ð½ÐºÐ°"), "95": ("mm", "ÐÑÑÐ½Ð¼Ð°"),
    "98": ("ir", "ÐÑÐ°Ð½"), "212": ("ma", "ÐÐ°ÑÐ¾ÐºÐºÐ¾"), "213": ("dz", "ÐÐ»Ð¶Ð¸Ñ"), "216": ("tn", "Ð¢ÑÐ½Ð¸Ñ"),
    "234": ("ng", "ÐÐ¸Ð³ÐµÑÐ¸Ñ"), "254": ("ke", "ÐÐµÐ½Ð¸Ñ"), "351": ("pt", "ÐÐ¾ÑÑÑÐ³Ð°Ð»Ð¸Ñ"), "352": ("lu", "ÐÑÐºÑÐµÐ¼Ð±ÑÑÐ³"),
    "353": ("ie", "ÐÑÐ»Ð°Ð½Ð´Ð¸Ñ"), "358": ("fi", "Ð¤Ð¸Ð½Ð»ÑÐ½Ð´Ð¸Ñ"), "359": ("bg", "ÐÐ¾Ð»Ð³Ð°ÑÐ¸Ñ"), "370": ("lt", "ÐÐ¸ÑÐ²Ð°"),
    "371": ("lv", "ÐÐ°ÑÐ²Ð¸Ñ"), "372": ("ee", "Ð­ÑÑÐ¾Ð½Ð¸Ñ"), "373": ("md", "ÐÐ¾Ð»Ð´Ð¾Ð²Ð°"), "374": ("am", "ÐÑÐ¼ÐµÐ½Ð¸Ñ"),
    "375": ("by", "ÐÐµÐ»Ð°ÑÑÑÑ"), "380": ("ua", "Ð£ÐºÑÐ°Ð¸Ð½Ð°"), "381": ("rs", "Ð¡ÐµÑÐ±Ð¸Ñ"), "385": ("hr", "Ð¥Ð¾ÑÐ²Ð°ÑÐ¸Ñ"),
    "386": ("si", "Ð¡Ð»Ð¾Ð²ÐµÐ½Ð¸Ñ"), "420": ("cz", "Ð§ÐµÑÐ¸Ñ"), "421": ("sk", "Ð¡Ð»Ð¾Ð²Ð°ÐºÐ¸Ñ"), "852": ("hk", "ÐÐ¾Ð½ÐºÐ¾Ð½Ð³"),
    "855": ("kh", "ÐÐ°Ð¼Ð±Ð¾Ð´Ð¶Ð°"), "856": ("la", "ÐÐ°Ð¾Ñ"), "880": ("bd", "ÐÐ°Ð½Ð³Ð»Ð°Ð´ÐµÑ"), "886": ("tw", "Ð¢Ð°Ð¹Ð²Ð°Ð½Ñ"),
    "961": ("lb", "ÐÐ¸Ð²Ð°Ð½"), "962": ("jo", "ÐÐ¾ÑÐ´Ð°Ð½Ð¸Ñ"), "964": ("iq", "ÐÑÐ°Ðº"), "965": ("kw", "ÐÑÐ²ÐµÐ¹Ñ"),
    "966": ("sa", "Ð¡Ð°ÑÐ´Ð¾Ð²ÑÐºÐ°Ñ ÐÑÐ°Ð²Ð¸Ñ"), "971": ("ae", "ÐÐÐ­"), "972": ("il", "ÐÐ·ÑÐ°Ð¸Ð»Ñ"), "974": ("qa", "ÐÐ°ÑÐ°Ñ"),
    "976": ("mn", "ÐÐ¾Ð½Ð³Ð¾Ð»Ð¸Ñ"), "977": ("np", "ÐÐµÐ¿Ð°Ð»"), "992": ("tj", "Ð¢Ð°Ð´Ð¶Ð¸ÐºÐ¸ÑÑÐ°Ð½"), "993": ("tm", "Ð¢ÑÑÐºÐ¼ÐµÐ½Ð¸ÑÑÐ°Ð½"),
    "994": ("az", "ÐÐ·ÐµÑÐ±Ð°Ð¹Ð´Ð¶Ð°Ð½"), "995": ("ge", "ÐÑÑÐ·Ð¸Ñ"), "996": ("kg", "ÐÐ¸ÑÐ³Ð¸Ð·Ð¸Ñ"), "998": ("uz", "Ð£Ð·Ð±ÐµÐºÐ¸ÑÑÐ°Ð½"),
}
COUNTRIES.update({
    "211": ("ss", "Ð®Ð¶Ð½ÑÐ¹ Ð¡ÑÐ´Ð°Ð½"), "218": ("ly", "ÐÐ¸Ð²Ð¸Ñ"), "220": ("gm", "ÐÐ°Ð¼Ð±Ð¸Ñ"), "221": ("sn", "Ð¡ÐµÐ½ÐµÐ³Ð°Ð»"),
    "225": ("ci", "ÐÐ¾Ñ-Ð´'ÐÐ²ÑÐ°Ñ"), "231": ("lr", "ÐÐ¸Ð±ÐµÑÐ¸Ñ"), "232": ("sl", "Ð¡ÑÐµÑÑÐ°-ÐÐµÐ¾Ð½Ðµ"), "233": ("gh", "ÐÐ°Ð½Ð°"),
    "235": ("td", "Ð§Ð°Ð´"), "237": ("cm", "ÐÐ°Ð¼ÐµÑÑÐ½"), "241": ("ga", "ÐÐ°Ð±Ð¾Ð½"), "242": ("cg", "ÐÐ¾Ð½Ð³Ð¾"),
    "243": ("cd", "ÐÐ  ÐÐ¾Ð½Ð³Ð¾"), "244": ("ao", "ÐÐ½Ð³Ð¾Ð»Ð°"), "249": ("sd", "Ð¡ÑÐ´Ð°Ð½"), "250": ("rw", "Ð ÑÐ°Ð½Ð´Ð°"),
    "251": ("et", "Ð­ÑÐ¸Ð¾Ð¿Ð¸Ñ"), "255": ("tz", "Ð¢Ð°Ð½Ð·Ð°Ð½Ð¸Ñ"), "256": ("ug", "Ð£Ð³Ð°Ð½Ð´Ð°"), "258": ("mz", "ÐÐ¾Ð·Ð°Ð¼Ð±Ð¸Ðº"),
    "260": ("zm", "ÐÐ°Ð¼Ð±Ð¸Ñ"), "261": ("mg", "ÐÐ°Ð´Ð°Ð³Ð°ÑÐºÐ°Ñ"), "263": ("zw", "ÐÐ¸Ð¼Ð±Ð°Ð±Ð²Ðµ"), "264": ("na", "ÐÐ°Ð¼Ð¸Ð±Ð¸Ñ"),
    "267": ("bw", "ÐÐ¾ÑÑÐ²Ð°Ð½Ð°"), "355": ("al", "ÐÐ»Ð±Ð°Ð½Ð¸Ñ"), "356": ("mt", "ÐÐ°Ð»ÑÑÐ°"), "357": ("cy", "ÐÐ¸Ð¿Ñ"),
    "382": ("me", "Ð§ÐµÑÐ½Ð¾Ð³Ð¾ÑÐ¸Ñ"), "387": ("ba", "ÐÐ¾ÑÐ½Ð¸Ñ Ð¸ ÐÐµÑÑÐµÐ³Ð¾Ð²Ð¸Ð½Ð°"), "389": ("mk", "Ð¡ÐµÐ²ÐµÑÐ½Ð°Ñ ÐÐ°ÐºÐµÐ´Ð¾Ð½Ð¸Ñ"),
    "502": ("gt", "ÐÐ²Ð°ÑÐµÐ¼Ð°Ð»Ð°"), "503": ("sv", "Ð¡Ð°Ð»ÑÐ²Ð°Ð´Ð¾Ñ"), "504": ("hn", "ÐÐ¾Ð½Ð´ÑÑÐ°Ñ"), "505": ("ni", "ÐÐ¸ÐºÐ°ÑÐ°Ð³ÑÐ°"),
    "506": ("cr", "ÐÐ¾ÑÑÐ°-Ð Ð¸ÐºÐ°"), "507": ("pa", "ÐÐ°Ð½Ð°Ð¼Ð°"), "509": ("ht", "ÐÐ°Ð¸ÑÐ¸"), "591": ("bo", "ÐÐ¾Ð»Ð¸Ð²Ð¸Ñ"),
    "592": ("gy", "ÐÐ°Ð¹Ð°Ð½Ð°"), "593": ("ec", "Ð­ÐºÐ²Ð°Ð´Ð¾Ñ"), "595": ("py", "ÐÐ°ÑÐ°Ð³Ð²Ð°Ð¹"), "598": ("uy", "Ð£ÑÑÐ³Ð²Ð°Ð¹"),
    "673": ("bn", "ÐÑÑÐ½ÐµÐ¹"), "853": ("mo", "ÐÐ°ÐºÐ°Ð¾"), "960": ("mv", "ÐÐ°Ð»ÑÐ´Ð¸Ð²Ñ"), "963": ("sy", "Ð¡Ð¸ÑÐ¸Ñ"),
    "967": ("ye", "ÐÐµÐ¼ÐµÐ½"), "968": ("om", "ÐÐ¼Ð°Ð½"), "973": ("bh", "ÐÐ°ÑÑÐµÐ¹Ð½"), "975": ("bt", "ÐÑÑÐ°Ð½"),
})


def country_info(phone: str) -> dict:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("7") and len(d) > 1 and d[1] in "67":
        return {"iso": "kz", "name": "ÐÐ°Ð·Ð°ÑÑÑÐ°Ð½", "dial": "7"}
    for ln in (3, 2, 1):
        if d[:ln] in COUNTRIES:
            iso, name = COUNTRIES[d[:ln]]
            return {"iso": iso, "name": name, "dial": d[:ln]}
    return {"iso": "", "name": "ÐÑÑÐ³Ð°Ñ ÑÑÑÐ°Ð½Ð°", "dial": d[:2]}


def flag_emoji(iso: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 97) for c in iso.lower()) if iso else ""


def country_label(phone: str) -> str:
    ci = country_info(phone)
    return f"{ci['name']} [+{ci['dial']}]"


def ensure_account_ids() -> None:
    for item in CATALOG.values():
        for acc in item.get("accounts", []):
            if not acc.get("id"):
                acc["id"] = secrets.token_hex(3)


def _parse_date(tok: str) -> str | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(tok, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_account_line(line: str) -> tuple[dict | None, list[str]]:
    warn: list[str] = []
    main, *meta = [p.strip() for p in line.split("|")]
    phone, _, secret = main.partition(":")
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None, [f"Ð½Ðµ Ð¿Ð¾Ð½ÑÐ» Ð½Ð¾Ð¼ÐµÑ: {line[:25]}"]
    acc: dict = {"id": secrets.token_hex(3), "phone": "+" + digits, "secret": secret.strip()}
    for tok in meta:
        t = tok.lower().strip()
        if not t:
            continue
        iso_date = _parse_date(t)
        if iso_date:
            acc["reg"] = iso_date
        elif t in ("Ð±ÐµÐ· ÑÐ±", "Ð±ÐµÐ· ÑÐ¿Ð°Ð¼Ð±Ð»Ð¾ÐºÐ°", "Ð½ÐµÑ ÑÐ±", "ÑÐ¸ÑÑÑÐ¹", "nosb", "clean"):
            acc["sb"] = False
        elif t in ("ÑÐ±", "ÑÐ¿Ð°Ð¼Ð±Ð»Ð¾Ðº", "sb", "spam", "spamblock"):
            acc["sb"] = True
        elif t in ("prem", "premium", "Ð¿ÑÐµÐ¼", "Ð¿ÑÐµÐ¼Ð¸ÑÐ¼"):
            acc["premium"] = True
        elif re.fullmatch(r"\d+([.,]\d+)?", t):
            acc["price_rub"] = float(t.replace(",", "."))
        else:
            warn.append(f"Â«{tok[:15]}Â» Ð¿ÑÐ¾Ð¿ÑÑÐµÐ½Ð¾")
    return acc, warn


def lot_view(key: str, item: dict, acc: dict) -> dict:
    digits = re.sub(r"\D", "", acc.get("phone", ""))
    session, _ = _parse_account_secret(acc.get("secret", ""))
    return {
        "id": acc.get("id", ""),
        "key": key,
        "title": item["title"],
        "mask": f"+{digits[:3]}...{digits[-4:]}" if len(digits) > 8 else f"+{digits[:2]}...{digits[-2:]}",
        "country": country_info(acc.get("phone", "")),
        "reg": acc.get("reg", ""),
        "sb": acc.get("sb"),
        "premium": bool(acc.get("premium")),
        "auto": bool(session),
        "price_rub": round(float(acc.get("price_rub") or item["price_rub"]), 2),
    }


def lots_out() -> list[dict]:
    return [lot_view(k, it, a) for k, it in CATALOG.items() for a in it.get("accounts", []) if a.get("id")]


async def do_purchase(uid: int, key: str, acc_id: str | None = None) -> tuple[dict | None, str | None]:
    global TOTAL_SOLD_COUNT
    item = CATALOG.get(key)
    accounts = item.get("accounts", []) if item else []
    if not accounts:
        return None, "ÐÐµÑ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸"
    if acc_id:
        idx = next((i for i, a in enumerate(accounts) if a.get("id") == acc_id), None)
        if idx is None:
            return None, "Ð­ÑÐ¾Ñ Ð°ÐºÐºÐ°ÑÐ½Ñ ÑÐ¶Ðµ Ð¿ÑÐ¾Ð´Ð°Ð½"
    else:
        idx = 0
    acc = accounts[idx]
    price = round(float(acc.get("price_rub") or item["price_rub"]), 2)
    if get_rub_balance(uid) < price:
        return None, f"ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÑÐµÐ´ÑÑÐ² ({price:.2f} â½). ÐÐ¾Ð¿Ð¾Ð»Ð½Ð¸ Ð±Ð°Ð»Ð°Ð½Ñ!"
    accounts.pop(idx)
    USER_RUB_BALANCE[uid] = round(get_rub_balance(uid) - price, 2)
    TOTAL_SOLD_COUNT += 1
    session_str, two_fa = _parse_account_secret(acc.get("secret", ""))
    label = country_label(acc.get("phone", ""))
    tx_add(uid, "buy", -price, label)
    rec = {
        "order_id": f"ORD-{secrets.token_hex(3).upper()}",
        "title": label,
        "phone": acc.get("phone", ""),
        "session_string": session_str,
        "two_fa": two_fa,
        "code": "",
        "price_rub": price,
        "ts": int(time.time()),
        "reviewed": False,
        "finished": False,
        "reg": acc.get("reg", ""),
        "sb": acc.get("sb"),
        "premium": bool(acc.get("premium")),
        "left": len(accounts),
    }
    LAST_ISSUED_ACCOUNT[uid] = rec
    PURCHASE_HISTORY.setdefault(uid, []).append(rec)
    RECENT_SALES.insert(0, {"title": label, "ts": rec["ts"]})
    del RECENT_SALES[30:]
    await save_data()
    await ref_try_pay(uid, "purchase")
    return rec, None


def public_order(o: dict) -> dict:
    finished = o["finished"] if "finished" in o else bool(o.get("code"))
    return {
        "order_id": o.get("order_id", ""),
        "title": o.get("title", ""),
        "phone": o.get("phone", ""),
        "two_fa": o.get("two_fa", ""),
        "code": o.get("code", ""),
        "reviewed": bool(o.get("reviewed")),
        "finished": bool(finished),
        "country": country_info(o.get("phone", "")),
        "reg": o.get("reg", ""),
        "sb": o.get("sb"),
        "premium": bool(o.get("premium")),
        "price_rub": o.get("price_rub", 0),
        "ts": o.get("ts", 0),
    }


def reviews_payload(uid: int = 0) -> dict:
    n = len(REVIEWS)
    avg = round(sum(r["rating"] for r in REVIEWS) / n, 1) if n else 0
    return {
        "avg": avg,
        "count": n,
        "list": [
            {
                "id": r.get("id", ""),
                "name": r["name"],
                "rating": r["rating"],
                "text": r["text"],
                "ts": r["ts"],
                "edited": bool(r.get("edited")),
                "mine": r.get("user_id") == uid,
            }
            for r in REVIEWS[:60]
        ],
    }


def build_state_payload(user_id: int, is_subscribed: bool = True) -> dict:
    items_out = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        if cnt > 0:
            items_out.append({
                "key": key,
                "title": item["title"],
                "description": item.get("description", ""),
                "price_rub": round(float(item["price_rub"]), 2),
                "count": cnt,
            })
    return {
        "ok": True,
        "user_id": user_id,
        "balance_rub": get_rub_balance(user_id),
        "rub_per_star": RUB_PER_STAR,
        "rub_per_ton": RUB_PER_TON,
        "crypto_wallet": CRYPTO_WALLET,
        "required_channel": REQUIRED_CHANNEL,
        "is_subscribed": is_subscribed,
        "orders": [public_order(o) for o in PURCHASE_HISTORY.get(user_id, []) if isinstance(o, dict)],
        "items": items_out,
        "lots": lots_out(),
        "guarantee_hours": GUARANTEE_HOURS,
        "price_split": PRICE_SPLIT,
        "total_sold_count": TOTAL_SOLD_COUNT,
        "recent_sales": RECENT_SALES[:15],
        "reviews": reviews_payload(user_id),
        "is_staff": is_admin(user_id),
        "history": list(reversed(TX_LOG.get(user_id, [])))[:50],
        "referral": referral_payload(user_id),
        "leaders": leaders_payload(user_id),
        "roulette": roulette_status(user_id),
    }


async def support_reply(bot_instance: Bot, uid: int, text: str) -> None:
    SUPPORT.setdefault(uid, []).append({"from": "admin", "text": text[:2000], "ts": int(time.time())})
    del SUPPORT[uid][:-200]
    await save_data()
    try:
        await bot_instance.send_message(
            chat_id=uid,
            text=f"ð¬ <b>ÐÑÐ²ÐµÑ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐ¸:</b>

{html.escape(text)}

<i>ÐÑÐ¾Ð´Ð¾Ð»Ð¶Ð¸ÑÑ Ð´Ð¸Ð°Ð»Ð¾Ð³ Ð¼Ð¾Ð¶Ð½Ð¾ Ð² Ð¼Ð¸Ð½Ð¸-Ð¿ÑÐ¸Ð»Ð¾Ð¶ÐµÐ½Ð¸Ð¸ Â«ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°Â».</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass


def roulette_day() -> int:
    return int((time.time() + MSK_OFFSET) // 86400)


def roulette_status(uid: int) -> dict:
    today = roulette_day()
    next_in = int((today + 1) * 86400 - (time.time() + MSK_OFFSET))
    return {"available": LAST_SPIN.get(uid) != today, "next_in": max(0, next_in), "prizes": ROULETTE_PRIZES}


def ton_memo_for(uid: int) -> str:
    now = time.time()
    for m in [m for m, v in PENDING_TON.items() if now - v["ts"] > 3 * 86400]:
        del PENDING_TON[m]
    for m, v in PENDING_TON.items():
        if v["uid"] == uid and now - v["ts"] < 3600:
            return m
    memo = "DH" + secrets.token_hex(4).upper()
    PENDING_TON[memo] = {"uid": uid, "ts": int(now)}
    return memo


def ton_pay_link(amount_ton: float, memo: str) -> str:
    nano = int(round(amount_ton * 1e9))
    return f"https://app.tonkeeper.com/transfer/{CRYPTO_WALLET}?amount={nano}&text={memo}"


async def ton_poll_once(bot_instance: Bot | None) -> None:
    headers = {"X-API-Key": TONCENTER_API_KEY} if TONCENTER_API_KEY else {}
    params = {"address": CRYPTO_WALLET, "limit": "40", "archival": "true"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
        async with sess.get("https://toncenter.com/api/v2/getTransactions", params=params, headers=headers) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error", "toncenter error")))
    for tx in reversed(data.get("result", [])):
        tid = tx.get("transaction_id", {})
        key = f"{tid.get('lt')}:{tid.get('hash')}"
        if key in PROCESSED_TX or tx.get("out_msgs"):
            continue
        msg = tx.get("in_msg") or {}
        if not msg.get("source"):
            continue
        try:
            value = int(msg.get("value") or 0)
        except ValueError:
            continue
        pend = PENDING_TON.get((msg.get("message") or "").strip())
        if not pend or value <= 0:
            continue
        PROCESSED_TX.append(key)
        del PROCESSED_TX[:-1000]
        uid = int(pend["uid"])
        ton = value / 1e9
        if ton < MIN_TON:
            await save_data()
            if bot_instance:
                await notify_owner(bot_instance, f"â ï¸ TON Ð½Ð¸Ð¶Ðµ Ð¼Ð¸Ð½Ð¸Ð¼ÑÐ¼Ð° ({ton:.4f}) Ð¾Ñ <code>{uid}</code> Ð½Ðµ Ð·Ð°ÑÐ¸ÑÐ»ÐµÐ½Ð¾ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¸.")
            continue
        rub = round(ton * RUB_PER_TON, 2)
        tx_add(uid, "dep_ton", rub, f"{ton:.4f} TON")
        new_bal = await add_rub_balance(uid, rub)
        if bot_instance:
            try:
                await bot_instance.send_message(
                    uid,
                    f"â <b>ÐÐ¾Ð»ÑÑÐµÐ½Ð¾ {ton:.4f} TON</b>
ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¾ <b>+{rub:.2f} â½</b>. ÐÐ°Ð»Ð°Ð½Ñ: <b>{new_bal:.2f} â½</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await notify_owner(bot_instance, f"ð ÐÐ²ÑÐ¾-Ð·Ð°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ TON: <code>{uid}</code> â {ton:.4f} TON (+{rub:.2f} â½, Ð±Ð°Ð»Ð°Ð½Ñ: {new_bal:.2f} â½)")


async def ton_poll_loop(bot_instance: Bot | None) -> None:
    await asyncio.sleep(5)
    while True:
        delay = 10
        try:
            if PENDING_TON:
                await ton_poll_once(bot_instance)
        except Exception as e:
            print(f"TON poll error: {e}")
            delay = 20
        await asyncio.sleep(delay)


# ÐÐ»Ð°Ð²Ð¸Ð°ÑÑÑÑ
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
    return (
        f"ð <b>ÐÑÐ¸Ð²ÐµÑ, {_format_user_label(user)} â Ð´Ð¾Ð±ÑÐ¾ Ð¿Ð¾Ð¶Ð°Ð»Ð¾Ð²Ð°ÑÑ Ð² DarkHub.</b>

"
        "ÐÐ°Ð³Ð°Ð·Ð¸Ð½ Ð½Ð¾Ð¼ÐµÑÐ¾Ð² Ð¸ Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð² Telegram. ÐÑÐ±Ð¸ÑÐ°Ð¹ ÑÑÑÐ°Ð½Ñ Ð¸ Ð¾Ð¿Ð»Ð°ÑÐ¸Ð²Ð°Ð¹ Ñ Ð±Ð°Ð»Ð°Ð½ÑÐ° â "
        "Ð½Ð¾Ð¼ÐµÑ, Ð¿Ð°ÑÐ¾Ð»Ñ Ð¸ ÐºÐ¾Ð´ Ð²ÑÐ¾Ð´Ð° Ð¿ÑÐ¸Ð´ÑÑ ÑÑÐ°Ð·Ñ Ð² Ð¿ÑÐ¸Ð»Ð¾Ð¶ÐµÐ½Ð¸Ð¸.

"
        f"ð¦ <b>ÐÑÐ¾Ð´Ð°Ð½Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð²:</b> {TOTAL_SOLD_COUNT}"
    )


def app_btn(text: str, page: str) -> InlineKeyboardButton | None:
    if WEBAPP_URL:
        sep = "&" if "?" in WEBAPP_URL else "?"
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=f"{WEBAPP_URL}{sep}v={WEBAPP_VER}&page={page}"))
    fallback = {"shop": "menu_catalog", "profile": "menu_profile", "deposit": "menu_deposit", "faq": "menu_rules"}
    if page in fallback:
        return InlineKeyboardButton(text=text, callback_data=fallback[page])
    if page == "reviews":
        return InlineKeyboardButton(text=text, url=REVIEWS_CHANNEL_URL)
    if page == "help":
        return InlineKeyboardButton(text=text, url=f"https://t.me/{SUPPORT_USERNAME}")
    return None


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [app_btn("ð ÐÐÐ¢ÐÐÐÐ / ÐÐÐÐÐÐÐ", "shop")],
        [app_btn("ð¤ ÐÑÐ¾ÑÐ¸Ð»Ñ", "profile"), app_btn("ð³ ÐÐ¾Ð¿Ð¾Ð»Ð½Ð¸ÑÑ", "deposit")],
        [app_btn("ð¦ ÐÐ°ÐºÐ°Ð·Ñ", "orders"), app_btn("â­ ÐÑÐ·ÑÐ²Ñ", "reviews")],
        [app_btn("ð° Ð ÑÐ»ÐµÑÐºÐ°", "roulette"), app_btn("ð ÐÐ¸Ð´ÐµÑÑ", "leaders")],
        [app_btn("â FAQ", "faq"), app_btn("ð¬ ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°", "help")],
    ]
    clean_rows = [[b for b in row if b] for row in rows]
    clean_rows = [row for row in clean_rows if row]
    if is_owner(user_id):
        clean_rows.append([InlineKeyboardButton(text="ð ÐÐ°Ð½ÐµÐ»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", callback_data="owner_panel")])
    elif is_admin(user_id):
        clean_rows.append([InlineKeyboardButton(text="ð ï¸ ÐÐ´Ð¼Ð¸Ð½ÐºÐ°", callback_data="staff_panel")])
    return InlineKeyboardMarkup(inline_keyboard=clean_rows)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def panel_keyboard(owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="+ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ", callback_data="add_start")],
        [InlineKeyboardButton(text="ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸ / ÑÐ´Ð°Ð»ÐµÐ½Ð¸Ðµ", callback_data="pos_list"), InlineKeyboardButton(text="ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ", callback_data="owner_new_cat")],
    ]
    if owner:
        rows += [
            [InlineKeyboardButton(text="ð¢ ÐÐ°Ð½Ð°Ð» Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ (ÐÐ)", callback_data="owner_op_menu")],
            [InlineKeyboardButton(text="ð Ð¡ÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ°", callback_data="owner_stats"), InlineKeyboardButton(text="ðï¸ ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´Ñ", callback_data="owner_promos")],
            [InlineKeyboardButton(text="ð£ Ð Ð°ÑÑÑÐ»ÐºÐ°", callback_data="owner_bcast"), InlineKeyboardButton(text="ð ÐÑÑÐ½Ð°Ð» Ð´ÐµÐ¹ÑÑÐ²Ð¸Ð¹", callback_data="owner_log")],
            [InlineKeyboardButton(text="ð° ÐÑÐ´Ð°ÑÑ Ð±Ð°Ð»Ð°Ð½Ñ", callback_data="owner_give_bal"), InlineKeyboardButton(text="ð ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID", callback_data="staff_check_bal")],
            [InlineKeyboardButton(text="ð® ÐÐ´Ð¼Ð¸Ð½Ñ", callback_data="owner_admins")],
        ]
    else:
        rows.append([InlineKeyboardButton(text="ð ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_panel_keyboard() -> InlineKeyboardMarkup:
    return panel_keyboard(True)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return panel_keyboard(False)


def panel_text(owner: bool) -> str:
    buyers = sum(1 for v in PURCHASE_HISTORY.values() if v)
    total_stock = sum(len(v.get("accounts", [])) for v in CATALOG.values())
    cat = {"0": [0, 0], "1": [0, 0], "n": [0, 0]}
    by_country: dict[tuple[str, str], int] = {}
    for v in CATALOG.values():
        for a in v.get("accounts", []):
            k = "0" if a.get("sb") is False else "1" if a.get("sb") is True else "n"
            price = float(a.get("price_rub") or v["price_rub"])
            cat[k][0 if price <= PRICE_SPLIT else 1] += 1
            ci = country_info(a.get("phone", ""))
            by_country[(ci["iso"], ci["name"])] = by_country.get((ci["iso"], ci["name"]), 0) + 1
    op_status = html.escape(REQUIRED_CHANNEL) if REQUIRED_CHANNEL else "<i>Ð¾ÑÐºÐ»ÑÑÐµÐ½Ð°</i>"
    lines = [
        "ð <b>ÐÐ°Ð½ÐµÐ»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°</b>" if owner else "ð ï¸ <b>ÐÐ´Ð¼Ð¸Ð½ÐºÐ°</b>",
        "â",
        f"ð¦ ÐÑÐ¾Ð´Ð°Ð½Ð¾: <b>{TOTAL_SOLD_COUNT}</b> | Ð Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸: <b>{total_stock}</b>",
        f"ð¢ ÐÐ±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð°Ñ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ°: {op_status}",
    ]
    if owner:
        lines.append(f"ð¥ ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»ÐµÐ¹: <b>{buyers}</b> | ÐÐ´Ð¼Ð¸Ð½Ð¾Ð²: <b>{len(ADMIN_IDS)}</b>")
    lines += [
        "",
        "ð <b>Ð Ð°ÑÐ¿ÑÐµÐ´ÐµÐ»ÐµÐ½Ð¸Ðµ</b>",
        f"â ÐÐµÐ· Ð¡Ð: <b>{sum(cat['0'])}</b> (Ð´ÐµÑÑÐ²ÑÑ {cat['0'][0]}, Ð´Ð¾ÑÐ¾Ð³Ð¸Ñ {cat['0'][1]})",
        f"â ï¸ Ð¡ Ð¡Ð: <b>{sum(cat['1'])}</b> (Ð´ÐµÑÑÐ²ÑÑ {cat['1'][0]}, Ð´Ð¾ÑÐ¾Ð³Ð¸Ñ {cat['1'][1]})",
    ]
    if sum(cat["n"]):
        lines.append(f"â ÐÐµÐ· Ð¾ÑÐ¼ÐµÑÐºÐ¸: <b>{sum(cat['n'])}</b>")
    lines.append(f"<i>ÐÐµÑÑÐ²ÑÐµ Ð´Ð¾ {PRICE_SPLIT:.0f} â½, Ð´Ð¾ÑÐ¾Ð³Ð¸Ðµ Ð´Ð¾ÑÐ¾Ð¶Ðµ. Ð Ð°ÑÐ¿ÑÐµÐ´ÐµÐ»ÑÐµÑÑÑ Ð¿Ð¾ ÑÐµÐ½Ðµ ÑÐ°Ð¼Ð¾.</i>")
    lines += ["", "ð <b>ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸</b>"]
    if not CATALOG:
        lines.append("â Ð¿Ð¾ÐºÐ° Ð½ÐµÑ, ÑÐ¾Ð·Ð´Ð°Ð¹ ÑÐµÑÐµÐ· Â«ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸ÑÂ»")
    else:
        for k, v in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"])):
            cnt = len(v.get("accounts", []))
            lines.append(f"â¢ <b>{html.escape(v['title'])}</b> â <b>{cnt} ÑÑ.</b> ({v['price_rub']:.0f} â½)")
    if by_country:
        lines += ["", "ð <b>Ð¡ÑÑÐ°Ð½Ñ</b>"]
        lines.append(" ".join(f"{flag_emoji(i)} {n}x{c}" for (i, n), c in sorted(by_country.items(), key=lambda x: -x[1])))
    if owner:
        lines += ["", "â¨ï¸ ÐÐ¾Ð´ Ð²ÑÑÑÐ½ÑÑ: <code>/code ID ÐÐÐ</code>", "ð¬ ÐÑÐ²ÐµÑ Ð² Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÑ: ÑÐµÐ¿Ð»Ð°ÐµÐ¼ Ð¸Ð»Ð¸ <code>/reply ID ÑÐµÐºÑÑ</code>"]
    return "
".join(lines)


def catalog_items_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"])):
        cnt = len(item.get("accounts", []))
        if cnt > 0:
            rows.append([InlineKeyboardButton(text=f"{item['title']} â {item['price_rub']:.0f} â½ ({cnt} ÑÑ.)", callback_data=f"item_view:{key}")])
    rows.append([InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_view_keyboard(key: str) -> InlineKeyboardMarkup:
    item = CATALOG.get(key, {})
    price = float(item.get("price_rub", 0))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"ð³ ÐÑÐ¿Ð¸ÑÑ Ð·Ð° {price:.0f} â½", callback_data=f"buy_acc:{key}")],
        [InlineKeyboardButton(text="Â« Ð ÐºÐ°ÑÐ°Ð»Ð¾Ð³", callback_data="menu_catalog")],
    ])


def issued_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ð ÐÐ¾Ð»ÑÑÐ¸ÑÑ / Ð·Ð°Ð¿ÑÐ¾ÑÐ¸ÑÑ ÐºÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°", callback_data="req_code")],
        [InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
    ])


def deposit_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="â­ ÐÐ²ÑÐ·Ð´Ñ â½", callback_data="dep_method:stars"),
            InlineKeyboardButton(text="ð TON â½", callback_data="dep_method:ton"),
        ],
        [InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
    ])


def back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ð¬ ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
    ])


# Ð¥ÐµÐ½Ð´Ð»ÐµÑÑ Ð±Ð¾ÑÐ°
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    WAITING_INPUT.pop(message.from_user.id, None)
    arg = (command.args or "").strip() if command else ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    await register_user(message.from_user.id, who(message.from_user), ref)
    if not await ensure_sub_message(message):
        return
    await message.answer(
        get_welcome_text(message.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data == "check_sub_cb")
async def cb_check_sub(callback: CallbackQuery):
    ok = await check_user_subscription(callback.from_user.id)
    if not ok:
        await callback.answer("â ÐÑ ÐµÑÑ Ð½Ðµ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°Ð»Ð¸ÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð»!", show_alert=True)
        return
    await callback.answer("â ÐÐ¾Ð´Ð¿Ð¸ÑÐºÐ° Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð°!", show_alert=False)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        get_welcome_text(callback.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )


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
    phone_line = f"
<b>ÐÐ¾Ð¼ÐµÑ:</b> <code>{phone}</code>" if phone else ""
    two_fa = acc.get("two_fa") or ""
    two_fa_line = f"
<b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""
    await message.bot.send_message(
        chat_id=uid,
        text=f"ð <b>ÐÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°:</b> <code>{code}</code>{phone_line}{two_fa_line}",
        parse_mode="HTML",
    )
    await log_action(message.bot, message.from_user.id, who(message.from_user), f"Ð¾ÑÐ¿ÑÐ°Ð²Ð¸Ð» ÐºÐ¾Ð´ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{uid}</code>")
    await message.answer(f"â ÐÐ¾Ð´ <code>{code}</code> Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ <code>{uid}</code>", parse_mode="HTML")


@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>/reply USER_ID ÑÐµÐºÑÑ</code> Ð¸Ð»Ð¸ Ð¿ÑÐ¾ÑÑÐ¾ Ð¾ÑÐ²ÐµÑÑ ÑÐµÐ¿Ð»Ð°ÐµÐ¼ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Â«ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°Â».", parse_mode="HTML")
        return
    await support_reply(message.bot, int(parts[1]), parts[2].strip())
    await log_action(
        message.bot,
        message.from_user.id,
        who(message.from_user),
        f"Ð¾ÑÐ²ÐµÑÐ¸Ð» Ð² Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐµ <code>{int(parts[1])}</code>: {html.escape(parts[2].strip()[:80])}",
    )
    await message.answer("â ÐÑÐ²ÐµÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½")


@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    uid = message.from_user.id
    stars_paid = int(sp.total_amount or 0)
    rub_add = round(stars_paid * RUB_PER_STAR, 2)
    tx_add(uid, "dep_stars", rub_add, f"â­ {stars_paid}")
    USER_NAMES[uid] = who(message.from_user)
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
        f"â­ ÐÐ¿Ð»Ð°ÑÐ° Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ Ð¾Ñ <code>{uid}</code>: {stars_paid} -> +{rub_add:.2f} â½ (Ð±Ð°Ð»Ð°Ð½Ñ: {new_bal:.2f} â½)",
    )


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        get_welcome_text(callback.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_rules")
async def cb_menu_rules(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    await callback.message.edit_text(
        "<b>ÐÑÐ°Ð²Ð¸Ð»Ð° Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½Ð°</b>

"
        "1. ÐÑÐ¾Ð²ÐµÑÑÐ¹ÑÐµ Ð°ÐºÐºÐ°ÑÐ½ÑÑ ÑÑÐ°Ð·Ñ Ð¿Ð¾ÑÐ»Ðµ Ð²ÑÐ´Ð°ÑÐ¸.
"
        "2. ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ Ð¸ TON Ð·Ð°ÑÐ¸ÑÐ»ÑÐµÑÑÑ Ð½Ð° Ð²Ð°Ñ Ð»Ð¸ÑÐ½ÑÐ¹ Ð±Ð°Ð»Ð°Ð½Ñ Ð² â½.
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
    await callback.message.edit_text(panel_text(True), parse_mode="HTML", reply_markup=panel_keyboard(True))
    await callback.answer()


@dp.callback_query(F.data == "staff_panel")
async def cb_staff_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(panel_text(False), parse_mode="HTML", reply_markup=panel_keyboard(False))
    await callback.answer()


def _owner_only(callback: CallbackQuery) -> bool:
    return is_owner(callback.from_user.id)


async def _owner_prompt(callback: CallbackQuery, state: str, text: str):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = state
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "owner_op_menu")
async def cb_owner_op_menu(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    cur = html.escape(REQUIRED_CHANNEL) if REQUIRED_CHANNEL else "<i>Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ (ÐÐ Ð²ÑÐºÐ»ÑÑÐµÐ½Ð°)</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("âï¸ Ð£ÑÑÐ°Ð½Ð¾Ð²Ð¸ÑÑ / ÑÐ¼ÐµÐ½Ð¸ÑÑ ÐºÐ°Ð½Ð°Ð»", "owner_op_set")],
        [_btn("ðï¸ Ð£Ð´Ð°Ð»Ð¸ÑÑ / Ð²ÑÐºÐ»ÑÑÐ¸ÑÑ ÐºÐ°Ð½Ð°Ð»", "owner_op_clear")],
        [_btn("Â« ÐÐ°Ð·Ð°Ð´ Ð² Ð¿Ð°Ð½ÐµÐ»Ñ", "owner_panel")],
    ])
    await callback.message.edit_text(
        f"ð¢ <b>ÐÐ±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð°Ñ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ° (ÐÐ)</b>

"
        f"Ð¢ÐµÐºÑÑÐ¸Ð¹ ÐºÐ°Ð½Ð°Ð»: {cur}

"
        "â¢ Ð§ÑÐ¾Ð±Ñ Ð¿ÑÐ¾Ð²ÐµÑÐºÐ° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ ÑÐ°Ð±Ð¾ÑÐ°Ð»Ð° Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¸, Ð´Ð¾Ð±Ð°Ð²ÑÑÐµ Ð±Ð¾ÑÐ° Ð² Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÑ ÑÑÐ¾Ð³Ð¾ ÐºÐ°Ð½Ð°Ð»Ð°.
"
        "â¢ ÐÑ Ð¼Ð¾Ð¶ÐµÑÐµ ÑÐ¼ÐµÐ½Ð¸ÑÑ ÐºÐ°Ð½Ð°Ð» Ð¸Ð»Ð¸ ÑÐ´Ð°Ð»Ð¸ÑÑ ÐµÐ³Ð¾ Ð² Ð»ÑÐ±Ð¾Ð¹ Ð¼Ð¾Ð¼ÐµÐ½Ñ.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_op_set")
async def cb_owner_op_set(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_set_op"
    await callback.message.edit_text(
        "ð¢ <b>Ð£ÑÑÐ°Ð½Ð¾Ð²ÐºÐ° ÐºÐ°Ð½Ð°Ð»Ð° Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð¾Ð¹ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸</b>

"
        "ÐÑÐ¿ÑÐ°Ð²ÑÑÐµ ÑÑÑÐ»ÐºÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð» (Ð½Ð°Ð¿ÑÐ¸Ð¼ÐµÑ: <code>https://t.me/+WFd6USOIgnYyZjFl</code> Ð¸Ð»Ð¸ <code>https://t.me/channel_name</code>) Ð¸Ð»Ð¸ <code>@username</code>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÑÐ¼ÐµÐ½Ð°", "owner_op_menu")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_op_clear")
async def cb_owner_op_clear(callback: CallbackQuery):
    global REQUIRED_CHANNEL
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    REQUIRED_CHANNEL = ""
    await save_data()
    await callback.answer("ÐÐ±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð°Ñ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ° ÑÐ´Ð°Ð»ÐµÐ½Ð° Ð¸ Ð¾ÑÐºÐ»ÑÑÐµÐ½Ð°", show_alert=True)
    await cb_owner_op_menu(callback)


@dp.callback_query(F.data == "owner_new_cat")
async def cb_owner_new_cat(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_new_cat"
    await callback.message.edit_text(
        "ð <b>ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ</b>

"
        "Ð­ÑÐ¾ Ð³ÑÑÐ¿Ð¿Ð° Ñ ÑÐµÐ½Ð¾Ð¹ Ð¿Ð¾ ÑÐ¼Ð¾Ð»ÑÐ°Ð½Ð¸Ñ. Ð£ Ð¾ÑÐ´ÐµÐ»ÑÐ½Ð¾Ð³Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ° Ð¼Ð¾Ð¶Ð½Ð¾ ÑÐºÐ°Ð·Ð°ÑÑ ÑÐ²Ð¾Ñ ÑÐµÐ½Ñ.

"
        "Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐºÐ»ÑÑ | ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ | Ð¦ÐµÐ½Ð°_Ð²_ÑÑÐ±Ð»ÑÑ | ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ</code>
"
        "ÐÑÐ¸Ð¼ÐµÑ: <code>main | Telegram-Ð°ÐºÐºÐ°ÑÐ½ÑÑ | 60 | ÐÑÐ´Ð°ÑÐ° Ð·Ð° ÑÐµÐºÑÐ½Ð´Ñ</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_give_bal")
async def cb_owner_give_bal(callback: CallbackQuery):
    await _owner_prompt(
        callback,
        "owner_give_bal",
        "ð° <b>ÐÑÐ´Ð°ÑÐ° Ð±Ð°Ð»Ð°Ð½ÑÐ° (Ð² â½)</b>

Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>USER_ID Ð¡Ð£ÐÐÐ_Ð Ð£ÐÐÐÐ</code>
ÐÑÐ¸Ð¼ÐµÑ: <code>123456789 500</code>",
    )


@dp.callback_query(F.data == "owner_stats")
async def cb_owner_stats(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    await callback.message.edit_text(
        stats_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("ð ÐÐ±Ð½Ð¾Ð²Ð¸ÑÑ", "owner_stats"), _btn("Â« ÐÐ°Ð·Ð°Ð´", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_promos")
async def cb_owner_promos(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    rows = [
        [_btn(f"ðï¸ {c} (+{p['amount']:.0f} â½, {len(p['used'])}/{p['limit'] or 'â'})", f"promo_del:{c}")]
        for c, p in PROMOS.items()
    ]
    rows += [[_btn("+ Ð¡Ð¾Ð·Ð´Ð°ÑÑ Ð¿ÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´", "promo_new"), _btn("Â« ÐÐ°Ð·Ð°Ð´", "owner_panel")]]
    await callback.message.edit_text(
        "ðï¸ <b>ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´Ñ</b>

ÐÐ°Ð¶Ð¼Ð¸ Ð½Ð° Ð¿ÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´, ÑÑÐ¾Ð±Ñ ÑÐ´Ð°Ð»Ð¸ÑÑ ÐµÐ³Ð¾." if PROMOS else "ðï¸ <b>ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´Ñ</b>

ÐÐ¾ÐºÐ° Ð½ÐµÑ Ð½Ð¸ Ð¾Ð´Ð½Ð¾Ð³Ð¾.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data == "promo_new")
async def cb_promo_new(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "promo_new"
    await callback.message.edit_text(
        "ðï¸ <b>ÐÐ¾Ð²ÑÐ¹ Ð¿ÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´</b>

Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐÐÐ | ÑÑÐ¼Ð¼Ð° | Ð»Ð¸Ð¼Ð¸Ñ</code>
"
        "ÐÐ¸Ð¼Ð¸Ñ â ÑÐºÐ¾Ð»ÑÐºÐ¾ ÑÐµÐ»Ð¾Ð²ÐµÐº Ð¼Ð¾Ð³ÑÑ Ð¸ÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÑ (0 â Ð±ÐµÐ· Ð»Ð¸Ð¼Ð¸ÑÐ°). ÐÐ´Ð¸Ð½ ÑÐµÐ»Ð¾Ð²ÐµÐº â Ð¾Ð´Ð¸Ð½ ÑÐ°Ð·.
"
        "ÐÑÐ¸Ð¼ÐµÑ: <code>START|10|100</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÑÐ¼ÐµÐ½Ð°", "owner_promos")]]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("promo_del:"))
async def cb_promo_del(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    PROMOS.pop(callback.data.split(":", 1)[1], None)
    await save_data()
    await callback.answer("Ð£Ð´Ð°Ð»ÑÐ½")
    await cb_owner_promos(callback)


@dp.callback_query(F.data == "owner_bcast")
async def cb_owner_bcast(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "bcast"
    await callback.message.edit_text(
        f"ð£ <b>Ð Ð°ÑÑÑÐ»ÐºÐ°</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐµÐºÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ â Ð¿Ð¾ÐºÐ°Ð¶Ñ Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ.
ÐÐ¾Ð»ÑÑÐ°ÑÐµÐ»ÐµÐ¹: <b>{len(all_user_ids())}</b>.
"
        "ÐÐ¾Ð¶Ð½Ð¾ Ð¸ÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÑ ÑÐ¾ÑÐ¼Ð°ÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ Telegram (Ð¶Ð¸ÑÐ½ÑÐ¹, ÑÑÑÐ»ÐºÐ¸).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÑÐ¼ÐµÐ½Ð°", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "bcast_go")
async def cb_bcast_go(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    text = BCAST_PENDING.pop(callback.from_user.id, None)
    if not text:
        await callback.answer("ÐÐµÑ ÑÐµÐºÑÑÐ° ÑÐ°ÑÑÑÐ»ÐºÐ¸", show_alert=True)
        return
    asyncio.create_task(_run_broadcast(callback.bot, callback.from_user.id, text))
    await callback.message.edit_text(
        "ð£ Ð Ð°ÑÑÑÐ»ÐºÐ° Ð·Ð°Ð¿ÑÑÐµÐ½Ð°. ÐÑÐ¸ÑÐ»Ñ Ð¾ÑÑÑÑ, ÐºÐ¾Ð³Ð´Ð° Ð·Ð°ÐºÐ¾Ð½ÑÐ¸ÑÑÑ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« Ð Ð¿Ð°Ð½ÐµÐ»Ñ", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "bcast_cancel")
async def cb_bcast_cancel(callback: CallbackQuery):
    BCAST_PENDING.pop(callback.from_user.id, None)
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        "Ð Ð°ÑÑÑÐ»ÐºÐ° Ð¾ÑÐ¼ÐµÐ½ÐµÐ½Ð°.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« Ð Ð¿Ð°Ð½ÐµÐ»Ñ", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_log")
async def cb_owner_log(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    if not ADMIN_LOG:
        body = "ÐÐ¾ÐºÐ° Ð½ÐµÑ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ð¹ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²."
    else:
        body = "

".join(
            f"<code>{time.strftime('%d.%m %H:%M', time.gmtime(e['ts'] + MSK_OFFSET))}</code> {html.escape(e['name'])}
{e['text']}"
            for e in reversed(ADMIN_LOG[-15:])
        )
    await callback.message.edit_text(
        f"ð <b>ÐÑÑÐ½Ð°Ð» Ð´ÐµÐ¹ÑÑÐ²Ð¸Ð¹ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²</b>
<i>Ð²ÑÐµÐ¼Ñ ÐÐ¡Ð, Ð¿Ð¾ÑÐ»ÐµÐ´Ð½Ð¸Ðµ 15</i>

{body}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÐ°Ð·Ð°Ð´", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_admins")
async def cb_owner_admins(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Ð¢Ð¾Ð»ÑÐºÐ¾ Ð´Ð»Ñ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÐ°", show_alert=True)
        return
    adm = ", ".join(f"<code>{x}</code>" for x in sorted(ADMIN_IDS)) or "Ð½ÐµÑ"
    await callback.message.edit_text(
        f"ð® <b>ÐÐ´Ð¼Ð¸Ð½Ñ</b>

{adm}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="+ ÐÑÐ´Ð°ÑÑ", callback_data="owner_add_adm"),
                InlineKeyboardButton(text="- Ð¡Ð½ÑÑÑ", callback_data="owner_del_adm"),
            ],
            [InlineKeyboardButton(text="Â« ÐÐ°Ð·Ð°Ð´", callback_data="owner_panel")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_add_adm")
async def cb_owner_add_adm(callback: CallbackQuery):
    await _owner_prompt(callback, "owner_add_adm", "â <b>ÐÑÐ´Ð°ÑÐ° Ð°Ð´Ð¼Ð¸Ð½ÐºÐ¸</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:")


@dp.callback_query(F.data == "owner_del_adm")
async def cb_owner_del_adm(callback: CallbackQuery):
    await _owner_prompt(callback, "owner_del_adm", "â <b>Ð¡Ð½ÑÑÐ¸Ðµ Ð°Ð´Ð¼Ð¸Ð½ÐºÐ¸</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:")


@dp.callback_query(F.data == "staff_check_bal")
async def cb_staff_check_bal(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "staff_check_bal"
    await callback.message.edit_text(
        "ð <b>ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾ ID</b>

ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


SB_LABEL = {"0": "â ÐÐµÐ· Ð¡Ð", "1": "â ï¸ Ð¡ Ð¡Ð", "n": "â Ð±ÐµÐ· Ð¾ÑÐ¼ÐµÑÐºÐ¸"}


def panel_back_cb(uid: int) -> str:
    return "owner_panel" if is_owner(uid) else "staff_panel"


def to_panel_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÐµÑÑ", callback_data="add_start")],
        [InlineKeyboardButton(text="Â« Ð Ð¿Ð°Ð½ÐµÐ»Ñ", callback_data=panel_back_cb(uid))],
    ])


async def _deny(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°", show_alert=True)
        return True
    return False


@dp.callback_query(F.data == "add_start")
async def cb_add_start(callback: CallbackQuery):
    if await _deny(callback):
        return
    uid = callback.from_user.id
    WAITING_INPUT.pop(uid, None)
    if not CATALOG:
        await callback.message.edit_text(
            "â <b>ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ</b>

Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð½ÑÐ¶Ð½Ð° Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ (Ð³ÑÑÐ¿Ð¿Ð° Ñ ÑÐµÐ½Ð¾Ð¹). Ð¡Ð¾Ð·Ð´Ð°Ð¹ ÐµÑ ÐºÐ½Ð¾Ð¿ÐºÐ¾Ð¹ Ð½Ð¸Ð¶Ðµ.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("ð ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ", "owner_new_cat")],
                [_btn("Â« ÐÐ°Ð·Ð°Ð´", panel_back_cb(uid))],
            ]),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "â <b>ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ</b> â <i>ÑÐ°Ð³ 1 Ð¸Ð· 3</i>

"
        "ÐÐ°ÐºÐ¸Ðµ Ð°ÐºÐºÐ°ÑÐ½ÑÑ Ð´Ð¾Ð±Ð°Ð²Ð»ÑÐµÑÑ?
"
        "â <b>ÐÐµÐ· Ð¡Ð</b> â ÑÐ¸ÑÑÑÐµ, Ð±ÐµÐ· ÑÐ¿Ð°Ð¼Ð±Ð»Ð¾ÐºÐ°
"
        "â ï¸ <b>Ð¡ Ð¡Ð</b> â ÑÐ¾ ÑÐ¿Ð°Ð¼Ð±Ð»Ð¾ÐºÐ¾Ð¼",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("â ÐÐµÐ· Ð¡Ð", "staff_sb:0"), _btn("â ï¸ Ð¡ Ð¡Ð", "staff_sb:1")],
            [_btn("â ÐÐµ ÑÐºÐ°Ð·ÑÐ²Ð°ÑÑ", "staff_sb:n")],
            [_btn("Â« ÐÐ°Ð·Ð°Ð´", panel_back_cb(uid))],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("staff_sb:"))
async def cb_staff_sb(callback: CallbackQuery):
    if await _deny(callback):
        return
    sb = callback.data.split(":")[1]
    rows = [
        [_btn(f"{item['title']} â {item['price_rub']:.0f} â½ ({len(item.get('accounts', []))} ÑÑ.)", f"staff_add:{key}:{sb}")]
        for key, item in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"]))
    ]
    rows.append([_btn("Â« ÐÐ°Ð·Ð°Ð´", "add_start")])
    await callback.message.edit_text(
        f"â <b>ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ: {SB_LABEL.get(sb, '')}</b> â <i>ÑÐ°Ð³ 2 Ð¸Ð· 3</i>

"
        "ÐÑÐ±ÐµÑÐ¸ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ. ÐÑ ÑÐµÐ½Ð° Ð±ÑÐ´ÐµÑ Ñ Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð² Ð¿Ð¾ ÑÐ¼Ð¾Ð»ÑÐ°Ð½Ð¸Ñ.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("staff_add:"))
async def cb_staff_add(callback: CallbackQuery):
    if await _deny(callback):
        return
    parts = callback.data.split(":")
    key, sb = parts[1], (parts[2] if len(parts) > 2 else "n")
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"staff_add:{key}:{sb}"
    await callback.message.edit_text(
        f"â <b>{html.escape(item['title'])}</b> ({SB_LABEL.get(sb, '')}) â <i>ÑÐ°Ð³ 3 Ð¸Ð· 3</i>

"
        "ÐÑÐ¿ÑÐ°Ð²Ñ Ð°ÐºÐºÐ°ÑÐ½ÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸ÐµÐ¼, <b>ÐºÐ°Ð¶Ð´ÑÐ¹ Ñ Ð½Ð¾Ð²Ð¾Ð¹ ÑÑÑÐ¾ÐºÐ¸</b>:
"
        "<code>+8613812342600: Ð¡ÐÐ¡Ð¡ÐÐ¯: 2FA</code>

"
        "ÐÐ¾ Ð¶ÐµÐ»Ð°Ð½Ð¸Ñ Ð¿Ð¾ÑÐ»Ðµ Ð·Ð½Ð°ÐºÐ° <b>|</b>:
"
        "â¢ Ð´Ð°ÑÐ° ÑÐµÐ³Ð¸ÑÑÑÐ°ÑÐ¸Ð¸ â <code>08.08.2026</code>
"
        "â¢ <code>prem</code> â Premium
"
        "â¢ ÑÐ¸ÑÐ»Ð¾ â ÑÐ²Ð¾Ñ ÑÐµÐ½Ð° Ð² â½ (Ð¸Ð½Ð°ÑÐµ ÑÐµÐ½Ð° Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¸)
"
        "ÐÑÐ¸Ð¼ÐµÑ:
<code>+8613812342600: Ð¡ÐÐ¡Ð¡ÐÐ¯: 2FA | 08.08.2026 | 21</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÑÐ¼ÐµÐ½Ð°", panel_back_cb(callback.from_user.id))]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "pos_list")
async def cb_pos_list(callback: CallbackQuery):
    if await _deny(callback):
        return
    uid = callback.from_user.id
    WAITING_INPUT.pop(uid, None)
    rows = [
        [_btn(f"{'ð¦' if v.get('accounts') else 'ð­'} {v['title']} â¢ {v['price_rub']:.0f} â½ ({len(v.get('accounts', []))} ÑÑ.)", f"pos_view:{k}")]
        for k, v in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"]))
    ]
    rows.append([_btn("ð ÐÐ¾Ð²Ð°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ", "owner_new_cat")])
    rows.append([_btn("Â« ÐÐ°Ð·Ð°Ð´", panel_back_cb(uid))])
    text = (
        "ð <b>ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸</b>

ÐÐ°Ð¶Ð¼Ð¸ Ð½Ð° Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ â Ð¼Ð¾Ð¶Ð½Ð¾ Ð´Ð¾Ð±Ð°Ð²Ð¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ, Ð¸Ð·Ð¼ÐµÐ½Ð¸ÑÑ ÑÐµÐ½Ñ, Ð¾ÑÐ¸ÑÑÐ¸ÑÑ Ð¸Ð»Ð¸ ÑÐ´Ð°Ð»Ð¸ÑÑ."
        if CATALOG
        else "ð <b>ÐÐ¾Ð·Ð¸ÑÐ¸Ð¸</b>

ÐÐ¾Ð·Ð¸ÑÐ¸Ð¹ Ð½ÐµÑ. Ð¡Ð¾Ð·Ð´Ð°Ð¹ Ð¿ÐµÑÐ²ÑÑ ÐºÐ½Ð¾Ð¿ÐºÐ¾Ð¹ Ð½Ð¸Ð¶Ðµ."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def show_pos(callback: CallbackQuery, key: str):
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    desc = f"
ð {html.escape(item['description'])}" if item.get("description") else ""
    await callback.message.edit_text(
        f"ð <b>{html.escape(item['title'])}</b>
â
"
        f"ð° Ð¦ÐµÐ½Ð° Ð¿Ð¾ ÑÐ¼Ð¾Ð»ÑÐ°Ð½Ð¸Ñ: <b>{item['price_rub']:.0f} â½</b>
"
        f"ð¦ ÐÐºÐºÐ°ÑÐ½ÑÐ¾Ð²: <b>{len(item.get('accounts', []))}</b>
"
        f"ð ÐÐ»ÑÑ: <code>{html.escape(key)}</code>{desc}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("+ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ", f"pos_add:{key}")],
            [_btn("ð² ÐÐ·Ð¼ÐµÐ½Ð¸ÑÑ ÑÐµÐ½Ñ", f"pos_price:{key}")],
            [_btn("ð§¹ ÐÑÐ¸ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÑ", f"pos_clear:{key}"), _btn("â Ð£Ð´Ð°Ð»Ð¸ÑÑ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ", f"pos_del:{key}")],
            [_btn("Â« Ð Ð¿Ð¾Ð·Ð¸ÑÐ¸ÑÐ¼", "pos_list")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pos_view:"))
async def cb_pos_view(callback: CallbackQuery):
    if await _deny(callback):
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await show_pos(callback, callback.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("pos_add:"))
async def cb_pos_add(callback: CallbackQuery):
    if await _deny(callback):
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    await callback.message.edit_text(
        f"â <b>{html.escape(item['title'])}</b>

ÐÐ°ÐºÐ¸Ðµ Ð°ÐºÐºÐ°ÑÐ½ÑÑ Ð´Ð¾Ð±Ð°Ð²Ð»ÑÐµÑÑ?
"
        "â <b>ÐÐµÐ· Ð¡Ð</b> â ÑÐ¸ÑÑÑÐµ
â ï¸ <b>Ð¡ Ð¡Ð</b> â ÑÐ¾ ÑÐ¿Ð°Ð¼Ð±Ð»Ð¾ÐºÐ¾Ð¼",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("â ÐÐµÐ· Ð¡Ð", f"staff_add:{key}:0"), _btn("â ï¸ Ð¡ Ð¡Ð", f"staff_add:{key}:1")],
            [_btn("â ÐÐµ ÑÐºÐ°Ð·ÑÐ²Ð°ÑÑ", f"staff_add:{key}:n")],
            [_btn("Â« ÐÐ°Ð·Ð°Ð´", f"pos_view:{key}")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pos_price:"))
async def cb_pos_price(callback: CallbackQuery):
    if await _deny(callback):
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"pos_price:{key}"
    await callback.message.edit_text(
        f"ð² <b>{html.escape(item['title'])}</b>

Ð¡ÐµÐ¹ÑÐ°Ñ: <b>{item['price_rub']:.0f} â½</b>
ÐÑÐ¿ÑÐ°Ð²Ñ Ð½Ð¾Ð²ÑÑ ÑÐµÐ½Ñ ÑÐ¸ÑÐ»Ð¾Ð¼:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« ÐÑÐ¼ÐµÐ½Ð°", f"pos_view:{key}")]]),
    )
    await callback.answer()


async def _confirm(callback: CallbackQuery, key: str, kind: str):
    item = CATALOG.get(key)
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    cnt = len(item.get("accounts", []))
    what = (
        f"Ð£Ð´Ð°Ð»Ð¸ÑÑ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ <b>{html.escape(item['title'])}</b> Ð²Ð¼ÐµÑÑÐµ Ñ <b>{cnt}</b> Ð°ÐºÐºÐ°ÑÐ½ÑÐ°Ð¼Ð¸?"
        if kind == "del"
        else f"Ð£Ð´Ð°Ð»Ð¸ÑÑ Ð²ÑÐµ <b>{cnt}</b> Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð² Ð¸Ð· <b>{html.escape(item['title'])}</b>? ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð¾ÑÑÐ°Ð½ÐµÑÑÑ."
    )
    await callback.message.edit_text(
        f"â ï¸ {what}

<b>ÐÑÐ¼ÐµÐ½Ð¸ÑÑ ÑÑÐ¾ Ð½ÐµÐ»ÑÐ·Ñ.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("ÐÐ°, ÑÐ´Ð°Ð»Ð¸ÑÑ", f"pos_{kind}_yes:{key}"), _btn("ÐÐµÑ, Ð½Ð°Ð·Ð°Ð´", f"pos_view:{key}")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pos_del:"))
async def cb_pos_del(callback: CallbackQuery):
    if await _deny(callback):
        return
    await _confirm(callback, callback.data.split(":", 1)[1], "del")


@dp.callback_query(F.data.startswith("pos_clear:"))
async def cb_pos_clear(callback: CallbackQuery):
    if await _deny(callback):
        return
    await _confirm(callback, callback.data.split(":", 1)[1], "clear")


async def _after_delete(callback: CallbackQuery, text: str):
    uid = callback.from_user.id
    await save_data()
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« Ð Ð¿Ð¾Ð·Ð¸ÑÐ¸ÑÐ¼", "pos_list"), _btn("Â« Ð Ð¿Ð°Ð½ÐµÐ»Ñ", panel_back_cb(uid))]]),
    )
    await log_action(callback.bot, uid, who(callback.from_user), text)
    await callback.answer("ÐÐ¾ÑÐ¾Ð²Ð¾")


@dp.callback_query(F.data.startswith("pos_del_yes:"))
async def cb_pos_del_yes(callback: CallbackQuery):
    if await _deny(callback):
        return
    item = CATALOG.pop(callback.data.split(":", 1)[1], None)
    if not item:
        await callback.answer("Ð£Ð¶Ðµ ÑÐ´Ð°Ð»ÐµÐ½Ð°", show_alert=True)
        return
    await _after_delete(callback, f"ðï¸ ÐÐ¾Ð·Ð¸ÑÐ¸Ñ <b>{html.escape(item['title'])}</b> ÑÐ´Ð°Ð»ÐµÐ½Ð° (Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð²: {len(item.get('accounts', []))}).")


@dp.callback_query(F.data.startswith("pos_clear_yes:"))
async def cb_pos_clear_yes(callback: CallbackQuery):
    if await _deny(callback):
        return
    item = CATALOG.get(callback.data.split(":", 1)[1])
    if not item:
        await callback.answer("ÐÐ¾Ð·Ð¸ÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°", show_alert=True)
        return
    n = len(item.get("accounts", []))
    item["accounts"] = []
    await _after_delete(callback, f"ð§¹ Ð Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¸ <b>{html.escape(item['title'])}</b> ÑÐ´Ð°Ð»ÐµÐ½Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ¾Ð²: {n}.")


@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    if not any(len(v.get("accounts", [])) > 0 for v in CATALOG.values()):
        await callback.message.edit_text(
            "ð <b>ÐÐ°ÑÐ°Ð»Ð¾Ð³</b>

Ð¡ÐµÐ¹ÑÐ°Ñ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸ Ð½ÐµÑ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¹.",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "ð <b>ÐÐ°ÑÐ°Ð»Ð¾Ð³</b>

ÐÑ Ð´ÐµÑÑÐ²ÑÑ Ðº Ð´Ð¾ÑÐ¾Ð³Ð¸Ð¼. ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ:",
        parse_mode="HTML",
        reply_markup=catalog_items_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("item_view:"))
async def cb_item_view(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("ÐÐµÑ Ð² Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸", show_alert=True)
        return
    await callback.message.edit_text(
        f"ð¦ <b>{html.escape(item['title'])}</b>

"
        f"{html.escape(item.get('description', ''))}

"
        f"ð° <b>Ð¦ÐµÐ½Ð°:</b> {item['price_rub']:.2f} â½
"
        f"ð <b>Ð Ð½Ð°Ð»Ð¸ÑÐ¸Ð¸:</b> {len(item['accounts'])} ÑÑ.",
        parse_mode="HTML",
        reply_markup=item_view_keyboard(key),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_acc:"))
async def cb_buy_acc(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    uid = callback.from_user.id
    if not check_rate_limit(uid, "buy", 1.2):
        await callback.answer("ÐÐµ ÑÐ°Ðº Ð±ÑÑÑÑÐ¾", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    rec, err = await do_purchase(uid, key)
    if err:
        await callback.answer(err, show_alert=True)
        return
    two_fa_line = f"
ð <b>2FA:</b> <code>{rec['two_fa']}</code>" if rec["two_fa"] else ""
    await callback.message.edit_text(
        f"â <b>ÐÐºÐºÐ°ÑÐ½Ñ Ð²ÑÐ´Ð°Ð½!</b>

"
        f"ð§¾ <b>ÐÐ°ÐºÐ°Ð·:</b> <code>{rec['order_id']}</code>
"
        f"ð¦ <b>ÐÐ¾Ð·Ð¸ÑÐ¸Ñ:</b> {html.escape(rec['title'])}
"
        f"ð± <b>ÐÐ¾Ð¼ÐµÑ:</b> <code>{rec['phone']}</code>{two_fa_line}

"
        "ÐÐ°Ð¶Ð¼Ð¸ <b>Â«ÐÐ¾Ð»ÑÑÐ¸ÑÑ / Ð·Ð°Ð¿ÑÐ¾ÑÐ¸ÑÑ ÐºÐ¾Ð´ Ð²ÑÐ¾Ð´Ð°Â»</b> Ð´Ð»Ñ Ð²ÑÐ¾Ð´Ð°.
"
        "ÐÑÐ·ÑÐ² Ð¼Ð¾Ð¶Ð½Ð¾ Ð¾ÑÑÐ°Ð²Ð¸ÑÑ Ð² Ð¼Ð¸Ð½Ð¸-Ð¿ÑÐ¸Ð»Ð¾Ð¶ÐµÐ½Ð¸Ð¸ Â«ÐÐ¾Ð¸ Ð·Ð°ÐºÐ°Ð·ÑÂ».",
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
        f"ÐÐ°ÐºÐ°Ð·: <code>{rec['order_id']}</code>
"
        f"ÐÐ¾Ð·Ð¸ÑÐ¸Ñ: {html.escape(rec['title'])} â <b>{rec['price_rub']:.2f} â½</b>
"
        f"ÐÐ¾Ð¼ÐµÑ: <code>{rec['phone']}</code>
"
        f"ÐÑÑÐ°ÑÐ¾Ðº: <b>{rec['left']} ÑÑ.</b>",
    )


@dp.callback_query(F.data == "req_code")
async def cb_req_code(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
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
        await save_data()
        await callback.message.answer(
            f"ð <b>ÐÐ¾Ð´ Ð²ÑÐ¾Ð´Ð° Ð´Ð»Ñ</b> <code>{acc['phone']}</code>: <code>{code}</code>",
            parse_mode="HTML",
        )
        await callback.answer("ÐÐ¾Ð´ Ð¿Ð¾Ð»ÑÑÐµÐ½!")
        return
    await callback.message.answer(f"â³ ÐÐ°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¿Ð¾ Ð½Ð¾Ð¼ÐµÑÑ <code>{acc['phone']}</code> Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½.", parse_mode="HTML")
    await callback.answer("ÐÐ°Ð¿ÑÐ¾Ñ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½")
    await notify_owner(
        callback.bot,
        f"ð ÐÐ°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¾Ñ <code>{uid}</code> Ð¿Ð¾ Ð½Ð¾Ð¼ÐµÑÑ <code>{acc['phone']}</code>.
"
        f"ÐÑÐ¿ÑÐ°Ð²Ñ: <code>/code {uid} ÐÐÐ</code>",
    )


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    uid = callback.from_user.id
    role = "ÐÐ»Ð°Ð´ÐµÐ»ÐµÑ" if is_owner(uid) else ("ÐÐ´Ð¼Ð¸Ð½" if is_admin(uid) else "ÐÐ¾ÐºÑÐ¿Ð°ÑÐµÐ»Ñ")
    await callback.message.edit_text(
        f"ð¤ <b>ÐÑÐ¾ÑÐ¸Ð»Ñ</b>

"
        f"Ð Ð¾Ð»Ñ: <b>{role}</b>
"
        f"ID: <code>{uid}</code>
"
        f"ÐÐ°Ð»Ð°Ð½Ñ: <b>{get_rub_balance(uid):.2f} â½</b>
"
        f"ÐÐ¾ÐºÑÐ¿Ð¾Ðº: <b>{len(PURCHASE_HISTORY.get(uid, []))}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_deposit")
async def cb_menu_deposit(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    uid = callback.from_user.id
    await callback.message.edit_text(
        f"ð³ <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° (Ð² â½)</b>

"
        f"ÐÐ°Ñ Ð±Ð°Ð»Ð°Ð½Ñ: <b>{get_rub_balance(uid):.2f} â½</b>
"
        f"ÐÑÑÑÑ: 1 â­ = {RUB_PER_STAR} â½ | 1 TON = {RUB_PER_TON} â½",
        parse_mode="HTML",
        reply_markup=deposit_choice_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dep_method:"))
async def cb_dep_method(callback: CallbackQuery):
    if not await ensure_sub_callback(callback):
        return
    m = callback.data.split(":", 1)[1]
    WAITING_INPUT[callback.from_user.id] = f"dep_amt:{m}"
    if m == "stars":
        text = (
            f"â­ <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð·Ð²ÑÐ·Ð´Ð°Ð¼Ð¸ (Ð² â½)</b>

"
            f"ÐÑÑÑ: <b>1 â­ = {RUB_PER_STAR} â½</b>
"
            "ÐÐ°Ð¿Ð¸ÑÐ¸ ÑÐµÐ»Ð¾Ðµ ÑÐ¸ÑÐ»Ð¾ Ð·Ð²ÑÐ·Ð´ Ð´Ð»Ñ Ð²ÑÑÑÐ°Ð²Ð»ÐµÐ½Ð¸Ñ ÑÑÑÑÐ°:"
        )
    else:
        text = (
            f"ð <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ TON (Ð² â½)</b>

"
            f"ÐÑÑÑ: <b>1 TON = {RUB_PER_TON} â½</b>
"
            f"ÐÐ¾ÑÐµÐ»ÑÐº: <code>{CRYPTO_WALLET}</code>

"
            "ÐÐ°Ð¿Ð¸ÑÐ¸ ÑÑÐ¼Ð¼Ñ Ð² TON â Ð¿ÑÐ¸ÑÐ»Ñ ÑÐµÐºÐ²Ð¸Ð·Ð¸ÑÑ Ð¸ ÐºÐ¾Ð¼Ð¼ÐµÐ½ÑÐ°ÑÐ¸Ð¹ Ð´Ð»Ñ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¾Ð³Ð¾ Ð·Ð°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ñ:"
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_main_keyboard())
    await callback.answer()


@dp.message(F.text)
async def handle_text_inputs(message: Message):
    global REQUIRED_CHANNEL
    uid = message.from_user.id
    txt = (message.text or "").strip()

    if is_admin(uid) and message.reply_to_message and txt and not txt.startswith("/"):
        m = re.search(r"ID:\s*(\d{5,})", message.reply_to_message.text or "")
        if m:
            await support_reply(message.bot, int(m.group(1)), txt)
            await log_action(
                message.bot,
                uid,
                who(message.from_user),
                f"Ð¾ÑÐ²ÐµÑÐ¸Ð» Ð² Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐµ <code>{m.group(1)}</code>: {html.escape(txt[:80])}",
            )
            await message.answer("â ÐÑÐ²ÐµÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ Ð² ÑÐ°Ñ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶ÐºÐ¸")
            return

    wait = WAITING_INPUT.get(uid)
    if not wait:
        return

    if wait == "owner_set_op" and is_owner(uid):
        WAITING_INPUT.pop(uid, None)
        REQUIRED_CHANNEL = txt
        await save_data()
        await message.answer(
            f"â ÐÐ°Ð½Ð°Ð» Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð¾Ð¹ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½:
<code>{html.escape(REQUIRED_CHANNEL)}</code>",
            parse_mode="HTML",
            reply_markup=panel_keyboard(True),
        )
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
        if ton_amt < MIN_TON:
            await message.answer(f"ÐÐ¸Ð½Ð¸Ð¼ÑÐ¼ {MIN_TON} TON.")
            return
        WAITING_INPUT.pop(uid, None)
        rub_est = round(ton_amt * RUB_PER_TON, 2)
        memo = ton_memo_for(uid)
        await save_data()
        await message.answer(
            f"ð <b>ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ TON</b>

"
            f"ÐÐµÑÐµÐ²ÐµÐ´Ð¸ <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} â½</b>) Ð½Ð° ÐºÐ¾ÑÐµÐ»ÑÐº:
<code>{CRYPTO_WALLET}</code>

"
            f"â ï¸ <b>ÐÐ±ÑÐ·Ð°ÑÐµÐ»ÑÐ½Ð¾ ÑÐºÐ°Ð¶Ð¸ ÐºÐ¾Ð¼Ð¼ÐµÐ½ÑÐ°ÑÐ¸Ð¹:</b>
<code>{memo}</code>

"
            "ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¾Ðµ â Ð¾Ð±ÑÑÐ½Ð¾ Ð² ÑÐµÑÐµÐ½Ð¸Ðµ Ð¼Ð¸Ð½ÑÑÑ Ð¿Ð¾ÑÐ»Ðµ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð¸Ñ ÑÐµÑÐ¸.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="ð ÐÑÐºÑÑÑÑ Ð² ÐºÐ¾ÑÐµÐ»ÑÐºÐµ", url=ton_pay_link(ton_amt, memo))],
                [InlineKeyboardButton(text="Â« Ð Ð¼ÐµÐ½Ñ", callback_data="back_main")],
            ]),
        )
        return

    if wait == "staff_check_bal" and is_admin(uid):
        if not txt.isdigit():
            await message.answer("ÐÑÐ¶ÐµÐ½ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ ID.")
            return
        WAITING_INPUT.pop(uid, None)
        target_id = int(txt)
        await log_action(message.bot, uid, who(message.from_user), f"ÑÐ¼Ð¾ÑÑÐµÐ» Ð±Ð°Ð»Ð°Ð½Ñ <code>{target_id}</code>")
        await message.answer(
            f"ð³ ÐÐ°Ð»Ð°Ð½Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{target_id}</code>: <b>{get_rub_balance(target_id):.2f} â½</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait.startswith("staff_add:") and is_admin(uid):
        _, key, *rest = wait.split(":")
        sb_mode = rest[0] if rest else "n"
        item = CATALOG.get(key)
        if not item:
            WAITING_INPUT.pop(uid, None)
            return
        added, notes, countries = 0, [], {}
        for line in txt.splitlines():
            if not line.strip():
                continue
            acc, warn = parse_account_line(line)
            notes.extend(warn)
            if not acc:
                continue
            if "sb" not in acc and sb_mode in ("0", "1"):
                acc["sb"] = sb_mode == "1"
            item.setdefault("accounts", []).append(acc)
            added += 1
            ci = country_info(acc["phone"])
            countries[(ci["iso"], ci["name"])] = countries.get((ci["iso"], ci["name"]), 0) + 1
        WAITING_INPUT.pop(uid, None)
        await save_data()
        summary = " ".join(f"{flag_emoji(i)} {n} x{c}" for (i, n), c in countries.items())
        await log_action(
            message.bot,
            uid,
            who(message.from_user),
            f"Ð´Ð¾Ð±Ð°Ð²Ð¸Ð» <b>{added}</b> Ð°ÐºÐº. Ð² Â«{html.escape(item['title'])}Â» ({SB_LABEL.get(sb_mode, '')})",
        )
        text = f"â ÐÐ¾Ð±Ð°Ð²Ð»ÐµÐ½Ð¾ <b>{added} ÑÑ.</b> Ð² <b>{html.escape(item['title'])}</b> (Ð²ÑÐµÐ³Ð¾: {len(item['accounts'])}).
{summary}"
        if notes:
            text += "
â ï¸ " + html.escape("; ".join(notes[:5]))
        await message.answer(text, parse_mode="HTML", reply_markup=to_panel_kb(uid))
        return

    if wait.startswith("pos_price:") and is_admin(uid):
        key = wait.split(":", 1)[1]
        item = CATALOG.get(key)
        try:
            price = float(txt.replace(",", "."))
            assert price > 0
        except (ValueError, AssertionError):
            await message.answer("ÐÑÐ¶Ð½Ð° ÑÐµÐ½Ð° ÑÐ¸ÑÐ»Ð¾Ð¼ Ð±Ð¾Ð»ÑÑÐµ Ð½ÑÐ»Ñ, Ð½Ð°Ð¿ÑÐ¸Ð¼ÐµÑ 60")
            return
        WAITING_INPUT.pop(uid, None)
        if item:
            old_price = item["price_rub"]
            item["price_rub"] = price
            await save_data()
            await log_action(
                message.bot,
                uid,
                who(message.from_user),
                f"Ð¸Ð·Ð¼ÐµÐ½Ð¸Ð» ÑÐµÐ½Ñ Â«{html.escape(item['title'])}Â»: {old_price:.0f} -> {price:.0f} â½",
            )
            await message.answer(
                f"ð² Ð¦ÐµÐ½Ð° <b>{html.escape(item['title'])}</b> ÑÐµÐ¿ÐµÑÑ <b>{price:.0f} â½</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« Ð Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¸", f"pos_view:{key}")]]),
            )
        return

    if wait == "owner_new_cat" and is_admin(uid):
        parts = [p.strip() for p in txt.split("|", 3)]
        if len(parts) < 3:
            await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐºÐ»ÑÑ | ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ | Ð¦ÐµÐ½Ð°_Ð²_ÑÑÐ±Ð»ÑÑ | ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ</code>", parse_mode="HTML")
            return
        key, title, price_raw = parts[0], parts[1], parts[2]
        desc = parts[3] if len(parts) > 3 else ""
        try:
            price_rub = float(price_raw.replace(",", "."))
        except ValueError:
            await message.answer("Ð¦ÐµÐ½Ð° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ ÑÐ¸ÑÐ»Ð¾Ð¼.")
            return
        WAITING_INPUT.pop(uid, None)
        await log_action(message.bot, uid, who(message.from_user), f"ÑÐ¾Ð·Ð´Ð°Ð» Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ Â«{html.escape(title)}Â» ({price_rub:.0f} â½)")
        CATALOG[key] = {
            "title": title,
            "price_rub": price_rub,
            "description": desc,
            "accounts": CATALOG.get(key, {}).get("accounts", []),
        }
        await save_data()
        await message.answer(
            f"â ÐÐ¾Ð·Ð¸ÑÐ¸Ñ <b>{html.escape(title)}</b> (<code>{html.escape(key)}</code>) ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð° â {price_rub:.0f} â½.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if not is_owner(uid):
        return

    if wait == "promo_new":
        parts = [p.strip() for p in txt.split("|")]
        try:
            code = re.sub(r"[^A-Za-z0-9_-]", "", parts[0]).upper()
            amount = float(parts[1].replace(",", "."))
            limit = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            assert code and amount > 0 and limit >= 0
        except Exception:
            await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: <code>ÐÐÐ | ÑÑÐ¼Ð¼Ð° | Ð»Ð¸Ð¼Ð¸Ñ</code>, Ð½Ð°Ð¿ÑÐ¸Ð¼ÐµÑ <code>START|10|100</code>", parse_mode="HTML")
            return
        WAITING_INPUT.pop(uid, None)
        PROMOS[code] = {"amount": amount, "limit": limit, "used": [], "active": True}
        await save_data()
        await message.answer(
            f"ðï¸ ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´ <code>{code}</code> ÑÐ¾Ð·Ð´Ð°Ð½: +{amount:.0f} â½, Ð»Ð¸Ð¼Ð¸Ñ {limit or 'â'}.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("Â« Ð Ð¿ÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´Ð°Ð¼", "owner_promos")]]),
        )
        return

    if wait == "bcast":
        WAITING_INPUT.pop(uid, None)
        BCAST_PENDING[uid] = message.html_text
        await message.answer(
            f"ð£ <b>ÐÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ</b>

{message.html_text}

ÐÐ¾Ð»ÑÑÐ°ÑÐµÐ»ÐµÐ¹: <b>{len(all_user_ids())}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("â ÐÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð²ÑÐµÐ¼", "bcast_go"), _btn("â ÐÑÐ¼ÐµÐ½Ð°", "bcast_cancel")]]),
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
        tx_add(target_id, "gift", rub_amt, "Ð¾Ñ Ð¼Ð°Ð³Ð°Ð·Ð¸Ð½Ð°")
        new_bal = await add_rub_balance(target_id, rub_amt)
        await message.answer(
            f"ð° ÐÑÐ´Ð°Ð½Ð¾ <b>{rub_amt:.2f} â½</b> Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{target_id}</code>. ÐÐ°Ð»Ð°Ð½Ñ: <b>{new_bal:.2f} â½</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait in ("owner_add_adm", "owner_del_adm"):
        if not txt.isdigit():
            await message.answer("ÐÑÐ¶ÐµÐ½ ÑÐ¸ÑÐ»Ð¾Ð²Ð¾Ð¹ USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        if wait == "owner_add_adm":
            ADMIN_IDS.add(adm_id)
            msg = f"â ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ <code>{adm_id}</code> Ð½Ð°Ð·Ð½Ð°ÑÐµÐ½ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð¼."
        else:
            ADMIN_IDS.discard(adm_id)
            msg = f"â ÐÐ´Ð¼Ð¸Ð½ÐºÐ° Ñ <code>{adm_id}</code> ÑÐ½ÑÑÐ°."
        await save_data()
        await message.answer(msg, parse_mode="HTML", reply_markup=main_menu_keyboard(uid))


# HTTP API Ð´Ð»Ñ Ð¼Ð¸Ð½Ð¸-Ð°Ð¿Ð¿Ð°
def _cors_response(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=CORS_HEADERS)


def auth_user(request: web.Request) -> dict | None:
    user = verify_init_data(request.headers.get("X-Init-Data", ""))
    if user:
        USER_NAMES[int(user["id"])] = display_name(user)
    return user


def _unauth() -> web.Response:
    return _cors_response({"ok": False, "error": "ÐÑÐºÑÐ¾Ð¹ÑÐµ Ð¿ÑÐ¸Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ ÑÐµÑÐµÐ· Telegram"}, status=401)


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def api_options(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def api_health(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers=CORS_HEADERS)


async def api_state(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sp = str(user.get("_sp") or "")
    ref = int(sp[4:]) if sp.startswith("ref_") and sp[4:].isdigit() else None
    await register_user(uid, display_name(user), ref)
    sub_ok = await check_user_subscription(uid)
    return _cors_response(build_state_payload(uid, is_subscribed=sub_ok))


async def _require_sub_api(uid: int) -> web.Response | None:
    if not REQUIRED_CHANNEL:
        return None
    if await check_user_subscription(uid):
        return None
    return _cors_response(
        {"ok": False, "error": "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ¸ÑÐµÑÑ Ð½Ð° Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐ¹ ÐºÐ°Ð½Ð°Ð»", "is_subscribed": False, "required_channel": REQUIRED_CHANNEL},
        status=403,
    )


async def api_buy(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    data = await _json(request)
    item_key = str(data.get("item_key") or "").strip()
    if not item_key:
        return _cors_response({"ok": False, "error": "ÐÐµ ÑÐºÐ°Ð·Ð°Ð½Ð° Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ"}, status=400)
    if not check_rate_limit(uid, "api_buy", 1.2):
        return _cors_response({"ok": False, "error": "Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ ÑÐ°ÑÑÑÐµ Ð·Ð°Ð¿ÑÐ¾ÑÑ"}, status=429)
    acc_id = str(data.get("acc_id") or "").strip() or None
    rec, err = await do_purchase(uid, item_key, acc_id)
    if err:
        return _cors_response({"ok": False, "error": err}, status=400)
    if bot:
        await notify_owner(
            bot,
            f"ð <b>MiniApp Ð¿Ð¾ÐºÑÐ¿ÐºÐ°</b>
{html.escape(display_name(user))} (<code>{uid}</code>)
"
            f"{html.escape(rec['title'])} â <code>{rec['order_id']}</code> â <b>{rec['price_rub']:.2f} â½</b>
"
            f"<code>{rec['phone']}</code>
ÐÑÑÐ°ÑÐ¾Ðº: {rec['left']} ÑÑ.",
        )
    return _cors_response({
        "ok": True,
        "order_id": rec["order_id"],
        "phone": rec["phone"],
        "two_fa": rec["two_fa"],
        "balance_rub": get_rub_balance(uid),
    })


async def api_deposit(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    data = await _json(request)
    method = str(data.get("method") or "").strip().lower()
    try:
        amount = float(str(data.get("amount") or "0").replace(",", "."))
    except (ValueError, TypeError):
        amount = 0.0
    if amount <= 0 or method not in ("stars", "ton"):
        return _cors_response({"ok": False, "error": "ÐÑÐ¾Ð²ÐµÑÑÑÐµ ÑÑÐ¼Ð¼Ñ Ð¸ ÑÐ¿Ð¾ÑÐ¾Ð± Ð¾Ð¿Ð»Ð°ÑÑ"}, status=400)
    if not check_rate_limit(uid, "api_dep", 1.0):
        return _cors_response({"ok": False, "error": "ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ÑÐµ ÑÐµÐºÑÐ½Ð´Ñ Ð¸ Ð¿Ð¾Ð¿ÑÐ¾Ð±ÑÐ¹ÑÐµ ÑÐ½Ð¾Ð²Ð°"}, status=429)
    if method == "stars":
        stars_cnt = max(1, int(round(amount)))
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        if not bot:
            return _cors_response({"ok": False, "error": "BOT_TOKEN Ð½Ðµ Ð½Ð°ÑÑÑÐ¾ÐµÐ½ Ð½Ð° ÑÐµÑÐ²ÐµÑÐµ"}, status=500)
        try:
            link = await bot.create_invoice_link(
                title=f"ÐÐ¾Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ðµ Ð±Ð°Ð»Ð°Ð½ÑÐ° ({rub_preview:.2f} â½)",
                description=f"ÐÐ°ÑÐ¸ÑÐ»ÐµÐ½Ð¸Ðµ {rub_preview:.2f} â½ Ð·Ð° {stars_cnt} â­",
                payload=f"rub_stars:{uid}:{stars_cnt}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{stars_cnt} â­", amount=stars_cnt)],
            )
            return _cors_response({"ok": True, "invoice_link": link})
        except Exception as e:
            return _cors_response({"ok": False, "error": f"Telegram Stars: {e}"}, status=400)
    if amount < MIN_TON:
        return _cors_response({"ok": False, "error": f"ÐÐ¸Ð½Ð¸Ð¼ÑÐ¼ {MIN_TON} TON"}, status=400)
    memo = ton_memo_for(uid)
    await save_data()
    return _cors_response({
        "ok": True,
        "wallet": CRYPTO_WALLET,
        "memo": memo,
        "pay_link": ton_pay_link(amount, memo),
        "estimated_rub": round(amount * RUB_PER_TON, 2),
    })


async def api_code(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    data = await _json(request)
    order_id = str(data.get("order_id") or "").strip()
    if not check_rate_limit(uid, "api_code", 2.0):
        return _cors_response({"ok": False, "error": "Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ ÑÐ°ÑÑÑÐµ Ð·Ð°Ð¿ÑÐ¾ÑÑ ÐºÐ¾Ð´Ð°"}, status=429)
    target = None
    for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
        if isinstance(rec, dict) and (not order_id or rec.get("order_id") == order_id):
            target = rec
            break
    if not target:
        return _cors_response({"ok": False, "error": "ÐÐ°ÐºÐ°Ð· Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½"}, status=404)
    if target.get("code"):
        return _cors_response({"ok": True, "code": target["code"]})
    fetched = await fetch_tg_login_code(target.get("session_string", ""))
    if fetched:
        target["code"] = fetched
        await save_data()
        return _cors_response({"ok": True, "code": fetched})
    if bot and not target.get("code_requested"):
        target["code_requested"] = True
        await save_data()
        await notify_owner(
            bot,
            f"ð MiniApp Ð·Ð°Ð¿ÑÐ¾Ñ ÐºÐ¾Ð´Ð° Ð¾Ñ <code>{uid}</code> (<code>{target.get('phone')}</code>).
"
            f"ÐÑÐ¿ÑÐ°Ð²Ñ: <code>/code {uid} ÐÐÐ</code>",
        )
    return _cors_response({"ok": False, "error": "ÐÐ°Ð¿ÑÐ¾Ñ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½, ÐºÐ¾Ð´ ÑÐºÐ¾ÑÐ¾ Ð¿ÑÐ¸Ð´ÑÑ"})


async def api_review(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    data = await _json(request)
    order_id = str(data.get("order_id") or "").strip()
    text = str(data.get("text") or "").strip()[:500]
    try:
        rating = int(data.get("rating") or 0)
    except (ValueError, TypeError):
        rating = 0
    if rating < 1 or rating > 5:
        return _cors_response({"ok": False, "error": "ÐÐ¾ÑÑÐ°Ð²ÑÑÐµ Ð¾ÑÐµÐ½ÐºÑ Ð¾Ñ 1 Ð´Ð¾ 5"}, status=400)
    if not check_rate_limit(uid, "api_review", 2.0):
        return _cors_response({"ok": False, "error": "ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ÑÐµ Ð¿Ð°ÑÑ ÑÐµÐºÑÐ½Ð´"}, status=429)
    rec = next((r for r in PURCHASE_HISTORY.get(uid, []) if isinstance(r, dict) and r.get("order_id") == order_id), None)
    if not rec:
        return _cors_response({"ok": False, "error": "ÐÑÐ·ÑÐ² Ð¼Ð¾Ð¶Ð½Ð¾ Ð¾ÑÑÐ°Ð²Ð¸ÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð¿Ð¾ÑÐ»Ðµ Ð¿Ð¾ÐºÑÐ¿ÐºÐ¸"}, status=403)
    if rec.get("reviewed"):
        return _cors_response({"ok": False, "error": "ÐÑ ÑÐ¶Ðµ Ð¾ÑÑÐ°Ð²Ð¸Ð»Ð¸ Ð¾ÑÐ·ÑÐ² Ð½Ð° ÑÑÐ¾Ñ Ð·Ð°ÐºÐ°Ð·"}, status=400)
    rec["reviewed"] = True
    REVIEWS.insert(0, {
        "id": secrets.token_hex(4),
        "user_id": uid,
        "order_id": order_id,
        "name": display_name(user),
        "rating": rating,
        "text": text,
        "ts": int(time.time()),
    })
    await save_data()
    if bot:
        await notify_owner(
            bot,
            f"â­ <b>ÐÐ¾Ð²ÑÐ¹ Ð¾ÑÐ·ÑÐ²</b> {'â' * rating}{'â' * (5 - rating)}
"
            f"{html.escape(display_name(user))} (<code>{uid}</code>)
{html.escape(text) or 'Ð±ÐµÐ· ÐºÐ¾Ð¼Ð¼ÐµÐ½ÑÐ°ÑÐ¸Ñ'}",
        )
    return _cors_response({"ok": True, "reviews": reviews_payload(uid)})


async def api_support_send(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    data = await _json(request)
    text = str(data.get("text") or "").strip()[:1000]
    if not text:
        return _cors_response({"ok": False, "error": "ÐÑÑÑÐ¾Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ"}, status=400)
    if not check_rate_limit(uid, "api_support", 1.0):
        return _cors_response({"ok": False, "error": "ÐÐµ ÑÐ°Ðº Ð±ÑÑÑÑÐ¾"}, status=429)
    SUPPORT.setdefault(uid, []).append({"from": "user", "text": text, "ts": int(time.time())})
    del SUPPORT[uid][:-200]
    await save_data()
    if bot:
        await notify_staff(
            bot,
            f"ð¬ <b>ÐÐ¾Ð´Ð´ÐµÑÐ¶ÐºÐ°</b> {html.escape(display_name(user))}
"
            f"ID: <code>{uid}</code>

{html.escape(text)}

"
            "<i>ÐÑÐ²ÐµÑÑ ÑÐµÐ¿Ð»Ð°ÐµÐ¼ Ð½Ð° ÑÑÐ¾ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ.</i>",
        )
    return _cors_response({"ok": True, "messages": SUPPORT[uid]})


async def api_support_messages(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    return _cors_response({"ok": True, "messages": SUPPORT.get(int(user["id"]), [])})


async def api_roulette(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    if LAST_SPIN.get(uid) == roulette_day():
        return _cors_response({"ok": False, "error": "Ð¡ÐµÐ³Ð¾Ð´Ð½Ñ Ð²Ñ ÑÐ¶Ðµ ÐºÑÑÑÐ¸Ð»Ð¸", "roulette": roulette_status(uid)}, status=400)
    LAST_SPIN[uid] = roulette_day()
    prize = _rng.choices(ROULETTE_PRIZES, weights=ROULETTE_WEIGHTS)[0]
    tx_add(uid, "roulette", prize, "ÑÑÐ»ÐµÑÐºÐ°")
    new_bal = await add_rub_balance(uid, prize)
    return _cors_response({"ok": True, "prize": prize, "balance_rub": new_bal, "roulette": roulette_status(uid)})


async def api_review_edit(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    rev = next((r for r in REVIEWS if r.get("id") == str(data.get("review_id") or "")), None)
    if not rev or rev.get("user_id") != uid:
        return _cors_response({"ok": False, "error": "Ð ÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°ÑÑ Ð¼Ð¾Ð¶Ð½Ð¾ ÑÐ¾Ð»ÑÐºÐ¾ ÑÐ²Ð¾Ð¹ Ð¾ÑÐ·ÑÐ²"}, status=403)
    try:
        rating = int(data.get("rating") or 0)
    except (ValueError, TypeError):
        rating = 0
    if rating < 1 or rating > 5:
        return _cors_response({"ok": False, "error": "ÐÐ¾ÑÑÐ°Ð²ÑÑÐµ Ð¾ÑÐµÐ½ÐºÑ Ð¾Ñ 1 Ð´Ð¾ 5"}, status=400)
    rev["rating"] = rating
    rev["text"] = str(data.get("text") or "").strip()[:500]
    rev["edited"] = True
    await save_data()
    return _cors_response({"ok": True, "reviews": reviews_payload(uid)})


async def api_review_delete(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    if not is_admin(uid):
        return _cors_response({"ok": False, "error": "Ð£Ð´Ð°Ð»ÑÑÑ Ð¾ÑÐ·ÑÐ²Ñ Ð¼Ð¾Ð¶ÐµÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð²Ð»Ð°Ð´ÐµÐ»ÐµÑ Ð¸Ð»Ð¸ Ð°Ð´Ð¼Ð¸Ð½"}, status=403)
    data = await _json(request)
    rev = next((r for r in REVIEWS if r.get("id") == str(data.get("review_id") or "")), None)
    if not rev:
        return _cors_response({"ok": False, "error": "ÐÑÐ·ÑÐ² Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½"}, status=404)
    REVIEWS.remove(rev)
    await save_data()
    await log_action(
        bot,
        uid,
        display_name(user),
        f"ÑÐ´Ð°Ð»Ð¸Ð» Ð¾ÑÐ·ÑÐ² {html.escape(rev['name'])} ({rev['rating']}â): {html.escape(rev['text'][:60]) or 'Ð±ÐµÐ· ÑÐµÐºÑÑÐ°'}",
    )
    return _cors_response({"ok": True, "reviews": reviews_payload(uid)})


async def api_order_finish(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    rec = next((r for r in PURCHASE_HISTORY.get(uid, []) if isinstance(r, dict) and r.get("order_id") == str(data.get("order_id") or "")), None)
    if not rec:
        return _cors_response({"ok": False, "error": "ÐÐ°ÐºÐ°Ð· Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½"}, status=404)
    rec["finished"] = True
    await save_data()
    return _cors_response({"ok": True})


async def api_promo(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    sub_err = await _require_sub_api(uid)
    if sub_err:
        return sub_err
    if not check_rate_limit(uid, "promo", 2.0):
        return _cors_response({"ok": False, "error": "ÐÐµ ÑÐ°Ðº Ð±ÑÑÑÑÐ¾"}, status=429)
    data = await _json(request)
    code = str(data.get("code") or "").strip().upper()
    p = PROMOS.get(code)
    if not p or not p.get("active", True):
        return _cors_response({"ok": False, "error": "ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½"}, status=404)
    if uid in p["used"]:
        return _cors_response({"ok": False, "error": "ÐÑ ÑÐ¶Ðµ Ð¸ÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°Ð»Ð¸ ÑÑÐ¾Ñ Ð¿ÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´"}, status=400)
    if p.get("limit") and len(p["used"]) >= p["limit"]:
        return _cors_response({"ok": False, "error": "ÐÑÐ¾Ð¼Ð¾ÐºÐ¾Ð´ Ð·Ð°ÐºÐ¾Ð½ÑÐ¸Ð»ÑÑ"}, status=400)
    p["used"].append(uid)
    tx_add(uid, "promo", p["amount"], code)
    new_bal = await add_rub_balance(uid, p["amount"])
    return _cors_response({"ok": True, "amount": p["amount"], "balance_rub": new_bal})


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", api_health)
    routes_get = {"/api/state": api_state, "/api/support/messages": api_support_messages}
    routes_post = {
        "/api/buy": api_buy,
        "/api/deposit": api_deposit,
        "/api/code": api_code,
        "/api/review": api_review,
        "/api/roulette": api_roulette,
        "/api/review/edit": api_review_edit,
        "/api/review/delete": api_review_delete,
        "/api/order/finish": api_order_finish,
        "/api/promo": api_promo,
        "/api/support/send": api_support_send,
    }
    for path in [*routes_get, *routes_post]:
        app.router.add_options(path, api_options)
    for path, h in routes_get.items():
        app.router.add_get(path, h)
    for path, h in routes_post.items():
        app.router.add_post(path, h)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    global BOT_USERNAME
    await init_db()
    await load_data()
    if bot:
        try:
            BOT_USERNAME = (await bot.get_me()).username or ""
        except Exception as e:
            print(f"get_me error: {e}")
    runner = await start_http_server()
    ton_task = asyncio.create_task(ton_poll_loop(bot))
    try:
        if bot:
            await dp.start_polling(bot)
    finally:
        ton_task.cancel()
        await runner.cleanup()
        if DB_POOL:
            await DB_POOL.close()


if __name__ == "__main__":
    asyncio.run(main())