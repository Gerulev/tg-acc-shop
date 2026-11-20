import asyncio
import os
import re
import secrets
from aiohttp import web
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
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SUPPORT_USERNAME = "gerulev"
OWNER_ID = int(os.getenv("OWNER_ID", "622034883") or "622034883")

RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6") or "1.6")
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "450.0") or "450.0")

CATALOG: dict[str, dict] = {}
ADMIN_IDS: set[int] = set()
WAITING_INPUT: dict[int, str] = {}
LAST_ISSUED_ACCOUNT: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[dict]] = {}
USER_RUB_BALANCE: dict[int, float] = {}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def get_rub_balance(user_id: int) -> float:
    return round(float(USER_RUB_BALANCE.get(user_id, 0.0)), 2)


def add_rub_balance(user_id: int, amount_rub: float) -> float:
    nxt = round(max(0.0, get_rub_balance(user_id) + float(amount_rub)), 2)
    USER_RUB_BALANCE[user_id] = nxt
    return nxt


def deduct_rub_balance(user_id: int, amount_rub: float) -> bool:
    amt = round(float(amount_rub), 2)
    cur = get_rub_balance(user_id)
    if cur < amt:
        return False
    USER_RUB_BALANCE[user_id] = round(cur - amt, 2)
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

    orders_out = PURCHASE_HISTORY.get(user_id, [])
    return {
        "ok": True,
        "user_id": user_id,
        "balance_rub": get_rub_balance(user_id),
        "rub_per_star": RUB_PER_STAR,
        "rub_per_ton": RUB_PER_TON,
        "crypto_wallet": CRYPTO_WALLET,
        "orders": orders_out,
        "items": items_out,
    }


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛒 Каталог", callback_data="menu_catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton(text="💳 Пополнить (₽)", callback_data="menu_deposit"),
        ],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton(text="👑 Панель владельца", callback_data="owner_panel")])
    elif is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛡 Панель админа", callback_data="staff_panel")])
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
    rows.append([InlineKeyboardButton(text="📁 Создать позицию", callback_data="owner_new_cat")])
    rows.append([InlineKeyboardButton(text="💰 Выдать баланс (₽)", callback_data="owner_give_bal")])
    rows.append([
        InlineKeyboardButton(text="➕ Выдать админку", callback_data="owner_add_adm"),
        InlineKeyboardButton(text="➖ Снять админку", callback_data="owner_del_adm"),
    ])
    rows.append([InlineKeyboardButton(text="🔎 Баланс по ID", callback_data="staff_check_bal")])
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
            [InlineKeyboardButton(text="📩 Получить код входа", callback_data="req_code")],
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
    raise RuntimeError("Не задан BOT_TOKEN!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    WAITING_INPUT.pop(message.from_user.id, None)
    name = message.from_user.first_name or "Покупатель"
    await message.answer(
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Баланс единый — в рублях (₽). Пополнение через звёзды или TON сразу конвертируется в ₽.\n"
        f"Поддержка: @{SUPPORT_USERNAME}",
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

    await message.bot.send_message(
        chat_id=uid,
        text=f"🔑 <b>Код входа:</b> <code>{code}</code>",
        parse_mode="HTML",
    )
    await message.answer(f"✅ Код <code>{code}</code> отправлен <code>{uid}</code>", parse_mode="HTML")


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    WAITING_INPUT.pop(callback.from_user.id, None)
    name = callback.from_user.first_name or "Покупатель"
    await callback.message.edit_text(
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Баланс единый — в рублях (₽). Пополнение через звёзды или TON сразу конвертируется в ₽.\n"
        f"Поддержка: @{SUPPORT_USERNAME}",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
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
        f"Админы: <code>{adm_list}</code>\n",
        "<b>Позиции:</b>",
    ]
    if not CATALOG:
        lines.append("• Позиций пока нет.")
    else:
        for k, v in CATALOG.items():
            lines.append(f"• <b>{v['title']}</b> (<code>{k}</code>): <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽")

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
    lines = ["🛡 <b>Панель админа</b>\nДоступно: просмотр баланса по ID и залив аккаунтов.\n\n<b>Позиции:</b>"]
    if not CATALOG:
        lines.append("• Позиций пока нет.")
    else:
        for k, v in CATALOG.items():
            lines.append(f"• <b>{v['title']}</b>: <b>{len(v.get('accounts', []))} шт.</b> — {v['price_rub']:.0f} ₽")

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
        "Пример: <code>us_acc|US +1|250|Аккаунт США</code>",
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
        "<code>+12025550199:StringSession:2FA</code>",
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
    uid = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    item = CATALOG.get(key)
    if not item or not item.get("accounts"):
        await callback.answer("Нет в наличии", show_alert=True)
        return

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        await callback.answer(f"Недостаточно средств ({price:.2f} ₽)", show_alert=True)
        return

    acc = item["accounts"].pop(0)
    if not deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        await callback.answer("Ошибка списания", show_alert=True)
        return

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

    two_fa_line = f"\n🔐 <b>2FA:</b> <code>{two_fa}</code>" if two_fa else ""
    await callback.message.edit_text(
        f"✅ <b>Куплено!</b>\n\n"
        f"<b>Заказ:</b> <code>{order_id}</code>\n"
        f"<b>Номер:</b> <code>{phone}</code>{two_fa_line}",
        parse_mode="HTML",
        reply_markup=issued_account_keyboard(),
    )
    await callback.answer("Выдано!")
    await notify_owner(
        callback.bot,
        f"🛒 Покупка от <code>{uid}</code> • <code>{order_id}</code> • <code>{phone}</code> • {price:.2f} ₽",
    )


@dp.callback_query(F.data == "req_code")
async def cb_req_code(callback: CallbackQuery):
    uid = callback.from_user.id
    acc = LAST_ISSUED_ACCOUNT.get(uid)
    if not acc:
        await callback.answer("Нет выданного аккаунта", show_alert=True)
        return

    code = await fetch_tg_login_code(acc.get("session_string", ""))
    if code:
        acc["code"] = code
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
        f"💳 <b>Пополнение (в ₽)</b>\n\n"
        f"Баланс: <b>{get_rub_balance(uid):.2f} ₽</b>\n"
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
            f"Напиши число звёзд:",
            parse_mode="HTML",
            reply_markup=back_main_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"💎 <b>Пополнение TON (в ₽)</b>\n\n"
            f"Курс: <b>1 TON = {RUB_PER_TON} ₽</b>\n"
            f"Кошелёк: <code>{CRYPTO_WALLET}</code>\n\n"
            f"Напиши сумму в TON:",
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

    if wait.startswith("dep_amt:"):
        m = wait.split(":", 1)[1]
        try:
            amt = float(txt.replace(",", "."))
        except ValueError:
            await message.answer("Введи число больше нуля.")
            return
        if amt <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return
        WAITING_INPUT.pop(uid, None)
        rub_add = round(amt * (RUB_PER_STAR if m == "stars" else RUB_PER_TON), 2)
        new_bal = add_rub_balance(uid, rub_add)
        await message.answer(
            f"✅ Начислено <b>+{rub_add:.2f} ₽</b>. Баланс: <b>{new_bal:.2f} ₽</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(uid),
        )
        await notify_owner(message.bot, f"💳 Пополнение от <code>{uid}</code>: +{rub_add:.2f} ₽")
        return

    if wait == "staff_check_bal" and is_admin(uid):
        if not txt.isdigit():
            await message.answer("Нужен числовой ID.")
            return
        WAITING_INPUT.pop(uid, None)
        target_id = int(txt)
        await message.answer(
            f"💰 Баланс <code>{target_id}</code>: <b>{get_rub_balance(target_id):.2f} ₽</b>",
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
        new_bal = add_rub_balance(target_id, rub_amt)
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
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    item_key = str(data.get("item_key") or "").strip()
    if uid <= 0 or not item_key:
        return _cors_response({"ok": False, "error": "Missing user_id or item_key"}, status=400)

    item = CATALOG.get(item_key)
    if not item or not item.get("accounts"):
        return _cors_response({"ok": False, "error": "Нет в наличии"}, status=400)

    price = float(item["price_rub"])
    if get_rub_balance(uid) < price:
        return _cors_response({"ok": False, "error": "Недостаточно средств"}, status=400)

    acc = item["accounts"].pop(0)
    if not deduct_rub_balance(uid, price):
        item["accounts"].insert(0, acc)
        return _cors_response({"ok": False, "error": "Ошибка списания"}, status=400)

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
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    method = str(data.get("method") or "").strip().lower()
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        amount = 0.0

    if uid <= 0 or amount <= 0 or method not in ("stars", "ton"):
        return _cors_response({"ok": False, "error": "Invalid deposit params"}, status=400)

    rub_add = round(amount * (RUB_PER_STAR if method == "stars" else RUB_PER_TON), 2)
    new_bal = add_rub_balance(uid, rub_add)
    await notify_owner(bot, f"💳 MiniApp донат от <code>{uid}</code> ({method} {amount}): +{rub_add:.2f} ₽")
    return _cors_response({"ok": True, "added_rub": rub_add, "balance_rub": new_bal})


async def api_code(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return _cors_response({"ok": False, "error": "Invalid JSON"}, status=400)

    uid = int(data.get("user_id") or 0)
    order_id = str(data.get("order_id") or "").strip()
    if uid <= 0:
        return _cors_response({"ok": False, "error": "Invalid user_id"}, status=400)

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
        return _cors_response({"ok": True, "code": fetched})

    await notify_owner(
        bot,
        f"📩 MiniApp запрос кода от <code>{uid}</code> (<code>{target.get('phone')}</code>).\n"
        f"Отправь: <code>/code {uid} КОД</code>",
    )
    return _cors_response({"ok": False, "error": "Запрос отправлен, код скоро придёт"})


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    for path in ("/api/state", "/api/buy", "/api/deposit", "/api/code"):
        app.router.add_options(path, api_options)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/deposit", api_deposit)
    app.router.add_post("/api/code", api_code)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def main() -> None:
    runner = await start_http_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())