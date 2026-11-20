import asyncio
import base64
import math
import os
import re
import secrets
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CRYPTO_WALLET = os.getenv(
    "CRYPTO_WALLET",
    "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux",
)
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SUPPORT_USERNAME = "gerulev"
ADMIN_ID = int(os.getenv("ADMIN_ID", "622034883") or "622034883")

RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6") or "1.6")
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "450.0") or "450.0")

CATALOG: dict[str, dict] = {
    "uk_2016": {
        "title": "UK 2016",
        "price_rub": 250.0,
        "description": "Аккаунт UK 2016. После покупки выдаётся номер и кнопка получения/запроса кода входа.",
        "accounts": [],
    }
}

WAITING_INPUT: dict[int, str] = {}
PENDING_DEPOSITS: dict[int, dict] = {}
PENDING_ORDERS: dict[int, dict] = {}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[str]] = {}
USER_STARS_BALANCE: dict[int, int] = {}
USER_TON_BALANCE: dict[int, float] = {}
USED_TX_KEYS: set[str] = set()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def notify_admin(bot_instance: Bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot_instance.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
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
    rows.append([
        InlineKeyboardButton(text="💰 Выдать баланс", callback_data="adm_give_bal"),
        InlineKeyboardButton(text="🔎 Профиль по ID", callback_data="adm_check_user"),
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


def catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]
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


if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Render!")

bot = Bot(token=BOT_TOKEN)
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
    two_fa = acc.get("two_fa") or ""
    two_fa_text = f"\n🔐 <b>2FA пароль:</b> <code>{two_fa}</code>" if two_fa else ""

    try:
        await message.bot.send_message(
            chat_id=uid,
            text=f"🔑 <b>Код входа:</b> <code>{code}</code>\n<b>Номер:</b> <code>{phone}</code>{two_fa_text}",
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


@dp.callback_query(F.data == "menu_reviews")
async def cb_menu_reviews(callback: CallbackQuery):
    await callback.message.edit_text(
        f"⭐ <b>Отзывы и новости</b>\n\nВопросы и отзывы: @{SUPPORT_USERNAME}",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_rules")
async def cb_menu_rules(callback: CallbackQuery):
    await callback.message.edit_text(
        "📜 <b>Правила магазина</b>\n\n"
        "1. Проверяйте аккаунт сразу после получения.\n"
        "2. Замена возможна при проблемах до передачи/входа по согласованию с поддержкой.\n"
        f"3. По всем вопросам: @{SUPPORT_USERNAME}",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
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
        lines.append(
            f"• <b>{v['title']}</b> (<code>{k}</code>): <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽"
        )

    lines.append(
        "\n<b>Формат загрузки:</b> по одному аккаунту на строку\n"
        "<code>+447700900123:StringSession:2FA</code>\n"
        "или <code>+447700900123:2FA</code> / <code>+447700900123</code>\n"
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
        "<code>+447700900123:StringSession:2FA</code>\n"
        "или <code>+447700900123:2FA</code> / <code>+447700900123</code>",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_give_bal")
async def cb_adm_give_bal(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    WAITING_INPUT[callback.from_user.id] = "adm_give_bal"
    await callback.message.edit_text(
        "💰 <b>Выдача баланса</b>\n\n"
        "Отправь в формате:\n"
        "<code>ID_ЮЗЕРА СУММА_РУБЛЕЙ</code>\n\n"
        "Пример:\n"
        "<code>987654321 500</code>\n"
        "<i>(чтобы списать — укажи минус, например: <code>987654321 -100</code>)</i>",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_check_user")
async def cb_adm_check_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    WAITING_INPUT[callback.from_user.id] = "adm_check_user"
    await callback.message.edit_text(
        "🔎 <b>Проверка профиля по ID</b>\n\n"
        "Отправь числовой <code>user_id</code> покупателя:",
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
    session_str, two_fa = _parse_account_secret(acc.get("secret", ""))

    LAST_ISSUED_ACCOUNT[uid] = {
        "order_id": order_id,
        "item_title": item["title"],
        "phone": phone,
        "session_string": session_str,
        "two_fa": two_fa,
    }
    PURCHASE_HISTORY.setdefault(uid, []).append(f"{order_id}: {item['title']} ({phone})")

    two_fa_line = f"\n🔐 <b>2FA пароль:</b> <code>{two_fa}</code>" if two_fa else ""
    await callback.message.edit_text(
        f"✅ <b>Аккаунт выдан!</b>\n\n"
        f"<b>Заказ:</b> <code>{order_id}</code>\n"
        f"<b>Товар:</b> {item['title']}\n"
        f"<b>Номер:</b> <code>{phone}</code>{two_fa_line}\n\n"
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
            two_fa = acc.get("two_fa") or ""
            two_fa_text = f"\n🔐 <b>2FA пароль:</b> <code>{two_fa}</code>" if two_fa else ""
            await callback.message.answer(
                f"🔑 <b>Код входа получен автоматически!</b>\n\n"
                f"<b>Номер:</b> <code>{acc['phone']}</code>\n"
                f"<b>Код:</b> <code>{code}</code>{two_fa_text}",
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

    if mode == "adm_give_bal" and is_admin(user_id):
        WAITING_INPUT.pop(user_id, None)
        parts = raw_text.replace(",", ".").split()
        if len(parts) < 2 or not parts[0].isdigit():
            await message.answer(
                "Формат: <code>ID_ЮЗЕРА СУММА_РУБЛЕЙ</code>, например: <code>123456789 300</code>",
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
            return

        target_id = int(parts[0])
        try:
            delta_rub = round(float(parts[1]), 2)
        except ValueError:
            await message.answer("Сумма должна быть числом.", reply_markup=admin_panel_keyboard())
            return

        cur_rub = get_rub_balance(target_id)
        if delta_rub >= 0:
            add_stars = int(round(delta_rub / RUB_PER_STAR)) if RUB_PER_STAR > 0 else 0
            if add_stars > 0:
                USER_STARS_BALANCE[target_id] = USER_STARS_BALANCE.get(target_id, 0) + add_stars
            else:
                add_ton = round(delta_rub / RUB_PER_TON, 4) if RUB_PER_TON > 0 else delta_rub
                USER_TON_BALANCE[target_id] = round(USER_TON_BALANCE.get(target_id, 0.0) + add_ton, 4)
        else:
            deduct_rub_balance(target_id, abs(delta_rub))

        new_rub = get_rub_balance(target_id)
        await message.answer(
            f"✅ <b>Баланс обновлён!</b>\n\n"
            f"🆔 <b>ID:</b> <code>{target_id}</code>\n"
            f"Было: <b>{cur_rub:.2f} ₽</b>\n"
            f"Изменение: <b>{delta_rub:+.2f} ₽</b>\n"
            f"Стало: <b>{new_rub:.2f} ₽</b>",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        try:
            await message.bot.send_message(
                chat_id=target_id,
                text=f"💳 Ваш баланс изменён администратором на <b>{delta_rub:+.2f} ₽</b>.\nТекущий баланс: <b>{new_rub:.2f} ₽</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    if mode == "adm_check_user" and is_admin(user_id):
        WAITING_INPUT.pop(user_id, None)
        if not raw_text.isdigit():
            await message.answer(
                "Введи числовой <code>user_id</code>, например: <code>123456789</code>",
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
            return

        target_id = int(raw_text)
        rub_bal = get_rub_balance(target_id)
        stars_bal = USER_STARS_BALANCE.get(target_id, 0)
        ton_bal = USER_TON_BALANCE.get(target_id, 0.0)
        orders = PURCHASE_HISTORY.get(target_id, [])
        last_acc = LAST_ISSUED_ACCOUNT.get(target_id)

        text = (
            f"👤 <b>Профиль пользователя</b>\n\n"
            f"🆔 <b>ID:</b> <code>{target_id}</code>\n"
            f"💰 <b>Баланс:</b> <b>{rub_bal:.2f} ₽</b>\n"
            f"<i>(⭐ {stars_bal} • 💎 {ton_bal:.4f} TON)</i>\n"
            f"🛒 <b>Покупок:</b> {len(orders)} шт."
        )
        if last_acc:
            text += f"\n📱 <b>Последний номер:</b> <code>{last_acc.get('phone', '-')}</code>"
        if orders:
            text += "\n\n<b>История покупок:</b>\n" + "\n".join(f"• <code>{o}</code>" for o in orders[-10:])

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return

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
            parts = [p.strip() for p in ln.split(":", 2)]
            phone = parts[0] if len(parts) > 0 else ""
            p2 = parts[1] if len(parts) > 1 else ""
            p3 = parts[2] if len(parts) > 2 else ""
            if p2 and p3:
                secret = f"{p2}:{p3}"
            elif p2:
                secret = p2
            else:
                secret = ""
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
        parts = [p.strip() for p in raw_text.split("|", 3)]
        if len(parts) < 3 or not parts[0] or not parts[1]:
            await message.answer(
                "Формат: <code>ключ|Название|Цена_в_рублях|Описание</code>",
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
            return

        key = re.sub(r"[^a-zA-Z0-9_-]+", "_", parts[0]).strip("_").lower()
        title = parts[1]
        try:
            price_rub = float(parts[2].replace(",", "."))
        except ValueError:
            await message.answer("Цена должна быть числом.", reply_markup=admin_panel_keyboard())
            return

        desc = parts[3] if len(parts) > 3 and parts[3] else f"Аккаунты {title}."
        if not key:
            await message.answer("Некорректный ключ позиции.", reply_markup=admin_panel_keyboard())
            return

        CATALOG[key] = {
            "title": title,
            "price_rub": price_rub,
            "description": desc,
            "accounts": [],
        }
        await message.answer(
            f"✅ Позиция <b>{title}</b> (<code>{key}</code>) добавлена — <b>{price_rub:.2f} ₽</b>.",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return

    if mode == "waiting_stars":
        WAITING_INPUT.pop(user_id, None)
        if not raw_text.isdigit() or int(raw_text) <= 0:
            await message.answer(
                "Введи целое положительное число звёзд, например: <code>25</code>",
                parse_mode="HTML",
                reply_markup=back_to_main_keyboard(),
            )
            return

        stars_cnt = int(raw_text)
        rub_add = round(stars_cnt * RUB_PER_STAR, 2)
        USER_STARS_BALANCE[user_id] = USER_STARS_BALANCE.get(user_id, 0) + stars_cnt
        new_rub = get_rub_balance(user_id)

        await message.answer(
            f"⭐ Зачислено <b>{stars_cnt} звёзд</b> (+<b>{rub_add:.2f} ₽</b>).\n"
            f"Текущий баланс: <b>{new_rub:.2f} ₽</b>",
            parse_mode="HTML",
            reply_markup=back_to_main_keyboard(),
        )
        uname = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
        await notify_admin(
            message.bot,
            f"⭐ <b>Пополнение звёздами!</b>\n"
            f"Юзер: {uname} (<code>{user_id}</code>)\n"
            f"Сумма: <b>{stars_cnt} ⭐</b> (+<b>{rub_add:.2f} ₽</b>)\n"
            f"Новый баланс: <b>{new_rub:.2f} ₽</b>",
        )
        return

    if mode == "waiting_ton":
        WAITING_INPUT.pop(user_id, None)
        try:
            ton_amount = round(float(raw_text.replace(",", ".")), 4)
        except ValueError:
            await message.answer(
                "Введи корректную сумму в TON, например: <code>0.5</code>",
                parse_mode="HTML",
                reply_markup=back_to_main_keyboard(),
            )
            return

        if ton_amount <= 0:
            await message.answer(
                "Сумма в TON должна быть больше нуля.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        rub_add = round(ton_amount * RUB_PER_TON, 2)
        USER_TON_BALANCE[user_id] = round(USER_TON_BALANCE.get(user_id, 0.0) + ton_amount, 4)
        new_rub = get_rub_balance(user_id)

        await message.answer(
            f"💎 Зачислено <b>{ton_amount:.4f} TON</b> (+<b>{rub_add:.2f} ₽</b>).\n"
            f"Текущий баланс: <b>{new_rub:.2f} ₽</b>\n"
            f"Кошелёк: <code>{CRYPTO_WALLET}</code>",
            parse_mode="HTML",
            reply_markup=back_to_main_keyboard(),
        )
        uname = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
        await notify_admin(
            message.bot,
            f"💎 <b>Пополнение TON!</b>\n"
            f"Юзер: {uname} (<code>{user_id}</code>)\n"
            f"Сумма: <b>{ton_amount:.4f} TON</b> (+<b>{rub_add:.2f} ₽</b>)\n"
            f"Новый баланс: <b>{new_rub:.2f} ₽</b>",
        )
        return


def _json_cors(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=CORS_HEADERS)


async def api_options(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def api_state(request: web.Request) -> web.Response:
    raw_uid = request.query.get("user_id", "0")
    uid = int(raw_uid) if raw_uid.isdigit() else 0

    items = []
    for key, item in CATALOG.items():
        cnt = len(item.get("accounts", []))
        if cnt > 0:
            items.append({
                "key": key,
                "title": item["title"],
                "price_rub": float(item["price_rub"]),
                "description": item.get("description", ""),
                "count": cnt,
            })

    return _json_cors({
        "ok": True,
        "user_id": uid,
        "balance_rub": get_rub_balance(uid),
        "stars": USER_STARS_BALANCE.get(uid, 0),
        "ton": USER_TON_BALANCE.get(uid, 0.0),
        "rub_per_star": RUB_PER_STAR,
        "rub_per_ton": RUB_PER_TON,
        "crypto_wallet": CRYPTO_WALLET,
        "orders": PURCHASE_HISTORY.get(uid, []),
        "items": items,
    })


async def api_buy(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _json_cors({"ok": False, "error": "Некорректный JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    key = str(data.get("item_key") or "").strip()
    if not uid or not key:
        return _json_cors({"ok": False, "error": "Не указан user_id или товар"}, status=400)

    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        return _json_cors({"ok": False, "error": "Товара нет в наличии"}, status=400)

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        return _json_cors({"ok": False, "error": "Недостаточно средств на балансе"}, status=400)

    acc = item["accounts"].pop(0)
    if not deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        return _json_cors({"ok": False, "error": "Ошибка списания баланса"}, status=400)

    order_id = f"ORD-{secrets.token_hex(3).upper()}"
    phone = acc.get("phone", "")
    session_str, two_fa = _parse_account_secret(acc.get("secret", ""))

    LAST_ISSUED_ACCOUNT[uid] = {
        "order_id": order_id,
        "item_title": item["title"],
        "phone": phone,
        "session_string": session_str,
        "two_fa": two_fa,
    }
    PURCHASE_HISTORY.setdefault(uid, []).append(f"{order_id}: {item['title']} ({phone})")

    uname = str(data.get("username") or "").strip()
    uname_fmt = f"@{uname.lstrip('@')}" if uname else "без юзернейма"
    await notify_admin(
        bot,
        f"🛒 <b>Покупка (Mini App)!</b>\n"
        f"Юзер: {uname_fmt} (<code>{uid}</code>)\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Позиция: {item['title']} — <b>{price:.2f} ₽</b>\n"
        f"Номер: <code>{phone}</code>\n"
        f"Остаток: <b>{len(item['accounts'])} шт.</b>",
    )

    return _json_cors({
        "ok": True,
        "order_id": order_id,
        "phone": phone,
        "two_fa": two_fa,
        "balance_rub": get_rub_balance(uid),
        "orders": PURCHASE_HISTORY.get(uid, []),
    })


async def api_deposit(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _json_cors({"ok": False, "error": "Некорректный JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    method = str(data.get("method") or "").strip().lower()
    raw_amount = data.get("amount")
    if not uid or method not in ("stars", "ton"):
        return _json_cors({"ok": False, "error": "Некорректные параметры"}, status=400)

    try:
        amount = float(str(raw_amount).replace(",", "."))
    except Exception:
        return _json_cors({"ok": False, "error": "Некорректная сумма"}, status=400)

    if amount <= 0:
        return _json_cors({"ok": False, "error": "Сумма должна быть больше нуля"}, status=400)

    uname = str(data.get("username") or "").strip()
    uname_fmt = f"@{uname.lstrip('@')}" if uname else "без юзернейма"

    if method == "stars":
        stars_cnt = int(round(amount))
        if stars_cnt <= 0:
            return _json_cors({"ok": False, "error": "Укажи целое число звёзд"}, status=400)
        USER_STARS_BALANCE[uid] = USER_STARS_BALANCE.get(uid, 0) + stars_cnt
        rub_add = round(stars_cnt * RUB_PER_STAR, 2)
        new_rub = get_rub_balance(uid)
        await notify_admin(
            bot,
            f"⭐ <b>Пополнение звёздами (Mini App)!</b>\n"
            f"Юзер: {uname_fmt} (<code>{uid}</code>)\n"
            f"Сумма: <b>{stars_cnt} ⭐</b> (+<b>{rub_add:.2f} ₽</b>)\n"
            f"Новый баланс: <b>{new_rub:.2f} ₽</b>",
        )
    else:
        ton_cnt = round(amount, 4)
        USER_TON_BALANCE[uid] = round(USER_TON_BALANCE.get(uid, 0.0) + ton_cnt, 4)
        rub_add = round(ton_cnt * RUB_PER_TON, 2)
        new_rub = get_rub_balance(uid)
        await notify_admin(
            bot,
            f"💎 <b>Пополнение TON (Mini App)!</b>\n"
            f"Юзер: {uname_fmt} (<code>{user_id}</code>)\n"
            f"Сумма: <b>{ton_cnt:.4f} TON</b> (+<b>{rub_add:.2f} ₽</b>)\n"
            f"Новый баланс: <b>{new_rub:.2f} ₽</b>",
        )

    return _json_cors({
        "ok": True,
        "balance_rub": get_rub_balance(uid),
        "stars": USER_STARS_BALANCE.get(uid, 0),
        "ton": USER_TON_BALANCE.get(uid, 0.0),
    })


async def serve_miniapp(request: web.Request) -> web.StreamResponse:
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp_balanced-v2.html")
    if not os.path.exists(file_path):
        return web.Response(status=404, text="miniapp_balanced-v2.html not found")
    return web.FileResponse(file_path)


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_route("OPTIONS", "/api/{tail:.*}", api_options)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/deposit", api_deposit)
    app.router.add_get("/miniapp_balanced-v2.html", serve_miniapp)
    app.router.add_get("/", serve_miniapp)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def _run_all() -> None:
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run_all())