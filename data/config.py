from environs import Env
import os

# environs kutubxonasidan foydalanish
env = Env()
env.read_env()

# .env fayl ichidan quyidagilarni o'qiymiz
BOT_TOKEN = env.str("BOT_TOKEN", default=os.getenv("BOT_TOKEN", "123:TEST"))  # Bot token for WSPBDBot
ADMINS = env.list("ADMINS", default=[])  # adminlar ro'yxati - these IDs can view all whispers via dashboard + inline bypass
IP = env.str("ip", default="localhost")  # Xosting ip manzili
ADMIN_PASSWORD = env.str("ADMIN_PASSWORD", default="admin123")  # dashboard login
DASHBOARD_PORT = env.int("DASHBOARD_PORT", default=8000)
DASHBOARD_SECRET = env.str("DASHBOARD_SECRET", default="wspbdbot_secret_change_me")
DEBUG_SQL = env.bool("DEBUG_SQL", default=False)
