from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandStart

from loader import dp

# Minimal professional welcome - no admin details for regular users


@dp.message_handler(CommandStart())
async def bot_start(message: types.Message):
    text = (
        f"Hello, {message.from_user.full_name}! 👋\n\n"
        f"I'm <b>WSPBDBot</b> - Send private whispers in any chat.\n\n"
        f"<b>How to use:</b>\n"
        f"Type <code>@WSPBDBot your message @username</code>\n\n"
        f"<b>Example:</b>\n"
        f"<code>@WSPBDBot You're amazing @delete_ee</code>\n\n"
        f"Only you and the recipient can read it."
    )
    await message.answer(text)
