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

REVIEWS: list[dict] = []
SUPPORT_MESSAGES: dict[int, list[dict]] = {} 

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
    global CATALOG, ADMIN_IDS, PURCHASE_HISTORY, USER_RUB_BALANCE, TOTAL_SOLD_COUNT, REVIEWS, SUPPORT_MESSAGES
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
            # Очищаем тестовые миллионы, если они есть (больше 1 млн)
            USER_RUB_BALANCE = {int(k): float(v) for k, v in raw_balances.items() if float(v) < 1000000}

            TOTAL_SOLD_COUNT = int(data.get("total_sold_count", 0))
            REVIEWS = data.get("reviews", [])
            
            raw_support = data.get("support_messages", {})
            SUPPORT_MESSAGES = {int(k): v for k, v in raw_support.items()}
            
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
        "reviews": REVIEWS,
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
        print(f"Ошибка при сохранении данных в PostgreSQL: {e}")

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

    recent_sales = []
    for uid, hist in PURCHASE_HISTORY.items():
        for rec in hist[-2:]: 
            if isinstance(rec, dict):
                recent_sales.append({
                    "title": rec.get("title", "Аккаунт"),
                    "phone": rec.get("phone", ""),
                })
    recent_sales = recent_sales[-5:] 

    sorted_reviews = sorted(REVIEWS, key=lambda x: x.get("timestamp", 0), reverse=True)[:10]

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
        "recent_sales": recent_sales,
        "reviews": sorted_reviews,
        "support_history": SUPPORT_MESSAGES.get(user_id, []),
    }

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
    user_label = _format_user_label(user)
    return (
        f"🌑 <b>Привет, {user_label} — добро пожаловать в Dark Store.</b>\n\n"
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

def owner_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Залить в {item['title']} ({cnt} шт.)",
                callback_data=f"staff_add:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="📁 Новая позиция", callback_data="owner_new_cat")])
    rows.append([InlineKeyboardButton(text="💰 Выдать баланс (₽)", callback_data="owner_give_bal")])
    rows.append([
        InlineKeyboardButton(text="➕ Выдать админку", callback_data="owner_add_adm"),
        InlineKeyboardButton(text="➖ Снять админку", callback_data="owner_del_adm"),
    ])
    rows.append([InlineKeyboardButton(text="🔎 Смотреть баланс по ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Залить в {item['title']} ({cnt} шт.)",
                callback_data=f"staff_add:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🔎 Смотреть баланс по ID", callback_data="staff_check_bal")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def catalog_items_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        if cnt > 0:
            rows.append([
                InlineKeyboardButton(
                    text=f"{item['title']} — {item['price_rub']:.0f} ₽ ({cnt} шт.)",
                    callback_data=f"item_view:{key}",
                )
            ])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def item_view_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Купить за ₽", callback_data=f"buy_acc:{key}")],
            [InlineKeyboardButton(text="⬅️ В каталог", callback_data="menu_catalog")],
        ]
    )

def issued_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📩 Получить / запросить код входа", callback_data="req_code")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
        ]
    )

def deposit_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ Звёзды → ₽", callback_data="dep_method:stars"),
                InlineKeyboardButton(text="💎 TON → ₽", callback_data="dep_method:ton"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
        ]
    )

def back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")],
        ]
    )

if not BOT_TOKEN:
    print("ВНИМАНИЕ: BOT_TOKEN не задан в переменных окружения.")

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
        await message.answer("Формат: <code>/reply USER_ID ТЕКСТ</code>", parse_mode="HTML")
        return

    target_uid = int(parts[1])
    reply_text = parts[2].strip()
    
    SUPPORT_MESSAGES.setdefault(target_uid, []).append({
        "from": "admin",
        "text": reply_text,
        "time": time.time()
    })
    await save_data()
    await message.answer(f"✅ Ответ отправлен пользователю <code>{target_uid}</code> в мини-апп.", parse_mode="HTML")
    try:
        await bot.send_message(target_uid, f"💬 <b>Ответ поддержки:</b>\n{reply_text}", parse_mode="HTML")
    except Exception:
        pass

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
    await message.answer(f"✅ Код <code>{code}</code> отправлен <code>{uid}</code>", parse_mode="HTML")

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
        f"📜 <b>Правила магазина</b>\n\n"
        "1. Проверяйте аккаунты сразу после выдачи.\n"
        "2. Пополнение звёздами и TON зачисляется на единый баланс в ₽.\n"
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
    adm_list = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "нет"
    lines = [
        "👑 <b>Панель владельца</b>",
        f"Админы: <code>{adm_list}</code>",
        f"Всего продано: <b>{TOTAL_SOLD_COUNT} шт.</b>\n",
        "<b>Позиции:</b>",
    ]
    if not CATALOG:
        lines.append("• Позиций пока нет — создай через «Новая позиция».")
    else:
        for k, v in CATALOG.items():
            lines.append(
                f"• <b>{v['title']}</b> (<code>{k}</code>): <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽"
            )
    lines.append("\n<b>Выдача кода:</b> <code>/code &lt;user_id&gt; &lt;КОД&gt;</code>")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=owner_panel_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "staff_panel")
async def cb_staff_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    WAITING_INPUT.pop(callback.from_user.id, None)
    lines = [
        "🛠 <b>Админка</b>",
        "Доступно: просмотр баланса по ID и залив аккаунтов.\n\n<b>Позиции:</b>",
    ]
    if not CATALOG:
        lines.append("• Позиций пока нет.")
    else:
        for k, v in CATALOG.items():
            lines.append(
                f"• <b>{v['title']}</b>: <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽"
            )
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "owner_new_cat")
async def cb_owner_new_cat(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_new_cat"
    await callback.message.edit_text(
        "📁 <b>Новая позиция</b>\n\n"
        "Формат: <code>ключ|Название|Цена_в_рублях|Описание</code>\n"
        "Пример: <code>de_acc|DE +49|190|Аккаунт Германии</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "owner_give_bal")
async def cb_owner_give_bal(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_give_bal"
    await callback.message.edit_text(
        "💰 <b>Выдача баланса (в ₽)</b>\n\n"
        "Формат: <code>USER_ID СУММА_РУБЛЕЙ</code>\n"
        "Пример: <code>123456789 500</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "owner_add_adm")
async def cb_owner_add_adm(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_add_adm"
    await callback.message.edit_text(
        "➕ <b>Выдача админки</b>\n\nОтправь числовой <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "owner_del_adm")
async def cb_owner_del_adm(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = "owner_del_adm"
    await callback.message.edit_text(
        "➖ <b>Снятие админки</b>\n\nОтправь числовой <code>USER_ID</code>:",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()

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

@dp.callback_query(F.data.startswith("staff_add:"))
async def cb_staff_add(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    WAITING_INPUT[callback.from_user.id] = f"staff_add:{key}"
    await callback.message.edit_text(
        f"➕ <b>Залив в {item['title']}</b>\n\n"
        "Отправь аккаунты (каждый с новой строки):\n"
        "<code>+12025550199:StringSession:2FA</code>\n"
        "или <code>+12025550199:2FA</code> / <code>+12025550199</code>",
        parse_mode="HTML",
        reply_markup=back_main_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    has_items = any(len(v.get("accounts", [])) > 0 for v in CATALOG.values())
    if not has_items:
        await callback.message.edit_text(
            "🛒 <b>Каталог</b>\n\nСейчас в наличии нет позиций.",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "🛒 <b>Каталог</b>\n\nВыберите позицию:",
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
    text = (
        f"📦 <b>{item['title']}</b>\n\n"
        f"{item.get('description', '')}\n\n"
        f"💰 <b>Цена:</b> {item['price_rub']:.2f} ₽\n"
        f"📊 <b>В наличии:</b> {len(item['accounts'])} шт."
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
        await callback.answer("Не так быстро", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("Нет в наличии", show_alert=True)
        return

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        await callback.answer(
            f"Недостаточно средств ({price:.2f} ₽). Пополни баланс!",
            show_alert=True,
        )
        return

    acc = item["accounts"].pop(0)
    if not await deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        await callback.answer("Ошибка списания", show_alert=True)
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
    await save_data()

    two_fa_line = f"\n🔐 <b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""
    await callback.message.edit_text(
        f"✅ <b>Аккаунт выдан!</b>\n\n"
        f"<b>Заказ:</b> <code>{order_id}</code>\n"
        f"<b>Позиция:</b> {item['title']}\n"
        f"<b>Номер:</b> <code>{phone}</code>{two_fa_line}\n\n"
        "Нажми <b>«Получить / запросить код входа»</b> для входа.",
        parse_mode="HTML",
        reply_markup=issued_account_keyboard(),
    )
    await callback.answer("Выдано!")

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейма"
    await notify_owner(
        callback.bot,
        f"🛒 <b>Покупка!</b>\n"
        f"Юзер: {uname} (<code>{uid}</code>)\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Позиция: {item['title']} — <b>{price:.2f} ₽</b>\n"
        f"Номер: <code>{phone}</code>\n"
        f"Остаток: <b>{len(item['accounts'])} шт.</b>",
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
        for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
            if isinstance(rec, dict) and rec.get("order_id") == acc.get("order_id"):
                rec["code"] = code
                break
        await save_data()
        await callback.message.answer(
            f"🔑 <b>Код входа для</b> <code>{acc['phone']}</code>: <code>{code}</code>",
            parse_mode="HTML",
        )
        await callback.answer("Код получен!")
        return

    await callback.message.answer(
        f"⏳ Запрос кода по номеру <code>{acc['phone']}</code> отправлен.",
        parse_mode="HTML",
    )
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
    cnt = len(PURCHASE_HISTORY.get(uid, []))
    await callback.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"Роль: <b>{role}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Баланс: <b>{get_rub_balance(uid):.2f} ₽</b>\n"
        f"Покупок: <b>{cnt}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(uid),
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_deposit")
async def cb_menu_deposit(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.edit_text(
        f"💳 <b>Пополнение баланса (в ₽)</b>\n\n"
        f"Текущий баланс: <b>{get_rub_balance(uid):.2f} ₽</b>\n"
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
        await callback.message.edit_text(
            f"⭐ <b>Пополнение звёздами (в ₽)</b>\n\n"
            f"Курс: <b>1 ⭐ = {RUB_PER_STAR} ₽</b>\n"
            f"Напиши целое число звёзд для выставления счёта:",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"💎 <b>Пополнение TON (в ₽)</b>\n\n"
            f"Курс: <b>1 TON = {RUB_PER_TON} ₽</b>\n"
            f"Кошелёк: <code>{CRYPTO_WALLET}</code>\n\n"
            f"Напиши сумму в TON — заявка уйдёт владельцу на проверку перевода:",
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
        if txt and not txt.startswith("/"):
            SUPPORT_MESSAGES.setdefault(uid, []).append({
                "from": "user",
                "text": txt,
                "time": time.time()
            })
            await save_data()
            await message.answer("✅ Сообщение отправлено в поддержку. Ожидайте ответа.")
            await notify_owner(message.bot, f"🆘 <b>Сообщение в поддержку</b> от <code>{uid}</code>:\n{txt}")
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
        rub_est = round(ton_amt * RUB_PER_TON, 2)
        await message.answer(
            f"💎 <b>Заявка на пополнение TON отправлена</b>\n\n"
            f"Сумма: <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} ₽</b>)\n"
            f"Кошелёк: <code>{CRYPTO_WALLET}</code>\n"
            "Баланс будет зачислен после проверки перевода.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        await notify_owner(
            message.bot,
            f"💎 <b>Заявка TON</b> от <code>{uid}</code>: <b>{ton_amt} TON</b> (~<b>{rub_est:.2f} ₽</b>)\n"
            f"Выдать через панель: <code>{uid} {rub_est}</code>",
        )
        return

    if wait == "staff_check_bal" and is_admin(uid):
        if not txt.isdigit():
            await message.answer("Нужен числовой ID.")
            return
        WAITING_INPUT.pop(uid, None)
        target_id = int(txt)
        await message.answer(
            f"💰 Баланс пользователя <code>{target_id}</code>: <b>{get_rub_balance(target_id):.2f} ₽</b>",
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
            f"✅ Залито <b>{added} шт.</b> в <b>{item['title']}</b> (всего: {len(item['accounts'])} шт.).",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if not is_owner(uid):
        return

    if wait == "owner_new_cat":
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
        new_bal = await add_rub_balance(target_id, rub_amt)
        await message.answer(
            f"✅ Выдано <b>{rub_amt:.2f} ₽</b> пользователю <code>{target_id}</code>. Баланс: <b>{new_bal:.2f} ₽</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait == "owner_add_adm":
        if not txt.isdigit():
            await message.answer("Нужен числовой USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        ADMIN_IDS.add(adm_id)
        await save_data()
        await message.answer(
            f"✅ Пользователь <code>{adm_id}</code> назначен админом.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if wait == "owner_del_adm":
        if not txt.isdigit():
            await message.answer("Нужен числовой USER_ID.")
            return
        WAITING_INPUT.pop(uid, None)
        adm_id = int(txt)
        ADMIN_IDS.discard(adm_id)
        await save_data()
        await message.answer(
            f"✅ Админка у <code>{adm_id}</code> снята.",
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
        return _cors_response({"ok": False, "error": "Слишком частые запросы"}, status=429)

    item = CATALOG.get(item_key)
    if not item or not item.get("accounts"):
        return _cors_response({"ok": False, "error": "Нет в наличии"}, status=400)

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        return _cors_response({"ok": False, "error": "Недостаточно средств"}, status=400)

    acc = item["accounts"].pop(0)
    if not await deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        return _cors_response({"ok": False, "error": "Ошибка списания"}, status=400)

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
    await save_data()

    if bot:
        await notify_owner(bot, f"🛒 MiniApp покупка от <code>{uid}</code> • <code>{order_id}</code> • <code>{phone}</code>")
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
        return _cors_response({"ok": False, "error": "Некорректный JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    method = str(data.get("method") or "").strip().lower()
    send_to_bot = bool(data.get("send_to_bot", True))
    try:
        amount = float(str(data.get("amount") or "0").replace(",", "."))
    except (ValueError, TypeError):
        amount = 0.0

    if uid <= 0 or amount <= 0 or method not in ("stars", "ton"):
        return _cors_response({"ok": False, "error": "Проверьте сумму и способ оплаты"}, status=400)

    if not check_rate_limit(uid, "api_dep", 1.0):
        return _cors_response({"ok": False, "error": "Подождите секунду и попробуйте снова"}, status=429)

    if method == "stars":
        stars_cnt = max(1, int(round(amount)))
        rub_preview = round(stars_cnt * RUB_PER_STAR, 2)
        if not bot:
            return _cors_response({"ok": False, "error": "BOT_TOKEN не настроен на сервере"}, status=500)
        try:
            if send_to_bot:
                await bot.send_invoice(
                    chat_id=uid,
                    title=f"Пополнение баланса ({rub_preview:.2f} ₽)",
                    description=f"Зачисление {rub_preview:.2f} ₽ на баланс за {stars_cnt} ⭐",
                    payload=f"rub_stars:{uid}:{stars_cnt}",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"{stars_cnt} ⭐", amount=stars_cnt)],
                )
                return _cors_response({
                    "ok": True,
                    "pending_payment": True,
                    "sent_to_bot": True,
                    "balance_rub": get_rub_balance(uid),
                })

            invoice_link = await bot.create_invoice_link(
                title=f"Пополнение баланса ({rub_preview:.2f} ₽)",
                description=f"Зачисление {rub_preview:.2f} ₽ за {stars_cnt} ⭐",
                payload=f"rub_stars:{uid}:{stars_cnt}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{stars_cnt} ⭐", amount=stars_cnt)],
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
            f"💎 <b>Заявка TON из DarkHub</b> от <code>{uid}</code>: <b>{amount} TON</b> (~<b>{rub_est:.2f} ₽</b>)\n"
            f"Выдать: <code>{uid} {rub_est}</code>",
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
        return _cors_response({"ok": False, "error": "Слишком частые запросы кода"}, status=429)

    target = None
    for rec in reversed(PURCHASE_HISTORY.get(uid, [])):
        if isinstance(rec, dict) and (not order_id or rec.get("order_id") == order_id):
            target = rec
            break

    if not target:
        target = LAST_ISSUED_ACCOUNT.get(uid)

    if not target:
        return _cors_response({"ok": False, "error": "Заказ не найден"}, status=404)

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
            f"📩 MiniApp запрос кода от <code>{uid}</code> (<code>{target.get('phone')}</code>).\n"
            f"Отправь: <code>/code {uid} КОД</code>",
        )
    return _cors_response({"ok": False, "error": "Запрос отправлен, код скоро придёт"})

async def api_support_send(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)
    
    uid = int(data.get("user_id") or 0)
    text = str(data.get("text") or "").strip()
    if uid <= 0 or not text:
        return _cors_response({"ok": False, "error": "Missing user_id or text"}, status=400)
        
    SUPPORT_MESSAGES.setdefault(uid, []).append({
        "from": "user",
        "text": text,
        "time": time.time()
    })
    await save_data()
    if bot:
        await notify_owner(bot, f"🆘 <b>Новое сообщение в поддержку</b> от <code>{uid}</code>:\n{text}")
        
    return _cors_response({"ok": True})

async def api_support_get(request: web.Request) -> web.Response:
    uid_raw = request.query.get("user_id", "").strip()
    if not uid_raw.isdigit():
        return _cors_response({"ok": False, "error": "Invalid user_id"}, status=400)
    uid = int(uid_raw)
    return _cors_response({"ok": True, "messages": SUPPORT_MESSAGES.get(uid, [])})

async def api_review_send(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)
        
    uid = int(data.get("user_id") or 0)
    text = str(data.get("text") or "").strip()
    rating = int(data.get("rating") or 5)
    if uid <= 0 or not text:
        return _cors_response({"ok": False, "error": "Missing user_id or text"}, status=400)
        
    REVIEWS.append({
        "user_id": uid,
        "text": text,
        "rating": max(1, min(5, rating)),
        "timestamp": time.time()
    })
    await save_data()
    return _cors_response({"ok": True})

async def start_http_server() -> web.AppRunner:
    app = web.Application()
    for path in ("/api/state", "/api/buy", "/api/deposit", "/api/code", "/api/support/send", "/api/support/get", "/api/review/send"):
        app.router.add_options(path, api_options)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/deposit", api_deposit)
    app.router.add_post("/api/code", api_code)
    app.router.add_post("/api/support/send", api_support_send)
    app.router.add_get("/api/support/get", api_support_get)
    app.router.add_post("/api/review/send", api_review_send)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner

async def main() -> None:
    await init_db()
    await load_data()
    runner = await start_http_server()
    try:
        if bot:
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        if DB_POOL:
            await DB_POOL.close()

if __name__ == "__main__":
    asyncio.run(main())