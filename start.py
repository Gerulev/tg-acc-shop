import asyncio
import os
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message
import bot as shop

BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.jpg")
_banner_file_id = None
HIDDEN_PAGES = {"roulette", "leaders", "help"}

_orig_app_btn = shop.app_btn


def _app_btn(text, page, *args, **kwargs):
    if page in HIDDEN_PAGES:
        return None
    return _orig_app_btn(text, page, *args, **kwargs)


shop.app_btn = _app_btn


def welcome_caption(user=None) -> str:
    return (
        f"<b>Привет, {shop._format_user_label(user)} — это DarkHub</b>"
        "Магазин аккаунтов Telegram: быстро, надёжно, по честной цене.

"
        "<b>Что у нас есть:</b>
"
        "<blockquote>• Номера разных стран и дат регистрации
"
        "• Без СБ и со СБ — выбираешь сам
"
        "• Номер, пароль и код входа сразу после оплаты</blockquote>

"
        "Перед покупкой загляни в FAQ — там всё про вход и гарантию.

"
        f"📦 <b>Продано аккаунтов:</b> {shop.TOTAL_SOLD_COUNT}"
    )


async def send_welcome(msg: Message, user) -> None:
    global _banner_file_id
    kb = shop.main_menu_keyboard(user.id)
    photo = _banner_file_id or (FSInputFile(BANNER_PATH) if os.path.exists(BANNER_PATH) else None)
    if photo is not None:
        try:
            sent = await msg.answer_photo(photo, caption=welcome_caption(user), parse_mode="HTML", reply_markup=kb)
            if sent.photo:
                _banner_file_id = sent.photo[-1].file_id
            return
        except Exception as e:
            print(f"welcome banner error: {e}")
    await msg.answer(welcome_caption(user), parse_mode="HTML", reply_markup=kb)


async def new_cmd_start(message: Message, command: CommandObject):
    shop.WAITING_INPUT.pop(message.from_user.id, None)
    arg = (command.args or "").strip() if command else ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    await shop.register_user(message.from_user.id, shop.who(message.from_user), ref)
    if not await shop.ensure_sub_message(message):
        return
    await send_welcome(message, message.from_user)


async def new_back_main(callback: CallbackQuery):
    if not await shop.ensure_sub_callback(callback):
        return
    shop.WAITING_INPUT.pop(callback.from_user.id, None)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_welcome(callback.message, callback.from_user)
    await callback.answer()


def _swap(observer, old, new) -> bool:
    for h in observer.handlers:
        if h.callback is old:
            h.callback = new
            return True
    return False


print("start.py: /start подменён" if _swap(shop.dp.message, shop.cmd_start, new_cmd_start) else "start.py: ВНИМАНИЕ /start не найден")
print("start.py: «В меню» подменено" if _swap(shop.dp.callback_query, shop.cb_back_main, new_back_main) else "start.py: ВНИМАНИЕ «В меню» не найдено")

_orig_edit_text = Message.edit_text


async def _smart_edit_text(self, text, **kwargs):
    if getattr(self, "photo", None):
        try:
            await self.delete()
        except Exception:
            pass
        kwargs.pop("inline_message_id", None)
        return await self.answer(text, **kwargs)
    return await _orig_edit_text(self, text, **kwargs)


Message.edit_text = _smart_edit_text

if __name__ == "__main__":
    asyncio.run(shop.main())
