import asyncio
import base64
import os
import secrets
import time
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_WALLET = os.getenv(
    "CRYPTO_WALLET",
    "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux",
)
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
SUPPORT_USERNAME = "gerulev"

# Курсы для отображения баланса в рублях (₽) в профиле
RUB_PER_STAR = float(os.getenv("RUB_PER_STAR", "1.6"))
RUB_PER_TON = float(os.getenv("RUB_PER_TON", "450.0"))

CATALOG = {
    "acc_basic": {
        "title": "Аккаунт — Базовый",
        "price_ton": 0.5,
        "stock": ["acc1_data_example", "acc2_data_example"],
    },
    "acc_aged": {
        "title": "Аккаунт — С отлёжкой",
        "price_ton": 1.0,
        "stock": ["acc3_data_example"],
    },
}

STAR_PACKAGES = {
    "stars_10": {"stars": 10, "label": "10 ⭐"},
    "stars_25": {"stars": 25, "label": "25 ⭐"},
    "stars_50": {"stars": 50, "label": "50 ⭐"},
}

# user_id -> {"item_key": str, "comment": str, "amount_ton": float, "created_at": int}
PENDING_ORDERS: dict[int, dict] = {}
PURCHASE_HISTORY: dict[int, list[str]] = {}
USER_STARS_BALANCE: dict[int, int] = {}
USER_TON_BALANCE: dict[int, float] = {}
USED_TX_KEYS: set[str] = set()


def get_rub_balance(user_id: int) -> float:
    stars = USER_STARS_BALANCE.get(user_id, 0)
    ton = USER_TON_BALANCE.get(user_id, 0.0)
    return round(stars * RUB_PER_STAR + ton * RUB_PER_TON, 2)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
    )


def deposit_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for pkg_key, pkg in STAR_PACKAGES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⭐ Пополнить на {pkg['label']}",
                    callback_data=f"stars_pay:{pkg_key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, item in CATALOG.items():
        count = len(item["stock"])
        label = f"{item['title']} — {item['price_ton']} TON ({count} шт.)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            # В простых текстовых комментариях TON первые 4 байта — opcode (обычно 0)
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
        # Допускаем небольшую погрешность на округление в 0.001 TON
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
    first_name = message.from_user.first_name or "Покупатель"
    await message.answer(
        get_welcome_text(first_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    first_name = callback.from_user.first_name or "Покупатель"
    await callback.message.edit_text(
        get_welcome_text(first_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    text = "🛍 <b>Каталог товаров</b>\n\nВыбери интересующую позицию для покупки:"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=catalog_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    rub_balance = get_rub_balance(user_id)
    stars_balance = USER_STARS_BALANCE.get(user_id, 0)
    orders = PURCHASE_HISTORY.get(user_id, [])

    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"💰 <b>Баланс:</b> <b>{rub_balance:.2f} ₽</b> (⭐ {stars_balance})\n"
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
    user_id = callback.from_user.id
    rub_balance = get_rub_balance(user_id)
    stars_balance = USER_STARS_BALANCE.get(user_id, 0)
    text = (
        "💳 <b>Пополнение баланса через Telegram Stars</b>\n\n"
        f"Текущий баланс: <b>{rub_balance:.2f} ₽</b> (⭐ {stars_balance})\n"
        "Выбери пакет звёзд ниже — откроется окно оплаты в Telegram:"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=deposit_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("stars_pay:"))
async def cb_stars_pay(callback: CallbackQuery, bot: Bot):
    pkg_key = callback.data.split(":", 1)[1]
    pkg = STAR_PACKAGES.get(pkg_key)
    if not pkg:
        await callback.answer("Пакет не найден", show_alert=True)
        return

    stars_amount = pkg["stars"]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Пополнение баланса ({pkg['label']})",
        description=f"Зачисление {stars_amount} звёзд на ваш баланс в магазине.",
        payload=f"deposit_stars:{stars_amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=pkg["label"], amount=stars_amount)],
    )
    await callback.answer("Счёт на оплату звёздами отправлен!")


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
            f"⭐️ <b>Оплата прошла успешно!</b>\n\n"
            f"Начислено: <b>+{added_stars} ⭐</b>\n"
            f"Баланс: <b>{new_rub:.2f} ₽</b> (⭐ {new_stars})",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
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
    item_key = callback.data.split(":", 1)[1]
    item = CATALOG.get(item_key)

    if not item or len(item["stock"]) == 0:
        await callback.answer("Товара нет в наличии", show_alert=True)
        return

    user_id = callback.from_user.id
    comment = f"ORD{user_id}{secrets.token_hex(2).upper()}"
    amount_ton = float(item["price_ton"])

    PENDING_ORDERS[user_id] = {
        "item_key": item_key,
        "comment": comment,
        "amount_ton": amount_ton,
        "created_at": int(time.time()),
    }

    text = (
        f"<b>Заказ:</b> {item['title']}\n"
        f"<b>К оплате:</b> <code>{amount_ton}</code> TON\n"
        f"<b>TON-кошелёк:</b>\n<code>{CRYPTO_WALLET}</code>\n"
        f"<b>Обязательный комментарий (memo):</b>\n<code>{comment}</code>\n\n"
        "Переведи точную сумму с указанным комментарием и нажми кнопку проверки."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=payment_keyboard(item_key)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("check:"))
async def cb_check_payment(callback: CallbackQuery):
    item_key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    order = PENDING_ORDERS.get(user_id)

    if not order or order.get("item_key") != item_key:
        await callback.answer("Заказ не найден или уже закрыт", show_alert=True)
        return

    await callback.answer("Проверяем транзакцию в сети TON...")

    paid = await verify_ton_payment(
        expected_ton=order["amount_ton"],
        expected_comment=order["comment"],
        min_utime=order["created_at"],
    )

    if not paid:
        await callback.answer(
            "Перевод пока не найден. Проверь сумму и комментарий и попробуй через 10–15 сек.",
            show_alert=True,
        )
        return

    item = CATALOG.get(item_key)
    if not item or len(item["stock"]) == 0:
        PENDING_ORDERS.pop(user_id, None)
        await callback.message.edit_text(
            f"Оплата получена, но товар закончился. Напиши @{SUPPORT_USERNAME}.",
            reply_markup=catalog_keyboard(),
        )
        return

    delivered_item = item["stock"].pop(0)
    PENDING_ORDERS.pop(user_id, None)
    PURCHASE_HISTORY.setdefault(user_id, []).append(f"{item['title']}: {delivered_item}")

    await callback.message.edit_text(
        f"🎉 <b>Оплата в TON подтверждена!</b> Ваш товар:\n<code>{delivered_item}</code>",
        parse_mode="HTML",
        reply_markup=catalog_keyboard(),
    )


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery):
    PENDING_ORDERS.pop(callback.from_user.id, None)
    await callback.message.edit_text("Заказ отменён.", reply_markup=catalog_keyboard())
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
