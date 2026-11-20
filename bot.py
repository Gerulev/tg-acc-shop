import asyncio
import base64
import math
import os
import re
import secrets
import time
from aiohttp import web, ClientSession
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_WALLET = os.getenv(
    "CRYPTO_WALLET",
    "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux",
)
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
SUPPORT_USERNAME = "gerulev"
ADMIN_ID = int(os.getenv("ADMIN_ID", "622034883"))

# Курсы для отображения баланса в рублях (₽) в профиле
RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6"))
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "450.0"))

# Каталог товаров и пул свободных аккаунтов
# Формат элемента в accounts: {"id": str, "phone": str, "secret": str}
CATALOG: dict[str, dict] = {
    "uk_2016": {
        "title": "UK 2016",
        "price_rub": 250.0,
        "description": "Аккаунт UK 2016. После покупки выдаётся номер и кнопка получения/запроса кода входа.",
        "accounts": [],
    }
}

# user_id -> "waiting_stars" | "waiting_ton" | "adm_add:<key>" | "adm_new_cat"
WAITING_INPUT: dict[int, str] = {}

# user_id -> {"comment": str, "amount_ton": float, "created_at": int}
PENDING_DEPOSITS: dict[int, dict] = {}

# user_id -> {"item_key": str, "comment": str, "amount_ton": float, "created_at": int}
PENDING_ORDERS: dict[int, dict] = {}

# user_id -> {"order_id": str, "item_title": str, "phone": str, "session_string": str}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}

PURCHASE_HISTORY: dict[int, list[str]] = {}
USER_STARS_BALANCE: dict[int, int] = {}
USER_TON_BALANCE: dict[int, float] = {}
USED_TX_KEYS: set[str] = set()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception:
        pass


def get_rub_balance(user_id: int) -> float:
    stars = USER_STARS_BALANCE.get(user_id, 0)
    ton = USER_TON_BALANCE.get(user_id, 0.0)
    return round(stars * RUB_PER_STAR + ton * RUB_PER_TON, 2)


def deduct_rub_balance(user_id: int, amount_rub: float) -> bool:
    if get_rub_balance(user_id) < amount_rub:
        return False

    rem = round(amount_rub, 2)

    ton_bal = USER_TON_BALANCE.get(user_id, 0.0)
    if ton_bal > 0 and rem > 0 and RUB_PER_TON > 0:
        ton_val = ton_bal * RUB_PER_TON
        if ton_val >= rem:
            spent_ton = round(rem / RUB_PER_TON, 4)
            USER_TON_BALANCE[user_id] = round(max(0.0, ton_bal - spent_ton), 4)
            rem = 0.0
        else:
            USER_TON_BALANCE[user_id] = 0.0
            rem = round(rem - ton_val, 2)

    stars_bal = USER_STARS_BALANCE.get(user_id, 0)
    if stars_bal > 0 and rem > 0 and RUB_PER_STAR > 0:
        need = math.ceil(rem / RUB_PER_STAR)
        USER_STARS_BALANCE[user_id] = max(0, stars_bal - need)

    return True


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
            txt = msg.message or ""
            match = re.search(r"\b(\d{5})\b", txt)
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


def main_menu_keyboard(user_id: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛒 КАТАЛОГ / МАГАЗИН", callback_data="menu_catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton(text="💳 Пополнить", callback_data="menu_deposit"),
        ],
        [
            InlineKeyboardButton(text="⭐ Отзывы", callback_data="menu_reviews"),
            InlineKeyboardButton(text="📜 Правила", callback_data="menu_rules"),
        ],
        [
            InlineKeyboardButton(
                text="🆘 Поддержка",
                url=f"https://t.me/{SUPPORT_USERNAME}",
            )
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админка", callback_data="adm_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Залить в {item['title']} ({cnt} шт.)",
                callback_data=f"adm_add:{key}",
            )
        ])
    rows.append([InlineKeyboardButton(text="📁 Новая позиция", callback_data="adm_new_cat")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_view_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Купить с баланса", callback_data=f"buy_acc:{key}")],
            [InlineKeyboardButton(text="⬅️ В каталог", callback_data="menu_catalog")],
        ]
    )


def issued_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📩 Получить / запросить код входа", callback_data="req_code")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
        ]
    )


def deposit_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ Через звёзды", callback_data="dep_method:stars"),
                InlineKeyboardButton(text="💎 Через TON", callback_data="dep_method:ton"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")],
        ]
    )


def deposit_ton_check_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил (проверить TON)", callback_data="check_ton_deposit")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_deposit")],
        ]
    )


def catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]
        ]
    )


def payment_keyboard(item_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил (проверить TON)", callback_data=f"check:{item_key}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ]
    )


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]]
    )


def extract_comment_from_in_msg(in_msg: dict) -> str:
    msg_data = in_msg.get("msg_data") or {}
    if msg_data.get("@type") == "msg.dataText" and isinstance(msg_data.get("text"), str):
        return msg_data["text"].strip()

    b64_body = msg_data.get("body") or in_msg.get("message")
    if isinstance(b64_body, str) and b64_body:
        try:
            raw = base64.b64decode(b64_body)
            text_part = raw[4:] if len(raw) > 4 else raw
            decoded = text_part.decode("utf-8", errors="ignore").strip()
            if decoded:
                return decoded
            return raw.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""
    return ""


async def verify_ton_payment(expected_ton: float, expected_comment: str, min_utime: int) -> bool:
    url = "https://toncenter.com/api/v2/getTransactions"
    params = {
        "address": CRYPTO_WALLET,
        "limit": 25,
        "archival": "false",
    }
    if TONCENTER_API_KEY:
        params["api_key"] = TONCENTER_API_KEY

    try:
        async with ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
    except Exception:
        return False

    if not data.get("ok"):
        return False

    transactions = data.get("result") or []
    expected_nano = int(round(expected_ton * 1_000_000_000))

    for tx in transactions:
        utime = int(tx.get("utime") or 0)
        if utime < min_utime - 60:
            continue

        tx_id = tx.get("transaction_id") or {}
        tx_hash = str(tx_id.get("hash") or "")
        tx_lt = str(tx_id.get("lt") or "")
        tx_key = f"{tx_lt}:{tx_hash}"
        if tx_key in USED_TX_KEYS:
            continue

        in_msg = tx.get("in_msg") or {}
        value_nano = int(in_msg.get("value") or 0)
        if value_nano < int(expected_nano * 0.998):
            continue

        comment = extract_comment_from_in_msg(in_msg)
        if expected_comment in comment:
            USED_TX_KEYS.add(tx_key)
            return True

    return False


dp = Dispatcher()


def get_welcome_text(user_first_name: str) -> str:
    return (
        f"📱 <b>Привет, {user_first_name}!</b> 🎤\n\n"
        "Это лучший и самый быстрый магазин цифровых аккаунтов по самым выгодным ценам! ⚡️🌹\n\n"
        "<b>Что у нас есть:</b>\n"
        "💎 <i>Аккаунты с большой датой регистрации</i>\n"
        "🎯 <i>Аккаунты под любые ваши цели</i>\n"
        "⚡️ <i>Автореги и самореги с моментальной выдачей</i>\n\n"
        "🔒 <i>Перед покупкой рекомендуется ознакомиться с правилами магазина.</i>\n\n"
        f"🆘 <b>Поддержка:</b> @{SUPPORT_USERNAME}\n"
        "Пользуясь ботом, вы принимаете Пользовательское соглашение."
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    WAITING_INPUT.pop(message.from_user.id, None)
    first_name = message.from_user.first_name or "Покупатель"
    await message.answer(
        get_welcome_text(first_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@dp.message(Command("code"))
async def cmd_code(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/code 123456789 КОД</code>", parse_mode="HTML")
        return

    uid = int(parts[1])
    code = parts[2].strip()
    acc = LAST_ISSUED_ACCOUNT.get(uid) or {}
    phone = acc.get("phone", "аккаунта")

    try:
        await message.bot.send_message(
            chat_id=uid,
            text=f"🔑 <b>Код входа:</b> <code>{code}</code>\n<b>Номер:</b> <code>{phone}</code>",
            parse_mode="HTML",
        )
        await message.answer(f"✅ Код <code>{code}</code> отправлен юзеру <code>{uid}</code>", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"❌ Ошибка отправки: <code>{exc}</code>", parse_mode="HTML")


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    first_name = callback.from_user.first_name or "Покупатель"
    await callback.message.edit_text(
        get_welcome_text(first_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_panel")
async def cb_adm_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    WAITING_INPUT.pop(callback.from_user.id, None)
    lines = ["🛠 <b>Админ-панель</b>\n\n<b>Наличие:</b>"]
    for k, v in CATALOG.items():
        lines.append(f"• <b>{v['title']}</b> (<code>{k}</code>): <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽")

    lines.append(
        "\n<b>Формат загрузки:</b> по одному аккаунту на строку\n"
        "<code>+447700900123:StringSession</code> или просто <code>+447700900123</code>\n"
        "<b>Ручной код:</b> <code>/code &lt;user_id&gt; &lt;КОД&gt;</code>"
    )
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_add:"))
async def cb_adm_add(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    WAITING_INPUT[callback.from_user.id] = f"adm_add:{key}"
    await callback.message.edit_text(
        f"➕ <b>Загрузка в {item['title']}</b>\n\n"
        "Отправь список аккаунтов (каждый с новой строки):\n"
        "<code>+447700900123:StringSession</code> или просто <code>+447700900123</code>",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_new_cat")
async def cb_adm_new_cat(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    WAITING_INPUT[callback.from_user.id] = "adm_new_cat"
    await callback.message.edit_text(
        "📁 <b>Новая позиция</b>\n\n"
        "Отправь строку:\n"
        "<code>ключ|Название|Цена_в_рублях|Описание</code>\n\n"
        "Пример:\n"
        "<code>ru_2018|RU 2018|200|Аккаунт 2018 года</code>",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    has_any = any(len(v.get("accounts", [])) > 0 for v in CATALOG.values())
    if not has_any:
        await callback.message.edit_text(
            "🛍 <b>Каталог</b>\n\nСейчас в каталоге пусто — аккаунтов в наличии нет, выбора пока нет.",
            parse_mode="HTML",
            reply_markup=catalog_keyboard(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "🛍 <b>Каталог</b>\n\nВыберите позицию:",
        parse_mode="HTML",
        reply_markup=catalog_items_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("item_view:"))
async def cb_item_view(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return

    cnt = len(item.get("accounts", []))
    if cnt <= 0:
        await callback.answer("Нет в наличии", show_alert=True)
        return

    text = (
        f"📦 <b>{item['title']}</b>\n\n"
        f"{item.get('description', '')}\n\n"
        f"💰 <b>Цена:</b> {item['price_rub']:.2f} ₽\n"
        f"📊 <b>В наличии:</b> {cnt} шт."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=item_view_keyboard(key),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_acc:"))
async def cb_buy_acc(callback: CallbackQuery):
    uid = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("Сейчас нет товаров в наличии", show_alert=True)
        return

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        await callback.answer(
            f"Недостаточно средств ({price:.2f} ₽). Пополни баланс!",
            show_alert=True,
        )
        return

    acc = item["accounts"].pop(0)
    if not deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        await callback.answer("Ошибка списания баланса", show_alert=True)
        return

    order_id = f"ORD-{secrets.token_hex(3).upper()}"
    phone = acc.get("phone", "")
    secret_info = acc.get("secret", "")
    session_str = secret_info if len(secret_info) > 40 else ""

    LAST_ISSUED_ACCOUNT[uid] = {
        "order_id": order_id,
        "item_title": item["title"],
        "phone": phone,
        "session_string": session_str,
    }
    PURCHASE_HISTORY.setdefault(uid, []).append(f"{order_id}: {item['title']} ({phone})")

    extra = f"\n🔑 <b>Доп. инфо:</b> <code>{secret_info}</code>" if (secret_info and not session_str) else ""
    await callback.message.edit_text(
        f"✅ <b>Аккаунт выдан!</b>\n\n"
        f"<b>Заказ:</b> <code>{order_id}</code>\n"
        f"<b>Товар:</b> {item['title']}\n"
        f"<b>Номер:</b> <code>{phone}</code>{extra}\n\n"
        "Введи номер при входе и нажми <b>«Получить / запросить код входа»</b>.",
        parse_mode="HTML",
        reply_markup=issued_account_keyboard(),
    )
    await callback.answer("Выдано!")

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейма"
    await notify_admin(
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
    acc = LAST_ISSUED_ACCOUNT.get(uid)
    if not acc:
        await callback.answer("Нет выданного аккаунта", show_alert=True)
        return

    session_str = acc.get("session_string") or ""
    if session_str:
        await callback.answer("Проверяю входящий код...")
        code = await fetch_tg_login_code(session_str)
        if code:
            await callback.message.answer(
                f"🔑 <b>Код входа получен автоматически!</b>\n\n"
                f"<b>Номер:</b> <code>{acc['phone']}</code>\n"
                f"<b>Код:</b> <code>{code}</code>",
                parse_mode="HTML",
            )
            return

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейма"
    await notify_admin(
        callback.bot,
        f"📩 <b>Запрос кода!</b>\n"
        f"Юзер: {uname} (<code>{uid}</code>)\n"
        f"Номер: <code>{acc['phone']}</code>\n"
        f"Отправить: <code>/code {uid} КОД</code>",
    )
    await callback.answer("Код ещё не пришёл или нет сессии — запрос ушёл админу!", show_alert=True)


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    user_id = callback.from_user.id
    rub_balance = get_rub_balance(user_id)
    stars_balance = USER_STARS_BALANCE.get(user_id, 0)
    ton_balance = USER_TON_BALANCE.get(user_id, 0.0)
    orders = PURCHASE_HISTORY.get(user_id, [])

    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"💰 <b>Баланс:</b> <b>{rub_balance:.2f} ₽</b>\n"
        f"<i>(⭐ {stars_balance} • 💎 {ton_balance:.4f} TON)</i>\n"
        f"🛒 <b>Куплено товаров:</b> {len(orders)} шт.\n"
    )
    if orders:
        text += "\n<b>История покупок:</b>\n" + "\n".join(f"• <code>{o}</code>" for o in orders)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_deposit")
async def cb_menu_deposit(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    user_id = callback.from_user.id
    rub_balance = get_rub_balance(user_id)

    text = (
        "💳 <b>Пополнение баланса</b>\n\n"
        f"Текущий баланс: <b>{rub_balance:.2f} ₽</b>\n\n"
        "Выбери способ пополнения:"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=deposit_choice_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "dep_method:stars")
async def cb_dep_method_stars(callback: CallbackQuery):
    user_id = callback.from_user.id
    WAITING_INPUT[user_id] = "waiting_stars"

    text = (
        "⭐ <b>Пополнение через звёзды (XTR)</b>\n\n"
        "Напиши в чат <b>целое число звёзд</b>, на которое хочешь пополнить баланс.\n"
        "Например: <code>10</code> или <code>50</code>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "dep_method:ton")
async def cb_dep_method_ton(callback: CallbackQuery):
    user_id = callback.from_user.id
    WAITING_INPUT[user_id] = "waiting_ton"

    text = (
        "💎 <b>Пополнение через TON</b>\n\n"
        "Напиши в чат <b>сумму в TON</b>, на которую хочешь пополнить баланс.\n"
        "Например: <code>0.5</code> или <code>2</code>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_deposit_amount_input(message: Message):
    user_id = message.from_user.id
    mode = WAITING_INPUT.get(user_id)
    if not mode:
        return

    raw_text = (message.text or "").strip()

    if mode.startswith("adm_add:") and is_admin(user_id):
        key = mode.split(":", 1)[1]
        item = CATALOG.get(key)
        WAITING_INPUT.pop(user_id, None)
        if not item:
            await message.answer("Категория не найдена.", reply_markup=admin_panel_keyboard())
            return

        lines = [x.strip() for x in raw_text.splitlines() if x.strip()]
        added = 0
        for ln in lines:
            if ":" in ln:
                phone, secret = ln.split(":", 1)
                phone, secret = phone.strip(), secret.strip()
            else:
                phone, secret = ln, ""
            if phone:
                item.setdefault("accounts", []).append({
                    "id": f"ACC-{secrets.token_hex(2).upper()}",
                    "phone": phone,
                    "secret": secret,
                })
                added += 1

        await message.answer(
            f"✅ Загружено <b>{added} шт.</b> в <b>{item['title']}</b>.\n"
            f"Всего в наличии: <b>{len(item['accounts'])} шт.</b>",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return

    if mode == "adm_new_cat" and is_admin(user_id):
        WAITING_INPUT.pop(user_id, None)
        parts = [p.strip() for p in raw_text.split("|")]
        if len(parts) < 3:
            await message.answer(
                "Формат: <code>ключ|Название|Цена|Описание</code>",
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
            return

        cat_key, cat_title, price_raw = parts[0], parts[1], parts[2].replace(",", ".")
        cat_desc = parts[3] if len(parts) > 3 else ""
        try:
            cat_price = float(price_raw)
        except ValueError:
            await message.answer("Цена должна быть числом.", reply_markup=admin_panel_keyboard())
            return

        CATALOG[cat_key] = {
            "title": cat_title,
            "price_rub": cat_price,
            "description": cat_desc,
            "accounts": [],
        }
        await message.answer(
            f"✅ Добавлена позиция <b>{cat_title}</b> (<code>{cat_key}</code>) — <b>{cat_price:.2f} ₽</b>.",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return

    norm_num = raw_text.replace(",", ".")

    if mode == "waiting_stars":
        if not norm_num.isdigit():
            await message.answer(
                "Введите целое число звёзд, например: <code>15</code>",
                parse_mode="HTML",
            )
            return

        stars_amount = int(norm_num)
        if stars_amount < 1 or stars_amount > 100000:
            await message.answer("Укажите число звёзд от 1 до 100000.")
            return

        WAITING_INPUT.pop(user_id, None)
        await message.bot.send_invoice(
            chat_id=user_id,
            title=f"Пополнение баланса ({stars_amount} ⭐)",
            description=f"Зачисление {stars_amount} звёзд на ваш баланс.",
            payload=f"deposit_stars:{stars_amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars_amount} ⭐", amount=stars_amount)],
        )
        return

    if mode == "waiting_ton":
        try:
            ton_amount = round(float(norm_num), 4)
        except ValueError:
            await message.answer(
                "Введите корректную сумму в TON, например: <code>0.5</code>",
                parse_mode="HTML",
            )
            return

        if ton_amount <= 0 or ton_amount > 100000:
            await message.answer("Сумма в TON должна быть больше 0.")
            return

        WAITING_INPUT.pop(user_id, None)
        comment = f"DEP{user_id}{secrets.token_hex(2).upper()}"
        PENDING_DEPOSITS[user_id] = {
            "comment": comment,
            "amount_ton": ton_amount,
            "created_at": int(time.time()),
        }

        await message.answer(
            "💎 <b>Счёт на пополнение в TON создан</b>\n\n"
            f"<b>Сумма:</b> <code>{ton_amount}</code> TON\n"
            f"<b>Кошелёк:</b>\n<code>{CRYPTO_WALLET}</code>\n"
            f"<b>Обязательный комментарий:</b>\n<code>{comment}</code>\n\n"
            "Переведи точную сумму с указанным комментарием и нажми кнопку проверки ниже.",
            parse_mode="HTML",
            reply_markup=deposit_ton_check_keyboard(),
        )


@dp.callback_query(F.data == "check_ton_deposit")
async def cb_check_ton_deposit(callback: CallbackQuery):
    user_id = callback.from_user.id
    deposit = PENDING_DEPOSITS.get(user_id)
    if not deposit:
        await callback.answer("Заявка на пополнение не найдена", show_alert=True)
        return

    await callback.answer("Проверяем перевод в сети TON...")

    paid = await verify_ton_payment(
        expected_ton=deposit["amount_ton"],
        expected_comment=deposit["comment"],
        min_utime=deposit["created_at"],
    )

    if not paid:
        await callback.answer(
            "Перевод пока не найден. Проверь сумму и комментарий и попробуй через 10–15 сек.",
            show_alert=True,
        )
        return

    added_ton = float(deposit["amount_ton"])
    PENDING_DEPOSITS.pop(user_id, None)
    new_ton = round(USER_TON_BALANCE.get(user_id, 0.0) + added_ton, 4)
    USER_TON_BALANCE[user_id] = new_ton
    new_rub = get_rub_balance(user_id)

    await callback.message.edit_text(
        f"✅ <b>Обновление баланса</b>\n\n"
        f"Начислено: <b>+{added_ton} TON</b>\n"
        f"Баланс: <b>{new_rub:.2f} ₽</b> <i>(💎 {new_ton:.4f} TON)</i>",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )

    uname = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейма"
    await notify_admin(
        callback.bot,
        f"💎 <b>Пополнение TON!</b>\n"
        f"Юзер: {uname} (<code>{user_id}</code>)\n"
        f"Начислено: <b>+{added_ton} TON</b>\n"
        f"Баланс: <b>{new_rub:.2f} ₽</b>",
    )


@dp.callback_query(F.data == "cancel_deposit")
async def cb_cancel_deposit(callback: CallbackQuery):
    user_id = callback.from_user.id
    WAITING_INPUT.pop(user_id, None)
    PENDING_DEPOSITS.pop(user_id, None)
    await callback.message.edit_text(
        "Пополнение отменено.",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.pre_checkout_query()
async def on_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    if payment.currency != "XTR":
        return

    payload = payment.invoice_payload or ""
    if payload.startswith("deposit_stars:"):
        try:
            added_stars = int(payload.split(":", 1)[1])
        except ValueError:
            added_stars = payment.total_amount

        user_id = message.from_user.id
        new_stars = USER_STARS_BALANCE.get(user_id, 0) + added_stars
        USER_STARS_BALANCE[user_id] = new_stars
        new_rub = get_rub_balance(user_id)

        await message.answer(
            f"⭐️ <b>Обновление баланса</b>\n\n"
            f"Начислено: <b>+{added_stars} ⭐</b>\n"
            f"Баланс: <b>{new_rub:.2f} ₽</b> <i>(⭐ {new_stars})</i>",
            parse_mode="HTML",
            reply_markup=back_to_main_keyboard(),
        )

        uname = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
        await notify_admin(
            message.bot,
            f"⭐ <b>Пополнение Stars!</b>\n"
            f"Юзер: {uname} (<code>{user_id}</code>)\n"
            f"Начислено: <b>+{added_stars} ⭐</b>\n"
            f"Баланс: <b>{new_rub:.2f} ₽</b>",
        )


@dp.callback_query(F.data == "menu_rules")
async def cb_menu_rules(callback: CallbackQuery):
    text = (
        "📜 <b>Правила магазина</b>\n\n"
        "1. Гарантия на купленные аккаунты действует 20 минут с момента выдачи.\n"
        "2. Проверяйте товары сразу после получения.\n"
        "3. Возврат средств осуществляется только при наличии записи экрана с момента покупки."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_reviews")
async def cb_menu_reviews(callback: CallbackQuery):
    text = (
        "⭐ <b>Отзывы покупателей</b>\n\n"
        "Отзывы и новости нашего магазина публикуются в канале!\n"
        f"Напишите @{SUPPORT_USERNAME}, чтобы получить ссылку на канал."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    await callback.answer("Сейчас нет товаров в наличии", show_alert=True)


@dp.callback_query(F.data.startswith("check:"))
async def cb_check_payment(callback: CallbackQuery):
    await callback.answer("Заказ недоступен", show_alert=True)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery):
    PENDING_ORDERS.pop(callback.from_user.id, None)
    await callback.message.edit_text("Отменено.", reply_markup=back_to_main_keyboard())
    await callback.answer()


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")
    await start_health_server()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())