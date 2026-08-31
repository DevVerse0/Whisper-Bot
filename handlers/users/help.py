from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    text = (
        "<b>WSPBDBot Help</b>\n\n"
        "Send a private whisper:\n"
        "<code>@WSPBDBot your message @username</code>\n\n"
        "Example:\n"
        "<code>@WSPBDBot Hey there @delete_ee</code>\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Show this help"
    )
    await message.answer(text)
