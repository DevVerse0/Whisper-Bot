from aiogram import types
from aiogram.dispatcher.filters import BoundFilter

from data import config

class IsAdmin(BoundFilter):
    key = "is_admin"

    def __init__(self, is_admin: bool = None):
        self.is_admin = is_admin

    async def check(self, message: types.Message):
        if self.is_admin is None:
            return False
        is_admin = str(message.from_user.id) in [str(a).strip() for a in config.ADMINS if str(a).strip()]
        return is_admin if self.is_admin else not is_admin

def is_admin_user(user_id: int) -> bool:
    return str(user_id) in [str(a).strip() for a in config.ADMINS if str(a).strip()]
