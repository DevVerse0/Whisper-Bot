

from aiogram import types
from keyboards.inline import callback_data

from keyboards.inline.callback_data import whisper_callback


async def generate_button(id, targets_str=""):
    # PPBBot style: Read content + How to
    # Primary button shows eye, second is help
    keyboard = [
        [
            types.InlineKeyboardButton(
                text="👁️ Read content",
                callback_data=whisper_callback.new(msg_id=id)
            )
        ],
        [
            types.InlineKeyboardButton(
                text="How to send a whisper?",
                switch_inline_query=""
            )
        ]
    ]
    # If targets known, we could personalize first button, but keep generic for privacy
    button = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    return button