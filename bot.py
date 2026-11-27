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
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()   # желательно (бесплатно у @tonapibot)
MIN_TON = float(os.getenv("MIN_TON", "0.1") or "0.1")

CATALOG: dict[str, dict] = {}
ADMIN_IDS: set[int] = set()
WAITING_INPUT: dict[int, str] = {}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[dict]] = {}
USER_RUB_BALANCE: dict[int, float] = {}
LAST_API_ACTION: dict[tuple[int, str], float] = {}
TOTAL_SOLD_COUNT: int = 0
RECENT_SALES: list[dict] = []          # бегущая строка: последние продажи
REVIEWS: list[dict] = []               # отзывы (новые в начале)
SUPPORT: dict[int, list[dict]] = {}    # чат поддержки: user_id -> сообщения
PENDING_TON: dict[str, dict] = {}      # комментарий платежа -> {uid, ts}
PROCESSED_TX: list[str] = []           # уже зачисленные транзакции TON
LAST_SPIN: dict[int, int] = {}         # рулетка: user_id -> номер дня (МСК) последней крутки
ADMIN_LOG: list[dict] = []           # журнал действий админов (видит владелец)
KNOWN_USERS: set[int] = set()          # все, кто открывал бота / мини-апп (для рассылки)
USER_NAMES: dict[int, str] = {}        # имена для таблицы лидеров
TX_LOG: dict[int, list[dict]] = {}     # история баланса: user_id -> операции
DEPOSITED: dict[int, float] = {}       # сколько человек пополнил (для лидеров)
REF_OF: dict[int, int] = {}            # друг -> кто пригласил
REF_PAID: list[int] = []               # друзья, за которых бонус уже выплачен
REF_EARNED: dict[int, float] = {}      # сколько заработал на рефералах
PROMOS: dict[str, dict] = {}           # промокоды
BCAST_PENDING: dict[int, str] = {}     # текст рассылки до подтверждения
BOT_USERNAME = ""
REF_BONUS = float(os.getenv("REF_BONUS", "1") or "1")            # ₽ за одного друга
REF_TRIGGER = os.getenv("REF_TRIGGER", "purchase")               # purchase — за первую покупку друга, join — за переход по ссылке
ROULETTE_PRIZES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]      # призы в ₽
ROULETTE_WEIGHTS = [28, 20, 15, 11, 8, 6, 5, 4, 2, 1]  # шансы: чем больше число, тем чаще выпадает
MSK_OFFSET = 3 * 3600
_rng = random.SystemRandom()

DB_POOL: asyncpg.Pool | None = None
_SAVE_LOCK = asyncio.Lock()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Init-Data",
}


# ───────────────────────── БД ─────────────────────────

async def init_db() -> None:
    global DB_POOL
    if not DATABASE_URL:
        print("ВНИМАНИЕ: DATABASE_URL не задан — данные не будут сохраняться в PostgreSQL.")
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
    global CATALOG, ADMIN_IDS, PURCHASE_HISTORY, USER_RUB_BALANCE, TOTAL_SOLD_COUNT
    global RECENT_SALES, REVIEWS, SUPPORT, PENDING_TON, PROCESSED_TX, LAST_SPIN, ADMIN_LOG
    global KNOWN_USERS, USER_NAMES, TX_LOG, DEPOSITED, REF_OF, REF_PAID, REF_EARNED, PROMOS
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
            KNOWN_USERS = set(int(x) for x in data.get("known_users", [])) | set(PURCHASE_HISTORY) | set(USER_RUB_BALANCE) | set(SUPPORT)
            ensure_account_ids()
    except Exception as e:
        print(f"Ошибка при загрузке данных из PostgreSQL: {e}")


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
        print(f"Ошибка при сохранении данных в PostgreSQL: {e}")


# ───────────────────────── Утилиты ─────────────────────────

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
    TX_LOG.setdefault(uid, []).append({"ts": int(time.time()), "kind": kind, "amount": round(float(amount), 2), "note": note})
    del TX_LOG[uid][:-100]
    if kind.startswith("dep"):
        DEPOSITED[uid] = round(DEPOSITED.get(uid, 0.0) + float(amount), 2)


async def ref_try_pay(friend: int, event: str) -> None:
    inviter = REF_OF.get(friend)
    if not inviter or friend in REF_PAID or REF_TRIGGER != event:
        return
    REF_PAID.append(friend)
    REF_EARNED[inviter] = round(REF_EARNED.get(inviter, 0.0) + REF_BONUS, 2)
    tx_add(inviter, "ref", REF_BONUS, f"друг {USER_NAMES.get(friend, friend)}")
    await add_rub_balance(inviter, REF_BONUS)
    if bot:
        try:
            await bot.send_message(inviter, f"🎁 <b>+{REF_BONUS:.0f} ₽</b> на баланс — твой друг {html.escape(str(USER_NAMES.get(friend, '')))} {'купил аккаунт' if event == 'purchase' else 'зашёл по твоей ссылке'}!", parse_mode="HTML")
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
            buyers.append({"uid": u, "name": USER_NAMES.get(u, "Покупатель"), "buys": len(rr),
                           "spent": round(sum(float(r.get("price_rub") or 0) for r in rr), 2)})
    buyers.sort(key=lambda x: (-x["buys"], -x["spent"]))
    donors = sorted(({"uid": u, "name": USER_NAMES.get(u, "Покупатель"), "dep": v} for u, v in DEPOSITED.items() if v > 0),
                    key=lambda x: -x["dep"])

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
        return sum(e["amount"] for v in TX_LOG.values() for e in v if e["kind"].startswith("dep") and e["ts"] >= since)

    lines = ["📈 <b>Статистика</b>", "━━━━━━━━━━━━━━━"]
    for title, secs in (("24 часа", 86400), ("7 дней", 7 * 86400), ("30 дней", 30 * 86400)):
        n, rev = sales(now - secs)
        lines.append(f"<b>{title}</b>: продаж {n} • выручка {rev:.0f} ₽ • пополнений {deps(now - secs):.0f} ₽")
    n_all, rev_all = len(recs), sum(float(r.get("price_rub") or 0) for r in recs)
    lines.append(f"<b>Всего</b>: продаж {n_all} • выручка {rev_all:.0f} ₽ • пополнений {sum(DEPOSITED.values()):.0f} ₽")
    stock = sum(len(v.get("accounts", [])) for v in CATALOG.values())
    stock_val = sum(float(a.get("price_rub") or v["price_rub"]) for v in CATALOG.values() for a in v.get("accounts", []))
    bal = sum(b for u, b in USER_RUB_BALANCE.items() if u != OWNER_ID)
    lines += ["", f"👥 Пользователей: <b>{len(KNOWN_USERS)}</b> • покупали: <b>{sum(1 for v in PURCHASE_HISTORY.values() if v)}</b>",
              f"💼 Баланс у пользователей: <b>{bal:.0f} ₽</b>",
              f"📦 На складе: <b>{stock}</b> шт. на <b>{stock_val:.0f} ₽</b>"]
    top_c = Counter(country_info(r.get("phone", ""))["name"] for r in recs).most_common(5)
    if top_c:
        lines += ["", "🌍 <b>Топ стран по продажам</b>", "  ".join(f"{n} ×{c}" for n, c in top_c)]
    lb = leaders_payload(0)["buyers"][:3]
    if lb:
        lines += ["", "🏆 <b>Топ покупателей</b>"] + [f"{i + 1}. {html.escape(x['name'])} — {x['buys']} шт. • {x['spent']:.0f} ₽" for i, x in enumerate(lb)]
    lines += ["", f"🎁 Рефералов: <b>{len(REF_OF)}</b> (выплачено {sum(REF_EARNED.values()):.0f} ₽)"]
    return "\n".join(lines)


async def _run_broadcast(bot_instance: Bot, owner_id: int, text: str) -> None:
    ok = fail = 0
    for u in all_user_ids():
        try:
            await bot_instance.send_message(u, text, parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)
    await bot_instance.send_message(owner_id, f"📢 <b>Рассылка завершена</b>\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}", parse_mode="HTML")


def who(user) -> str:
    if not user:
        return "?"
    return f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "first_name", None) or str(user.id))


async def log_action(bot_instance: Bot | None, actor_id: int, actor_name: str, text: str) -> None:
    """Действия админов (не владельца) пишутся в журнал и сразу приходят владельцу."""
    if is_owner(actor_id):
        return
    ADMIN_LOG.append({"ts": int(time.time()), "uid": actor_id, "name": actor_name, "text": text})
    del ADMIN_LOG[:-300]
    await save_data()
    if bot_instance:
        await notify_owner(bot_instance, f"🛡 <b>Действие админа</b>\n{html.escape(actor_name)} (<code>{actor_id}</code>)\n{text}")


def verify_init_data(init_data: str, max_age: int = 3 * 86400) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает dict пользователя или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
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
    return (user.get("first_name") or "Покупатель")[:24]


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
            # берём только свежий код (не старше max_age сек), чтобы не выдать старый
            if msg.date and (datetime.now(timezone.utc) - msg.date).total_seconds() > max_age:
                continue
            match = re.search(r"\b(\d{5})\b", msg.message or "")
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
    "1": ("us", "США"), "7": ("ru", "Россия"), "20": ("eg", "Египет"), "27": ("za", "ЮАР"), "30": ("gr", "Греция"),
    "31": ("nl", "Нидерланды"), "32": ("be", "Бельгия"), "33": ("fr", "Франция"), "34": ("es", "Испания"),
    "36": ("hu", "Венгрия"), "39": ("it", "Италия"), "40": ("ro", "Румыния"), "41": ("ch", "Швейцария"),
    "43": ("at", "Австрия"), "44": ("gb", "Великобритания"), "45": ("dk", "Дания"), "46": ("se", "Швеция"),
    "47": ("no", "Норвегия"), "48": ("pl", "Польша"), "49": ("de", "Германия"), "51": ("pe", "Перу"),
    "52": ("mx", "Мексика"), "54": ("ar", "Аргентина"), "55": ("br", "Бразилия"), "56": ("cl", "Чили"),
    "57": ("co", "Колумбия"), "58": ("ve", "Венесуэла"), "60": ("my", "Малайзия"), "61": ("au", "Австралия"),
    "62": ("id", "Индонезия"), "63": ("ph", "Филиппины"), "64": ("nz", "Новая Зеландия"), "65": ("sg", "Сингапур"),
    "66": ("th", "Таиланд"), "81": ("jp", "Япония"), "82": ("kr", "Южная Корея"), "84": ("vn", "Вьетнам"),
    "86": ("cn", "Китай"), "90": ("tr", "Турция"), "91": ("in", "Индия"), "92": ("pk", "Пакистан"),
    "93": ("af", "Афганистан"), "94": ("lk", "Шри-Ланка"), "95": ("mm", "Мьянма"), "98": ("ir", "Иран"),
    "212": ("ma", "Марокко"), "213": ("dz", "Алжир"), "216": ("tn", "Тунис"), "234": ("ng", "Нигерия"),
    "254": ("ke", "Кения"), "351": ("pt", "Португалия"), "352": ("lu", "Люксембург"), "353": ("ie", "Ирландия"),
    "358": ("fi", "Финляндия"), "359": ("bg", "Болгария"), "370": ("lt", "Литва"), "371": ("lv", "Латвия"),
    "372": ("ee", "Эстония"), "373": ("md", "Молдова"), "374": ("am", "Армения"), "375": ("by", "Беларусь"),
    "380": ("ua", "Украина"), "381": ("rs", "Сербия"), "385": ("hr", "Хорватия"), "386": ("si", "Словения"),
    "420": ("cz", "Чехия"), "421": ("sk", "Словакия"), "852": ("hk", "Гонконг"), "855": ("kh", "Камбоджа"),
    "856": ("la", "Лаос"), "880": ("bd", "Бангладеш"), "886": ("tw", "Тайвань"), "961": ("lb", "Ливан"),
    "962": ("jo", "Иордания"), "964": ("iq", "Ирак"), "965": ("kw", "Кувейт"), "966": ("sa", "Саудовская Аравия"),
    "971": ("ae", "ОАЭ"), "972": ("il", "Израиль"), "974": ("qa", "Катар"), "976": ("mn", "Монголия"),
    "977": ("np", "Непал"), "992": ("tj", "Таджикистан"), "993": ("tm", "Туркменистан"), "994": ("az", "Азербайджан"),
    "995": ("ge", "Грузия"), "996": ("kg", "Киргизия"), "998": ("uz", "Узбекистан"),
}
COUNTRIES.update({
    "211": ("ss", "Южный Судан"), "218": ("ly", "Ливия"), "220": ("gm", "Гамбия"), "221": ("sn", "Сенегал"),
    "225": ("ci", "Кот-д'Ивуар"), "231": ("lr", "Либерия"), "232": ("sl", "Сьерра-Леоне"), "233": ("gh", "Гана"),
    "235": ("td", "Чад"), "237": ("cm", "Камерун"), "241": ("ga", "Габон"), "242": ("cg", "Конго"),
    "243": ("cd", "ДР Конго"), "244": ("ao", "Ангола"), "249": ("sd", "Судан"), "250": ("rw", "Руанда"),
    "251": ("et", "Эфиопия"), "255": ("tz", "Танзания"), "256": ("ug", "Уганда"), "258": ("mz", "Мозамбик"),
    "260": ("zm", "Замбия"), "261": ("mg", "Мадагаскар"), "263": ("zw", "Зимбабве"), "264": ("na", "Намибия"),
    "267": ("bw", "Ботсвана"), "355": ("al", "Албания"), "356": ("mt", "Мальта"), "357": ("cy", "Кипр"),
    "382": ("me", "Черногория"), "387": ("ba", "Босния и Герцеговина"), "389": ("mk", "Северная Македония"),
    "502": ("gt", "Гватемала"), "503": ("sv", "Сальвадор"), "504": ("hn", "Гондурас"), "505": ("ni", "Никарагуа"),
    "506": ("cr", "Коста-Рика"), "507": ("pa", "Панама"), "509": ("ht", "Гаити"), "591": ("bo", "Боливия"),
    "592": ("gy", "Гайана"), "593": ("ec", "Эквадор"), "595": ("py", "Парагвай"), "598": ("uy", "Уругвай"),
    "673": ("bn", "Бруней"), "853": ("mo", "Макао"), "960": ("mv", "Мальдивы"), "963": ("sy", "Сирия"),
    "967": ("ye", "Йемен"), "968": ("om", "Оман"), "973": ("bh", "Бахрейн"), "975": ("bt", "Бутан"),
})
PRICE_SPLIT = float(os.getenv("PRICE_SPLIT", "150") or "150")   # до этой цены — «дешёвые», выше — «дорогие» (распределяется само)
GUARANTEE_HOURS = int(os.getenv("GUARANTEE_HOURS", "0") or "0")   # 0 — строка «Гарантия» не показывается


def country_info(phone: str) -> dict:
    """Страна и флаг определяются по коду номера."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("7") and len(d) > 1 and d[1] in "67":
        return {"iso": "kz", "name": "Казахстан", "dial": "7"}
    for ln in (3, 2, 1):
        if d[:ln] in COUNTRIES:
            iso, name = COUNTRIES[d[:ln]]
            return {"iso": iso, "name": name, "dial": d[:ln]}
    return {"iso": "", "name": "Другая страна", "dial": d[:2]}


def flag_emoji(iso: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 97) for c in iso.lower()) if iso else "🌐"


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
    """+номер[:сессия[:2FA]] | дата | сб / без сб | prem | цена"""
    warn: list[str] = []
    main, *meta = [p.strip() for p in line.split("|")]
    phone, _, secret = main.partition(":")
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None, [f"не понял номер: {line[:25]}"]
    acc: dict = {"id": secrets.token_hex(3), "phone": "+" + digits, "secret": secret.strip()}
    for tok in meta:
        t = tok.lower().strip()
        if not t:
            continue
        iso_date = _parse_date(t)
        if iso_date:
            acc["reg"] = iso_date
        elif t in ("без сб", "без спамблока", "нет сб", "чистый", "nosb", "clean"):
            acc["sb"] = False
        elif t in ("сб", "спамблок", "sb", "spam", "spamblock"):
            acc["sb"] = True
        elif t in ("prem", "premium", "прем", "премиум"):
            acc["premium"] = True
        elif re.fullmatch(r"\d+([.,]\d+)?", t):
            acc["price_rub"] = float(t.replace(",", "."))
        else:
            warn.append(f"«{tok[:15]}» пропущено")
    return acc, warn


def lot_view(key: str, item: dict, acc: dict) -> dict:
    digits = re.sub(r"\D", "", acc.get("phone", ""))
    session, _ = _parse_account_secret(acc.get("secret", ""))
    return {
        "id": acc.get("id", ""),
        "key": key,
        "title": item["title"],
        "mask": f"+{digits[:3]}…{digits[-4:]}" if len(digits) > 8 else f"+{digits[:2]}…{digits[-2:]}",
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
    """Единая покупка для бота и мини-аппа. Баланс списывается С КОНКРЕТНОГО пользователя."""
    global TOTAL_SOLD_COUNT
    item = CATALOG.get(key)
    accounts = item.get("accounts", []) if item else []
    if not accounts:
        return None, "Нет в наличии"
    if acc_id:
        idx = next((i for i, a in enumerate(accounts) if a.get("id") == acc_id), None)
        if idx is None:
            return None, "Этот аккаунт уже продан"
    else:
        idx = 0
    acc = accounts[idx]
    price = round(float(acc.get("price_rub") or item["price_rub"]), 2)
    if get_rub_balance(uid) < price:
        return None, f"Недостаточно средств ({price:.2f} ₽). Пополни баланс!"

    # всё синхронно до await — гонок между покупателями нет
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
    finished = o["finished"] if "finished" in o else bool(o.get("code"))   # старые заказы с кодом считались завершёнными
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
            {"id": r.get("id", ""), "name": r["name"], "rating": r["rating"], "text": r["text"], "ts": r["ts"],
             "edited": bool(r.get("edited")), "mine": r.get("user_id") == uid}
            for r in REVIEWS[:60]
        ],
    }


def build_state_payload(user_id: int) -> dict:
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
            text=f"💬 <b>Ответ поддержки:</b>\n\n{html.escape(text)}\n\n<i>Продолжить диалог можно в мини-приложении → чат.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ───────────────────────── Рулетка (раз в день, сброс в 00:00 МСК) ─────────────────────────

def roulette_day() -> int:
    return int((time.time() + MSK_OFFSET) // 86400)


def roulette_status(uid: int) -> dict:
    today = roulette_day()
    next_in = int((today + 1) * 86400 - (time.time() + MSK_OFFSET))
    return {"available": LAST_SPIN.get(uid) != today, "next_in": max(0, next_in), "prizes": ROULETTE_PRIZES}


# ───────────────────────── Автопроверка TON ─────────────────────────

def ton_memo_for(uid: int) -> str:
    """Уникальный комментарий платежа. По нему находим, кому зачислить."""
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

    for tx in reversed(data.get("result", [])):          # от старых к новым
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

        PROCESSED_TX.append(key)                         # помечаем сразу — двойного зачисления не будет
        del PROCESSED_TX[:-1000]
        uid = int(pend["uid"])
        ton = value / 1e9
        if ton < MIN_TON:
            await save_data()
            if bot_instance:
                await notify_owner(bot_instance, f"⚠️ TON ниже минимума ({ton:.4f}) от <code>{uid}</code> — не зачислено автоматически.")
            continue
        rub = round(ton * RUB_PER_TON, 2)
        tx_add(uid, "dep_ton", rub, f"{ton:.4f} TON")
        new_bal = await add_rub_balance(uid, rub)
        if bot_instance:
            try:
                await bot_instance.send_message(
                    uid,
                    f"✅ <b>Получено {ton:.4f} TON</b>\nНачислено <b>+{rub:.2f} ₽</b>. Баланс: <b>{new_bal:.2f} ₽</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await notify_owner(bot_instance, f"💎 Авто-зачисление TON: <code>{uid}</code> • {ton:.4f} TON → +{rub:.2f} ₽ (баланс {new_bal:.2f} ₽)")


async def ton_poll_loop(bot_instance: Bot | None) -> None:
    await asyncio.sleep(5)
    while True:
        delay = 10
        try:
            if PENDING_TON:                                # без ожидающих платежей API не дёргаем
                await ton_poll_once(bot_instance)
        except Exception as e:
            print(f"TON poll error: {e}")
            delay = 20
        await asyncio.sleep(delay)


# ───────────────────────── Клавиатуры ─────────────────────────

def _format_user_label(user) -> str:
    if not user:
        return "друг"
    if getattr(user, "username", None):
        return f"@{html.escape(user.username)}"
    full_name = " ".join(
        part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
    ).strip()
    return html.escape(full_name) if full_name else "друг"


def get_welcome_text(user=None) -> str:
    return (
        f"🌑 <b>Привет, {_format_user_label(user)} — добро пожаловать в Dark Store.</b>\n\n"
        "Здесь собраны актуальные позиции, быстрая выдача сразу после оформления "
        "и прямой контакт с поддержкой без лишней суеты. "
        "Открывай каталог, смотри наличие и забирай нужный вариант в пару кликов.\n\n"
        f"📊 <b>Продано аккаунтов:</b> {TOTAL_SOLD_COUNT}"
    )


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛒 КАТАЛОГ / МАГАЗИН", callback_data="menu_catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton(text="💳 Пополнить", callback_data="menu_deposit"),
        ],
        [
            InlineKeyboardButton(text="⭐ Отзывы", url=REVIEWS_CHANNEL_URL),
            InlineKeyboardButton(text="📜 Правила", callback_data="menu_rules"),
        ],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton(text="👑 Панель владельца", callback_data="owner_panel")])
    elif is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админка", callback_data="staff_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stock_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(
            text=f"➕ Залить в {item['title']} ({len(item.get('accounts', []))} шт.)",
            callback_data=f"staff_add:{key}",
        )]
        for key, item in CATALOG.items()
    ]


def panel_keyboard(owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить аккаунты", callback_data="add_start")],
        [
            InlineKeyboardButton(text="📦 Позиции / удаление", callback_data="pos_list"),
            InlineKeyboardButton(text="📁 Новая позиция", callback_data="owner_new_cat"),
        ],
    ]
    if owner:
        rows += [
            [InlineKeyboardButton(text="📈 Статистика", callback_data="owner_stats"),
             InlineKeyboardButton(text="🎟 Промокоды", callback_data="owner_promos")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="owner_bcast"),
             InlineKeyboardButton(text="📜 Журнал действий", callback_data="owner_log")],
            [InlineKeyboardButton(text="💰 Выдать баланс", callback_data="owner_give_bal"),
             InlineKeyboardButton(text="🔎 Баланс по ID", callback_data="staff_check_bal")],
            [InlineKeyboardButton(text="🛡 Админы", callback_data="owner_admins")],
        ]
    else:
        rows.append([InlineKeyboardButton(text="🔎 Баланс по ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_panel_keyboard() -> InlineKeyboardMarkup:
    return panel_keyboard(True)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return panel_keyboard(False)


def panel_text(owner: bool) -> str:
    buyers = sum(1 for v in PURCHASE_HISTORY.values() if v)
    total_stock = sum(len(v.get("accounts", [])) for v in CATALOG.values())
    # распределение: СБ / без СБ  ×  дешёвые / дорогие (по цене, само)
    cat = {"0": [0, 0], "1": [0, 0], "n": [0, 0]}
    by_country: dict[tuple[str, str], int] = {}
    for v in CATALOG.values():
        for a in v.get("accounts", []):
            k = "0" if a.get("sb") is False else "1" if a.get("sb") is True else "n"
            price = float(a.get("price_rub") or v["price_rub"])
            cat[k][0 if price <= PRICE_SPLIT else 1] += 1
            ci = country_info(a.get("phone", ""))
            by_country[(ci["iso"], ci["name"])] = by_country.get((ci["iso"], ci["name"]), 0) + 1
    lines = [
        "👑 <b>Панель владельца</b>" if owner else "🛠 <b>Админка</b>",
        "━━━━━━━━━━━━━━━",
        f"📊 Продано: <b>{TOTAL_SOLD_COUNT}</b>  •  В наличии: <b>{total_stock}</b>",
    ]
    if owner:
        lines.append(f"👥 Покупателей: <b>{buyers}</b>  •  Админов: <b>{len(ADMIN_IDS)}</b>")
    lines += [
        "",
        "🗂 <b>Распределение</b>",
        f"✅ Без СБ: <b>{sum(cat['0'])}</b>  (дешёвых {cat['0'][0]} • дорогих {cat['0'][1]})",
        f"⚠️ С СБ: <b>{sum(cat['1'])}</b>  (дешёвых {cat['1'][0]} • дорогих {cat['1'][1]})",
    ]
    if sum(cat["n"]):
        lines.append(f"❔ Без отметки: <b>{sum(cat['n'])}</b>")
    lines.append(f"<i>Дешёвые — до {PRICE_SPLIT:.0f} ₽, дорогие — дороже. Распределяется по цене само.</i>")
    lines += ["", "📦 <b>Позиции</b>"]
    if not CATALOG:
        lines.append("— пока нет, создай через «Новая позиция»")
    else:
        for k, v in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"])):
            cnt = len(v.get("accounts", []))
            lines.append(f"{'🟢' if cnt else '🔴'} <b>{html.escape(v['title'])}</b> — <b>{cnt} шт.</b> • {v['price_rub']:.0f} ₽")
    if by_country:
        lines += ["", "🌍 <b>Страны</b>"]
        lines.append("  ".join(f"{flag_emoji(i)} {n} ×{c}" for (i, n), c in sorted(by_country.items(), key=lambda x: -x[1])))
    lines += ["━━━━━━━━━━━━━━━", "➕ <b>Как добавить:</b> «Добавить аккаунты» → тип (СБ / без СБ) → позиция → отправь список",
              "🗑 <b>Как удалить:</b> «Позиции / удаление» → выбери позицию",
              "<i>Проданный аккаунт сам удаляется со склада.</i>"]
    if owner:
        lines += ["", "🔑 Код вручную: <code>/code ID КОД</code>",
                  "💬 Ответ в поддержку: реплаем или <code>/reply ID текст</code>"]
    return "\n".join(lines)


def catalog_items_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"])):
        cnt = len(item.get("accounts", []))
        if cnt > 0:
            rows.append([InlineKeyboardButton(
                text=f"{item['title']} — {item['price_rub']:.0f} ₽ ({cnt} шт.)",
                callback_data=f"item_view:{key}",
            )])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_view_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Купить за ₽", callback_data=f"buy_acc:{key}")],
        [InlineKeyboardButton(text="⬅️ В каталог", callback_data="menu_catalog")],
    ])


def issued_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Получить / запросить код входа", callback_data="req_code")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
    ])


def deposit_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ Звёзды → ₽", callback_data="dep_method:stars"),
            InlineKeyboardButton(text="💎 TON → ₽", callback_data="dep_method:ton"),
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
    ])


def back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
    ])


if not BOT_TOKEN:
    print("ВНИМАНИЕ: BOT_TOKEN не задан в переменных окружения.")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


# ───────────────────────── Хендлеры бота ─────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    WAITING_INPUT.pop(message.from_user.id, None)
    arg = command.args or ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    await register_user(message.from_user.id, who(message.from_user), ref)
    await message.answer(
        get_welcome_text(message.from_user),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@dp.message(Command("code"))
async def cmd_code(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/code USER_ID КОД</code>", parse_mode="HTML")
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
    phone_line = f"\n<b>Номер:</b> <code>{phone}</code>" if phone else ""
    two_fa_line = f"\n🔐 <b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""

    await message.bot.send_message(
        chat_id=uid,
        text=f"🔑 <b>Код входа:</b> <code>{code}</code>{phone_line}{two_fa_line}",
        parse_mode="HTML",
    )
    await log_action(message.bot, message.from_user.id, who(message.from_user), f"отправил код пользователю <code>{uid}</code>")
    await message.answer(f"✅ Код <code>{code}</code> отправлен <code>{uid}</code>", parse_mode="HTML")


@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/reply USER_ID текст</code>\nИли просто ответь реплаем на сообщение «💬 Поддержка».", parse_mode="HTML")
        return
    await support_reply(message.bot, int(parts[1]), parts[2].strip())
    await log_action(message.bot, message.from_user.id, who(message.from_user), f"ответил в поддержке <code>{int(parts[1])}</code>: {html.escape(parts[2].strip()[:80])}")
    await message.answer("✅ Ответ отправлен")


@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    uid = message.from_user.id
    stars_paid = int(sp.total_amount or 0)
    rub_add = round(stars_paid * RUB_PER_STAR, 2)
    tx_add(uid, "dep_stars", rub_add, f"{stars_paid} ⭐")
    USER_NAMES[uid] = who(message.from_user)
    new_bal = await add_rub_balance(uid, rub_add)
    await message.answer(
        f"✅ Оплачено <b>{stars_paid} ⭐</b>! Начислено <b>+{rub_add:.2f} ₽</b>.\n"
        f"Текущий баланс: <b>{new_bal:.2f} ₽</b>.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await notify_owner(
        message.bot,
        f"⭐ Оплата звёздами от <code>{uid}</code>: {stars_paid} ⭐ -> +{rub_add:.2f} ₽ (баланс: {new_bal:.2f} ₽)",
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
        "📜 <b>Правила магазина</b>\n\n"
        "1. Проверяйте аккаунты сразу после выдачи.\n"
        "2. Пополнение звёздами и TON зачисляется на ваш личный баланс в ₽.\n"
        f"3. По всем вопросам обращайтесь в поддержку: @{SUPPORT_USERNAME}",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_panel")
async def cb_owner_panel(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(panel_text(True), parse_mode="HTML", reply_markup=panel_keyboard(True))
    await callback.answer()


@dp.callback_query(F.data == "staff_panel")
async def cb_staff_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text(panel_text(False), parse_mode="HTML", reply_markup=panel_keyboard(False))
    await callback.answer()


async def _owner_prompt(callback: CallbackQuery, state: str, text: str):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = state
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "owner_new_cat")
async def cb_owner_new_cat(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_new_cat"
    await callback.message.edit_text(
        "📁 <b>Новая позиция</b>\n\n"
        "Это группа с ценой по умолчанию. У отдельного аккаунта можно указать свою цену.\n\n"
        "Формат: <code>ключ|Название|Цена_в_рублях|Описание</code>\n"
        "Пример: <code>main|Telegram-аккаунты|60|Выдача за секунды</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_give_bal")
async def cb_owner_give_bal(callback: CallbackQuery):
    await _owner_prompt(
        callback, "owner_give_bal",
        "💰 <b>Выдача баланса (в ₽)</b>\n\nФормат: <code>USER_ID СУММА_РУБЛЕЙ</code>\nПример: <code>123456789 500</code>",
    )


def _owner_only(callback: CallbackQuery) -> bool:
    return is_owner(callback.from_user.id)


@dp.callback_query(F.data == "owner_stats")
async def cb_owner_stats(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    await callback.message.edit_text(
        stats_text(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Обновить", "owner_stats")], [_btn("⬅️ Назад", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_promos")
async def cb_owner_promos(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    rows = [[_btn(f"🗑 {c} • +{p['amount']:.0f} ₽ • {len(p['used'])}/{p['limit'] or '∞'}", f"promo_del:{c}")] for c, p in PROMOS.items()]
    rows += [[_btn("➕ Создать промокод", "promo_new")], [_btn("⬅️ Назад", "owner_panel")]]
    await callback.message.edit_text(
        "🎟 <b>Промокоды</b>\n\nНажми на промокод, чтобы удалить его." if PROMOS else "🎟 <b>Промокоды</b>\n\nПока нет ни одного.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data == "promo_new")
async def cb_promo_new(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "promo_new"
    await callback.message.edit_text(
        "🎟 <b>Новый промокод</b>\n\nФормат: <code>КОД|сумма|лимит</code>\n"
        "Лимит — сколько человек могут использовать (0 — без лимита). Один человек — один раз.\n"
        "Пример: <code>START|10|100</code>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", "owner_promos")]]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("promo_del:"))
async def cb_promo_del(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    PROMOS.pop(callback.data.split(":", 1)[1], None)
    await save_data()
    await callback.answer("Удалён")
    await cb_owner_promos(callback)


@dp.callback_query(F.data == "owner_bcast")
async def cb_owner_bcast(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "bcast"
    await callback.message.edit_text(
        f"📢 <b>Рассылка</b>\n\nОтправь текст сообщения — покажу предпросмотр. Получателей: <b>{len(all_user_ids())}</b>.\n"
        "Можно использовать форматирование Telegram (жирный, ссылки).",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "bcast_go")
async def cb_bcast_go(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("Только для владельца", show_alert=True)
        return
    text = BCAST_PENDING.pop(callback.from_user.id, None)
    if not text:
        await callback.answer("Нет текста рассылки", show_alert=True)
        return
    asyncio.create_task(_run_broadcast(callback.bot, callback.from_user.id, text))
    await callback.message.edit_text("📢 Рассылка запущена. Пришлю отчёт, когда закончится.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ В панель", "owner_panel")]]))
    await callback.answer()


@dp.callback_query(F.data == "bcast_cancel")
async def cb_bcast_cancel(callback: CallbackQuery):
    BCAST_PENDING.pop(callback.from_user.id, None)
    WAITING_INPUT.pop(callback.from_user.id, None)
    await callback.message.edit_text("Рассылка отменена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ В панель", "owner_panel")]]))
    await callback.answer()


@dp.callback_query(F.data == "owner_log")
async def cb_owner_log(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    if not ADMIN_LOG:
        body = "Пока нет действий админов."
    else:
        body = "\n\n".join(
            f"<code>{time.strftime('%d.%m %H:%M', time.gmtime(e['ts'] + MSK_OFFSET))}</code> • {html.escape(e['name'])}\n{e['text']}"
            for e in reversed(ADMIN_LOG[-15:])
        )
    await callback.message.edit_text(
        f"📜 <b>Журнал действий админов</b>\n<i>время МСК, последние 15</i>\n\n{body}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "owner_panel")]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_admins")
async def cb_owner_admins(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    adm = ", ".join(f"<code>{x}</code>" for x in sorted(ADMIN_IDS)) or "нет"
    await callback.message.edit_text(
        f"🛡 <b>Админы</b>\n\n{adm}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Выдать", callback_data="owner_add_adm"),
                InlineKeyboardButton(text="➖ Снять", callback_data="owner_del_adm"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="owner_panel")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "owner_add_adm")
async def cb_owner_add_adm(callback: CallbackQuery):
    await _owner_prompt(callback, "owner_add_adm", "➕ <b>Выдача админки</b>\n\nОтправь числовой <code>USER_ID</code>:")


@dp.callback_query(F.data == "owner_del_adm")
async def cb_owner_del_adm(callback: CallbackQuery):
    await _owner_prompt(callback, "owner_del_adm", "➖ <b>Снятие админки</b>\n\nОтправь числовой <code>USER_ID</code>:")


@dp.callback_query(F.data == "staff_check_bal")
async def cb_staff_check_bal(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "staff_check_bal"
    await callback.message.edit_text(
        "🔎 <b>Баланс по ID</b>\n\nОтправь числовой <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()


SB_LABEL = {"0": "✅ Без СБ", "1": "⚠️ С СБ", "n": "❔ без отметки"}


def panel_back_cb(uid: int) -> str:
    return "owner_panel" if is_owner(uid) else "staff_panel"


def to_panel_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_start")],
        [InlineKeyboardButton(text="⬅️ В панель", callback_data=panel_back_cb(uid))],
    ])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _deny(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
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
            "➕ <b>Добавить аккаунты</b>\n\nСначала нужна позиция (группа с ценой). Создай её кнопкой ниже.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("📁 Новая позиция", "owner_new_cat")], [_btn("⬅️ Назад", panel_back_cb(uid))]]),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "➕ <b>Добавить аккаунты</b>  <i>• шаг 1 из 3</i>\n\n"
        "Какие аккаунты добавляешь?\n"
        "✅ <b>Без СБ</b> — чистые, без спамблока\n"
        "⚠️ <b>С СБ</b> — со спамблоком",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ Без СБ", "staff_sb:0"), _btn("⚠️ С СБ", "staff_sb:1")],
            [_btn("❔ Не указывать", "staff_sb:n")],
            [_btn("⬅️ Назад", panel_back_cb(uid))],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("staff_sb:"))
async def cb_staff_sb(callback: CallbackQuery):
    if await _deny(callback):
        return
    sb = callback.data.split(":")[1]
    rows = [
        [_btn(f"{item['title']} • {item['price_rub']:.0f} ₽ • {len(item.get('accounts', []))} шт.", f"staff_add:{key}:{sb}")]
        for key, item in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"]))
    ]
    rows.append([_btn("⬅️ Назад", "add_start")])
    await callback.message.edit_text(
        f"➕ <b>Добавить: {SB_LABEL.get(sb, '')}</b>  <i>• шаг 2 из 3</i>\n\n"
        "Выбери позицию. Её цена будет у аккаунтов по умолчанию.",
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
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"staff_add:{key}:{sb}"
    await callback.message.edit_text(
        f"➕ <b>{html.escape(item['title'])}</b> • {SB_LABEL.get(sb, '')}  <i>• шаг 3 из 3</i>\n\n"
        "Отправь аккаунты сообщением, <b>каждый с новой строки</b>:\n"
        "<code>+8613812342600:СЕССИЯ:2FA</code>\n\n"
        "По желанию после знака <b>|</b>:\n"
        "• дата регистрации — <code>08.08.2026</code>\n"
        "• <code>prem</code> — Premium\n"
        "• число — своя цена в ₽ (иначе цена позиции)\n"
        "Пример:\n<code>+8613812342600:СЕССИЯ:2FA | 08.08.2026 | 21</code>\n\n"
        "Страна и флаг — по номеру. Дешёвые/дорогие — по цене, само.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", panel_back_cb(callback.from_user.id))]]),
    )
    await callback.answer()


# ───── позиции: список, цена, очистка, удаление ─────

@dp.callback_query(F.data == "pos_list")
async def cb_pos_list(callback: CallbackQuery):
    if await _deny(callback):
        return
    uid = callback.from_user.id
    WAITING_INPUT.pop(uid, None)
    rows = [
        [_btn(f"{'🟢' if v.get('accounts') else '🔴'} {v['title']} • {v['price_rub']:.0f} ₽ • {len(v.get('accounts', []))} шт.", f"pos_view:{k}")]
        for k, v in sorted(CATALOG.items(), key=lambda kv: float(kv[1]["price_rub"]))
    ]
    rows.append([_btn("📁 Новая позиция", "owner_new_cat")])
    rows.append([_btn("⬅️ Назад", panel_back_cb(uid))])
    text = "📦 <b>Позиции</b>\n\nНажми на позицию — можно добавить аккаунты, изменить цену, очистить или удалить." if CATALOG \
        else "📦 <b>Позиции</b>\n\nПозиций нет. Создай первую кнопкой ниже."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def _show_pos(callback: CallbackQuery, key: str):
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    desc = f"\n📝 {html.escape(item['description'])}" if item.get("description") else ""
    await callback.message.edit_text(
        f"📦 <b>{html.escape(item['title'])}</b>\n━━━━━━━━━━━━━━━\n"
        f"💰 Цена по умолчанию: <b>{item['price_rub']:.0f} ₽</b>\n"
        f"📊 Аккаунтов: <b>{len(item.get('accounts', []))}</b>\n"
        f"🔑 Ключ: <code>{html.escape(key)}</code>{desc}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("➕ Добавить аккаунты", f"pos_add:{key}")],
            [_btn("✏️ Изменить цену", f"pos_price:{key}")],
            [_btn("🧹 Очистить аккаунты", f"pos_clear:{key}"), _btn("❌ Удалить позицию", f"pos_del:{key}")],
            [_btn("⬅️ К позициям", "pos_list")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pos_view:"))
async def cb_pos_view(callback: CallbackQuery):
    if await _deny(callback):
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    await _show_pos(callback, callback.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("pos_add:"))
async def cb_pos_add(callback: CallbackQuery):
    if await _deny(callback):
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        f"➕ <b>{html.escape(item['title'])}</b>\n\nКакие аккаунты добавляешь?\n"
        "✅ <b>Без СБ</b> — чистые\n⚠️ <b>С СБ</b> — со спамблоком",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ Без СБ", f"staff_add:{key}:0"), _btn("⚠️ С СБ", f"staff_add:{key}:1")],
            [_btn("❔ Не указывать", f"staff_add:{key}:n")],
            [_btn("⬅️ Назад", f"pos_view:{key}")],
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
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"pos_price:{key}"
    await callback.message.edit_text(
        f"✏️ <b>{html.escape(item['title'])}</b>\n\nСейчас: <b>{item['price_rub']:.0f} ₽</b>\nОтправь новую цену числом:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"pos_view:{key}")]]),
    )
    await callback.answer()


async def _confirm(callback: CallbackQuery, key: str, kind: str):
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    cnt = len(item.get("accounts", []))
    what = (f"Удалить позицию <b>{html.escape(item['title'])}</b> вместе с <b>{cnt}</b> аккаунтами?"
            if kind == "del" else f"Удалить все <b>{cnt}</b> аккаунтов из <b>{html.escape(item['title'])}</b>? Позиция останется.")
    await callback.message.edit_text(
        f"⚠️ {what}\n\n<b>Отменить это нельзя.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🗑 Да, удалить", f"pos_{kind}_yes:{key}")],
            [_btn("⬅️ Нет, назад", f"pos_view:{key}")],
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
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("📦 К позициям", "pos_list")], [_btn("⬅️ В панель", panel_back_cb(uid))]]),
    )
    await log_action(callback.bot, uid, who(callback.from_user), text)
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("pos_del_yes:"))
async def cb_pos_del_yes(callback: CallbackQuery):
    if await _deny(callback):
        return
    item = CATALOG.pop(callback.data.split(":", 1)[1], None)
    if not item:
        await callback.answer("Уже удалена", show_alert=True)
        return
    await _after_delete(callback, f"🗑 Позиция <b>{html.escape(item['title'])}</b> удалена (аккаунтов: {len(item.get('accounts', []))}).")


@dp.callback_query(F.data.startswith("pos_clear_yes:"))
async def cb_pos_clear_yes(callback: CallbackQuery):
    if await _deny(callback):
        return
    item = CATALOG.get(callback.data.split(":", 1)[1])
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    n = len(item.get("accounts", []))
    item["accounts"] = []
    await _after_delete(callback, f"🧹 В позиции <b>{html.escape(item['title'])}</b> удалено аккаунтов: {n}.")


@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    if not any(len(v.get("accounts", [])) > 0 for v in CATALOG.values()):
        await callback.message.edit_text(
            "🛒 <b>Каталог</b>\n\nСейчас в наличии нет позиций.",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "🛒 <b>Каталог</b>\n\nОт дешёвых к дорогим. Выберите позицию:",
        parse_mode="HTML",
        reply_markup=catalog_items_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("item_view:"))
async def cb_item_view(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("Нет в наличии", show_alert=True)
        return
    await callback.message.edit_text(
        f"📦 <b>{item['title']}</b>\n\n"
        f"{item.get('description', '')}\n\n"
        f"💰 <b>Цена:</b> {item['price_rub']:.2f} ₽\n"
        f"📊 <b>В наличии:</b> {len(item['accounts'])} шт.",
        parse_mode="HTML",
        reply_markup=item_view_keyboard(key),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_acc:"))
async def cb_buy_acc(callback: CallbackQuery):
    uid = callback.from_user.id
    if not check_rate_limit(uid, "buy", 1.2):
        await callback.answer("Не так быстро", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    rec, err = await do_purchase(uid, key)
    if err:
        await callback.answer(err, show_alert=True)
        return

    two_fa_line = f"\n🔐 <b>2FA:</b> <code>{rec['two_fa']}</code>" if rec["two_fa"] else ""
    await callback.message.edit_text(
        f"✅ <b>Аккаунт выдан!</b>\n\n"
        f"<b>Заказ:</b> <code>{rec['order_id']}</code>\n"
        f"<b>Позиция:</b> {rec['title']}\n"
        f"<b>Номер:</b> <code>{rec['phone']}</code>{two_fa_line}\n\n"
        "Нажми <b>«Получить / запросить код входа»</b> для входа.\n"
        "Отзыв можно оставить в мини-приложении → «Мои заказы».",
        parse_mode="HTML",
        reply_markup=issued_account_keyboard(),
    )
    await callback.answer("Выдано!")

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейма"
    await notify_owner(
        callback.bot,
        f"🛒 <b>Покупка!</b>\n"
        f"Юзер: {uname} (<code>{uid}</code>)\n"
        f"Заказ: <code>{rec['order_id']}</code>\n"
        f"Позиция: {rec['title']} — <b>{rec['price_rub']:.2f} ₽</b>\n"
        f"Номер: <code>{rec['phone']}</code>\n"
        f"Остаток: <b>{rec['left']} шт.</b>",
    )


@dp.callback_query(F.data == "req_code")
async def cb_req_code(callback: CallbackQuery):
    uid = callback.from_user.id
    if not check_rate_limit(uid, "code", 2.0):
        await callback.answer("Подожди пару секунд", show_alert=True)
        return
    acc = LAST_ISSUED_ACCOUNT.get(uid)
    if not acc:
        await callback.answer("Нет выданного аккаунта", show_alert=True)
        return

    code = await fetch_tg_login_code(acc.get("session_string", ""))
    if code:
        acc["code"] = code
        await save_data()
        await callback.message.answer(
            f"🔑 <b>Код входа для</b> <code>{acc['phone']}</code>: <code>{code}</code>",
            parse_mode="HTML",
        )
        await callback.answer("Код получен!")
        return

    await callback.message.answer(f"⏳ Запрос кода по номеру <code>{acc['phone']}</code> отправлен.", parse_mode="HTML")
    await callback.answer("Запрос отправлен")
    await notify_owner(
        callback.bot,
        f"📩 Запрос кода от <code>{uid}</code> по номеру <code>{acc['phone']}</code>.\n"
        f"Отправь: <code>/code {uid} КОД</code>",
    )


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    uid = callback.from_user.id
    role = "Владелец" if is_owner(uid) else ("Админ" if is_admin(uid) else "Покупатель")
    await callback.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"Роль: <b>{role}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Баланс: <b>{get_rub_balance(uid):.2f} ₽</b>\n"
        f"Покупок: <b>{len(PURCHASE_HISTORY.get(uid, []))}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_deposit")
async def cb_menu_deposit(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.edit_text(
        f"💳 <b>Пополнение баланса (в ₽)</b>\n\n"
        f"Ваш баланс: <b>{get_rub_balance(uid):.2f} ₽</b>\n"
        f"Курсы: 1 ⭐ = {RUB_PER_STAR} ₽ • 1 TON = {RUB_PER_TON} ₽",
        parse_mode="HTML",
        reply_markup=deposit_choice_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dep_method:"))
async def cb_dep_method(callback: CallbackQuery):
    m = callback.data.split(":", 1)[1]
    WAITING_INPUT[callback.from_user.id] = f"dep_amt:{m}"
    if m == "stars":
        text = (
            f"⭐ <b>Пополнение звёздами (в ₽)</b>\n\n"
            f"Курс: <b>1 ⭐ = {RUB_PER_STAR} ₽</b>\n"
            f"Напиши целое число звёзд для выставления счёта:"
        )
    else:
        text = (
            f"💎 <b>Пополнение TON (в ₽)</b>\n\n"
            f"Курс: <b>1 TON = {RUB_PER_TON} ₽</b>\n"
            f"Кошелёк: <code>{CRYPTO_WALLET}</code>\n\n"
            f"Напиши сумму в TON — пришлю реквизиты и комментарий для автоматического зачисления:"
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_main_keyboard())
    await callback.answer()


@dp.message(F.text)
async def handle_text_inputs(message: Message):
    uid = message.from_user.id
    txt = (message.text or "").strip()

    # ответ админа реплаем на уведомление «💬 Поддержка»
    if is_admin(uid) and message.reply_to_message and txt and not txt.startswith("/"):
        m = re.search(r"ID:\s*(\d{5,})", message.reply_to_message.text or "")
        if m:
            await support_reply(message.bot, int(m.group(1)), txt)
            await log_action(message.bot, uid, who(message.from_user), f"ответил в поддержке <code>{m.group(1)}</code>: {html.escape(txt[:80])}")
            await message.answer("✅ Ответ отправлен в чат поддержки")
            return

    wait = WAITING_INPUT.get(uid)
    if not wait:
        return

    if wait == "dep_amt:stars":
        if not txt.isdigit() or int(txt) <= 0:
            await message.answer("Введи целое число звёзд больше нуля.")
            return
        stars_cnt = int(txt)
        WAITING_INPUT.pop(uid, None)
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        await message.bot.send_invoice(
            chat_id=uid,
            title=f"Пополнение баланса ({rub_preview:.2f} ₽)",
            description=f"Зачисление {rub_preview:.2f} ₽ на баланс за {stars_cnt} ⭐",
            payload=f"rub_stars:{uid}:{stars_cnt}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars_cnt} ⭐", amount=stars_cnt)],
        )
        return

    if wait == "dep_amt:ton":
        try:
            ton_amt = float(txt.replace(",", "."))
        except ValueError:
            await message.answer("Введи сумму в TON больше нуля.")
            return
        if ton_amt <= 0:
            await message.answer("Сумма в TON должна быть больше нуля.")
            return
        WAITING_INPUT.pop(uid, None)
        if ton_amt < MIN_TON:
            await message.answer(f"Минимум {MIN_TON} TON.")
            WAITING_INPUT[uid] = "dep_amt:ton"
            return
        rub_est = round(ton_amt * RUB_PER_TON, 2)
        memo = ton_memo_for(uid)
        await save_data()
        await message.answer(
            f"💎 <b>Пополнение TON</b>\n\n"
            f"Переведи <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} ₽</b>) на кошелёк:\n<code>{CRYPTO_WALLET}</code>\n\n"
            f"⚠️ <b>Обязательно укажи комментарий:</b>\n<code>{memo}</code>\n\n"
            "Зачисление автоматическое — обычно в течение минуты после подтверждения сети.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Открыть в кошельке", url=ton_pay_link(ton_amt, memo))],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
            ]),
        )
        return

    if wait == "staff_check_bal" and is_admin(uid):
        if not txt.isdigit():
            await message.answer("Нужен числовой ID.")
            return
        WAITING_INPUT.pop(uid, None)
        target_id = int(txt)
        await log_action(message.bot, uid, who(message.from_user), f"смотрел баланс <code>{target_id}</code>")
        await message.answer(
            f"💰 Баланс пользователя <code>{target_id}</code>: <b>{get_rub_balance(target_id):.2f} ₽</b>",
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
        summary = "  ".join(f"{flag_emoji(i)} {n} ×{c}" for (i, n), c in countries.items())
        await log_action(message.bot, uid, who(message.from_user), f"добавил <b>{added}</b> акк. в «{html.escape(item['title'])}» ({SB_LABEL.get(sb_mode, '')})")
        text = f"✅ Добавлено <b>{added} шт.</b> в <b>{html.escape(item['title'])}</b> (всего: {len(item['accounts'])}).\n{summary}"
        if notes:
            text += "\n⚠️ " + html.escape("; ".join(notes[:5]))
        await message.answer(text, parse_mode="HTML", reply_markup=to_panel_kb(uid))
        return

    if wait.startswith("pos_price:") and is_admin(uid):
        key = wait.split(":", 1)[1]
        item = CATALOG.get(key)
        try:
            price = float(txt.replace(",", "."))
            assert price > 0
        except (ValueError, AssertionError):
            await message.answer("Нужна цена числом больше нуля, например 60")
            return
        WAITING_INPUT.pop(uid, None)
        if item:
            old_price = item["price_rub"]
            item["price_rub"] = price
            await save_data()
            await log_action(message.bot, uid, who(message.from_user), f"изменил цену «{html.escape(item['title'])}»: {old_price:.0f} → {price:.0f} ₽")
            await message.answer(
                f"✅ Цена <b>{html.escape(item['title'])}</b> теперь <b>{price:.0f} ₽</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("📦 К позиции", f"pos_view:{key}")]]),
            )
        return

    if wait == "owner_new_cat" and is_admin(uid):
        parts = [p.strip() for p in txt.split("|", 3)]
        if len(parts) < 3:
            await message.answer("Формат: <code>ключ|Название|Цена_в_рублях|Описание</code>", parse_mode="HTML")
            return
        key, title, price_raw = parts[0], parts[1], parts[2]
        desc = parts[3] if len(parts) > 3 else ""
        try:
            price_rub = float(price_raw.replace(",", "."))
        except ValueError:
            await message.answer("Цена должна быть числом.")
            return
        WAITING_INPUT.pop(uid, None)
        await log_action(message.bot, uid, who(message.from_user), f"создал позицию «{html.escape(title)}» ({price_rub:.0f} ₽)")
        CATALOG[key] = {
            "title": title,
            "price_rub": price_rub,
            "description": desc,
            "accounts": CATALOG.get(key, {}).get("accounts", []),
        }
        await save_data()
        await message.answer(
            f"✅ Позиция <b>{title}</b> (<code>{key}</code>) сохранена — {price_rub:.0f} ₽.",
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
            await message.answer("Формат: <code>КОД|сумма|лимит</code>, например <code>START|10|100</code>", parse_mode="HTML")
            return
        WAITING_INPUT.pop(uid, None)
        PROMOS[code] = {"amount": amount, "limit": limit, "used": [], "active": True}
        await save_data()
        await message.answer(
            f"✅ Промокод <code>{code}</code> создан: +{amount:.0f} ₽, лимит {limit or '∞'}.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🎟 К промокодам", "owner_promos")]]),
        )
        return

    if wait == "bcast":
        WAITING_INPUT.pop(uid, None)
        BCAST_PENDING[uid] = message.html_text
        await message.answer(
            f"📢 <b>Предпросмотр</b>\n\n{message.html_text}\n\n━━━━━━━━━━\nПолучателей: <b>{len(all_user_ids())}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Отправить всем", "bcast_go")], [_btn("❌ Отмена", "bcast_cancel")]]),
        )
        return

    if wait == "owner_give_bal":
        parts = txt.split()
        if len(parts) != 2 or not parts[0].isdigit():
            await message.answer("Формат: <code>USER_ID СУММА_РУБЛЕЙ</code>", parse_mode="HTML")
            return
        target_id = int(parts[0])
        try:
            rub_amt = float(parts[1].replace(",", "."))
        except ValueError:
            await message.answer("Сумма должна быть числом.")
            return
        WAITING_INPUT.pop(uid, None)
        tx_add(target_id, "gift", rub_amt, "от магазина")
        new_bal = await add_rub_balance(target_id, rub_amt)
        await message.answer(
            f"✅ Выдано <b>{rub_amt:.2f} ₽</b> пользователю <code>{target_id}</code>. Баланс: <b>{new_bal:.2f} ₽</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait in ("owner_add_adm", "owner_del_adm"):
        if not txt.isdigit():
            await message.answer("Нужен числовой USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        if wait == "owner_add_adm":
            ADMIN_IDS.add(adm_id)
            msg = f"✅ Пользователь <code>{adm_id}</code> назначен админом."
        else:
            ADMIN_IDS.discard(adm_id)
            msg = f"✅ Админка у <code>{adm_id}</code> снята."
        await save_data()
        await message.answer(msg, parse_mode="HTML", reply_markup=main_menu_keyboard(uid))


# ───────────────────────── HTTP API для мини-аппа ─────────────────────────

def _cors_response(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=CORS_HEADERS)


def auth_user(request: web.Request) -> dict | None:
    """Пользователь берётся ТОЛЬКО из подписанных Telegram данных — подделать id нельзя."""
    user = verify_init_data(request.headers.get("X-Init-Data", ""))
    if user:
        USER_NAMES[int(user["id"])] = display_name(user)
    return user


def _unauth() -> web.Response:
    return _cors_response({"ok": False, "error": "Откройте приложение через Telegram"}, status=401)


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
    return _cors_response(build_state_payload(uid))


async def api_buy(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    item_key = str(data.get("item_key") or "").strip()
    if not item_key:
        return _cors_response({"ok": False, "error": "Не указана позиция"}, status=400)
    if not check_rate_limit(uid, "api_buy", 1.2):
        return _cors_response({"ok": False, "error": "Слишком частые запросы"}, status=429)

    acc_id = str(data.get("acc_id") or "").strip() or None
    rec, err = await do_purchase(uid, item_key, acc_id)
    if err:
        return _cors_response({"ok": False, "error": err}, status=400)

    if bot:
        await notify_owner(
            bot,
            f"🛒 <b>MiniApp покупка</b>\n{html.escape(display_name(user))} (<code>{uid}</code>)\n"
            f"{html.escape(rec['title'])} — <b>{rec['price_rub']:.2f} ₽</b>\n"
            f"<code>{rec['order_id']}</code> • <code>{rec['phone']}</code>\nОстаток: {rec['left']} шт.",
        )
    return _cors_response({"ok": True, "order_id": rec["order_id"], "phone": rec["phone"],
                           "two_fa": rec["two_fa"], "balance_rub": get_rub_balance(uid)})


async def api_deposit(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    method = str(data.get("method") or "").strip().lower()
    try:
        amount = float(str(data.get("amount") or "0").replace(",", "."))
    except (ValueError, TypeError):
        amount = 0.0
    if amount <= 0 or method not in ("stars", "ton"):
        return _cors_response({"ok": False, "error": "Проверьте сумму и способ оплаты"}, status=400)
    if not check_rate_limit(uid, "api_dep", 1.0):
        return _cors_response({"ok": False, "error": "Подождите секунду и попробуйте снова"}, status=429)

    if method == "stars":
        stars_cnt = max(1, int(round(amount)))
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        if not bot:
            return _cors_response({"ok": False, "error": "BOT_TOKEN не настроен на сервере"}, status=500)
        try:
            link = await bot.create_invoice_link(
                title=f"Пополнение баланса ({rub_preview:.2f} ₽)",
                description=f"Зачисление {rub_preview:.2f} ₽ за {stars_cnt} ⭐",
                payload=f"rub_stars:{uid}:{stars_cnt}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{stars_cnt} ⭐", amount=stars_cnt)],
            )
            return _cors_response({"ok": True, "invoice_link": link})
        except Exception as e:
            return _cors_response({"ok": False, "error": f"Telegram Stars: {e}"}, status=400)

    if amount < MIN_TON:
        return _cors_response({"ok": False, "error": f"Минимум {MIN_TON} TON"}, status=400)
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
    data = await _json(request)
    order_id = str(data.get("order_id") or "").strip()
    if not check_rate_limit(uid, "api_code", 2.0):
        return _cors_response({"ok": False, "error": "Слишком частые запросы кода"}, status=429)

    target = None
    for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
        if isinstance(rec, dict) and (not order_id or rec.get("order_id") == order_id):
            target = rec
            break
    if not target:
        return _cors_response({"ok": False, "error": "Заказ не найден"}, status=404)

    if target.get("code"):
        return _cors_response({"ok": True, "code": target["code"]})

    fetched = await fetch_tg_login_code(target.get("session_string", ""))
    if fetched:
        target["code"] = fetched
        await save_data()
        return _cors_response({"ok": True, "code": fetched})

    if bot and not target.get("code_requested"):   # владельцу — только первый запрос, дальше идёт опрос
        target["code_requested"] = True
        await save_data()
        await notify_owner(
            bot,
            f"📩 MiniApp запрос кода от <code>{uid}</code> (<code>{target.get('phone')}</code>).\n"
            f"Отправь: <code>/code {uid} КОД</code>",
        )
    return _cors_response({"ok": False, "error": "Запрос отправлен, код скоро придёт"})


async def api_review(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    order_id = str(data.get("order_id") or "").strip()
    text = str(data.get("text") or "").strip()[:500]
    try:
        rating = int(data.get("rating") or 0)
    except (ValueError, TypeError):
        rating = 0
    if rating < 1 or rating > 5:
        return _cors_response({"ok": False, "error": "Поставьте оценку от 1 до 5"}, status=400)
    if not check_rate_limit(uid, "api_review", 2.0):
        return _cors_response({"ok": False, "error": "Подождите пару секунд"}, status=429)

    rec = next((r for r in PURCHASE_HISTORY.get(uid, []) if isinstance(r, dict) and r.get("order_id") == order_id), None)
    if not rec:
        return _cors_response({"ok": False, "error": "Отзыв можно оставить только после покупки"}, status=403)
    if rec.get("reviewed"):
        return _cors_response({"ok": False, "error": "Вы уже оставили отзыв на этот заказ"}, status=400)

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
            f"⭐ <b>Новый отзыв</b> {'★' * rating}{'☆' * (5 - rating)}\n"
            f"{html.escape(display_name(user))} (<code>{uid}</code>)\n{html.escape(text) or 'без комментария'}",
        )
    return _cors_response({"ok": True, "reviews": reviews_payload(uid)})


async def api_support_send(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    text = str(data.get("text") or "").strip()[:1000]
    if not text:
        return _cors_response({"ok": False, "error": "Пустое сообщение"}, status=400)
    if not check_rate_limit(uid, "api_support", 1.0):
        return _cors_response({"ok": False, "error": "Не так быстро"}, status=429)

    SUPPORT.setdefault(uid, []).append({"from": "user", "text": text, "ts": int(time.time())})
    del SUPPORT[uid][:-200]
    await save_data()
    if bot:
        await notify_staff(
            bot,
            f"💬 <b>Поддержка</b> • {html.escape(display_name(user))}\n"
            f"ID: <code>{uid}</code>\n\n{html.escape(text)}\n\n"
            "<i>Ответь реплаем на это сообщение.</i>",
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
    if LAST_SPIN.get(uid) == roulette_day():
        return _cors_response({"ok": False, "error": "Сегодня вы уже крутили", "roulette": roulette_status(uid)}, status=400)
    LAST_SPIN[uid] = roulette_day()            # отмечаем ДО await — двойной тап не пройдёт
    prize = _rng.choices(ROULETTE_PRIZES, weights=ROULETTE_WEIGHTS)[0]
    tx_add(uid, "roulette", prize, "рулетка")
    new_bal = await add_rub_balance(uid, prize)  # внутри сохраняется и LAST_SPIN
    return _cors_response({"ok": True, "prize": prize, "balance_rub": new_bal, "roulette": roulette_status(uid)})


async def api_review_edit(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    rev = next((r for r in REVIEWS if r.get("id") == str(data.get("review_id") or "")), None)
    if not rev or rev.get("user_id") != uid:
        return _cors_response({"ok": False, "error": "Редактировать можно только свой отзыв"}, status=403)
    try:
        rating = int(data.get("rating") or 0)
    except (ValueError, TypeError):
        rating = 0
    if rating < 1 or rating > 5:
        return _cors_response({"ok": False, "error": "Поставьте оценку от 1 до 5"}, status=400)
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
        return _cors_response({"ok": False, "error": "Удалять отзывы может только владелец или админ"}, status=403)
    data = await _json(request)
    rev = next((r for r in REVIEWS if r.get("id") == str(data.get("review_id") or "")), None)
    if not rev:
        return _cors_response({"ok": False, "error": "Отзыв не найден"}, status=404)
    REVIEWS.remove(rev)
    await save_data()
    await log_action(bot, uid, display_name(user), f"удалил отзыв {html.escape(rev['name'])} ({rev['rating']}★): {html.escape(rev['text'][:60]) or 'без текста'}")
    return _cors_response({"ok": True, "reviews": reviews_payload(uid)})


async def api_order_finish(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    data = await _json(request)
    rec = next((r for r in PURCHASE_HISTORY.get(uid, []) if isinstance(r, dict) and r.get("order_id") == str(data.get("order_id") or "")), None)
    if not rec:
        return _cors_response({"ok": False, "error": "Заказ не найден"}, status=404)
    rec["finished"] = True
    await save_data()
    return _cors_response({"ok": True})


async def api_promo(request: web.Request) -> web.Response:
    user = auth_user(request)
    if not user:
        return _unauth()
    uid = int(user["id"])
    if not check_rate_limit(uid, "promo", 2.0):
        return _cors_response({"ok": False, "error": "Не так быстро"}, status=429)
    data = await _json(request)
    code = str(data.get("code") or "").strip().upper()
    p = PROMOS.get(code)
    if not p or not p.get("active", True):
        return _cors_response({"ok": False, "error": "Промокод не найден"}, status=404)
    if uid in p["used"]:
        return _cors_response({"ok": False, "error": "Вы уже использовали этот промокод"}, status=400)
    if p.get("limit") and len(p["used"]) >= p["limit"]:
        return _cors_response({"ok": False, "error": "Промокод закончился"}, status=400)
    p["used"].append(uid)                         # отмечаем до await — повторно не пройдёт
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
