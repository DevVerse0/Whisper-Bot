import os
import threading
from aiogram import executor

from loader import dp, db
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands


def _start_dashboard():
    # Only start if not disabled via env
    if os.getenv("DISABLE_DASHBOARD") == "1":
        return
    try:
        import uvicorn
        # Render sets PORT, prefer it over DASHBOARD_PORT
        port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8000")))
        print(f"🌐 Starting WSPBDBot Dashboard on http://0.0.0.0:{port}/admin")
        uvicorn.run("dashboard:app", host="0.0.0.0", port=port, log_level="info")
    except Exception as e:
        print(f"Dashboard failed to start: {e}")


async def on_startup(dispatcher):
    # Birlamchi komandalar (/star va /help)
    await set_default_commands(dispatcher)

    # Ma'lumotlar bazasini yaratamiz:
    try:
        db.create_table_messages()
    except Exception as err:
        pass

    # Bot ishga tushgani haqida adminga xabar berish
    await on_startup_notify(dispatcher)


if __name__ == '__main__':
    # Fix for Render + uvloop + Python 3.11: aiogram 2.14 executor needs event loop in MainThread
    import asyncio
    try:
        # uvloop replaces policy, ensure loop exists
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except Exception as _e:
        print(f"Loop init: {_e}")

    # Start dashboard in background thread so `python app.py` launches both
    try:
        t = threading.Thread(target=_start_dashboard, daemon=True)
        t.start()
    except Exception as e:
        print(f"Dashboard thread error: {e}")
    executor.start_polling(dp, on_startup=on_startup)
