

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from data import config
# Use unified Group-manager DB (includes Messages + users/groups)
# Fallback to old sqlite if needed
try:
    from database import db as unified_db
    db = unified_db
    # Ensure whisper table exists
    try:
        db.create_table_messages()
    except: pass
except Exception as e:
    print(f"Unified DB load failed {e}, fallback to sqlite")
    from utils.db_api.sqlite import Database
    db = Database(path_to_db="data/main.db")

bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML)
storage = MemoryStorage()

dp = Dispatcher(bot, storage=storage)
