import asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_WALLET = os.getenv("CRYPTO_WALLET", "UQBKmNK9ynwmMR_ORkA7Ii2TunYn8RpWsVDEZHOTOZ89ajux")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@DarkStors_admin")

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

PENDING_ORDERS: dict[int, str] = {}
PURCHASE_HISTORY: dict[int, list[str]] = {}
USER_BALANCES: dict[int, float] = {}


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
            [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        ]
    )


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
            [InlineKeyboardButton(text="✅ Я оплатил (проверить)", callback_data=f"check:{item_key}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ]
    )


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]]
    )


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
        f"🆘 <b>Поддержка:</b> {SUPPORT_USERNAME}\n"
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
    balance = USER_BALANCES.get(user_id, 0.0)
    orders = PURCHASE_HISTORY.get(user_id, [])

    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"💰 <b>Баланс:</b> {balance} TON\n"
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
    text = (
        "💳 <b>Пополнение баланса</b>\n\n"
        f"Для прямого пополнения переведите TON на кошелёк:\n<code>{CRYPTO_WALLET}</code>\n\n"
        "После перевода обратитесь в поддержку для зачисления баланса."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_to_main_keyboard()
    )
    await callback.answer()


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
        "Напишите поддержке, чтобы получить ссылку на канал."
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
    PENDING_ORDERS[user_id] = item_key

    text = (
        f"<b>Заказ:</b> {item['title']}\n"
        f"<b>К оплате:</b> {item['price_ton']} TON\n"
        f"<b>TON-кошелёк:</b>\n<code>{CRYPTO_WALLET}</code>\n\n"
        "После перевода нажми кнопку проверки оплаты."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=payment_keyboard(item_key)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("check:"))
async def cb_check_payment(callback: CallbackQuery):
    item_key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if PENDING_ORDERS.get(user_id) != item_key:
        await callback.answer("Заказ не найден или уже закрыт", show_alert=True)
        return

    item = CATALOG.get(item_key)
    if not item or len(item["stock"]) == 0:
        PENDING_ORDERS.pop(user_id, None)
        await callback.message.edit_text(
            "Товар закончился. Напишите в поддержку.",
            reply_markup=catalog_keyboard(),
        )
        await callback.answer()
        return

    delivered_item = item["stock"].pop(0)
    PENDING_ORDERS.pop(user_id, None)
    PURCHASE_HISTORY.setdefault(user_id, []).append(f"{item['title']}: {delivered_item}")

    await callback.message.edit_text(
        f"🎉 <b>Оплата подтверждена!</b> Ваш товар:\n<code>{delivered_item}</code>",
        parse_mode="HTML",
        reply_markup=catalog_keyboard(),
    )
    await callback.answer("Товар успешно выдан!")


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
