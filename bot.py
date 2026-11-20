import asyncio
import os
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


def catalog_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, item in CATALOG.items():
        count = len(item["stock"])
        label = f"{item['title']} — {item['price_ton']} TON ({count} шт.)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="Мои покупки", callback_data="history")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(item_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Я оплатил (проверить)", callback_data=f"check:{item_key}")],
            [InlineKeyboardButton(text="Отменить", callback_data="cancel")],
        ]
    )


dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Выбери позицию в каталоге.\n"
        "Оплата принимается в TON на кошелёк с автовыдачей после подтверждения.",
        reply_markup=catalog_keyboard(),
    )


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
            "Товар закончился. Напиши в поддержку.",
            reply_markup=catalog_keyboard(),
        )
        await callback.answer()
        return

    delivered_item = item["stock"].pop(0)
    PENDING_ORDERS.pop(user_id, None)
    PURCHASE_HISTORY.setdefault(user_id, []).append(f"{item['title']}: {delivered_item}")

    await callback.message.edit_text(
        f"Оплата подтверждена! Ваш товар:\n<code>{delivered_item}</code>",
        parse_mode="HTML",
        reply_markup=catalog_keyboard(),
    )
    await callback.answer("Товар выдан!")


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery):
    PENDING_ORDERS.pop(callback.from_user.id, None)
    await callback.message.edit_text("Заказ отменён.", reply_markup=catalog_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    orders = PURCHASE_HISTORY.get(callback.from_user.id, [])
    if not orders:
        text = "У вас пока нет покупок."
    else:
        text = "<b>Ваши покупки:</b>\n" + "\n".join(f"• <code>{o}</code>" for o in orders)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=catalog_keyboard())
    await callback.answer()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
