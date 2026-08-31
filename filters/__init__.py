from aiogram import Dispatcher

from loader import dp
from .admin import IsAdmin


if __name__ == "filters":
    pass

# Bind custom filter
try:
    dp.filters_factory.bind(IsAdmin)
except Exception:
    pass
