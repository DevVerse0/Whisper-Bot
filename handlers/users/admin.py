from aiogram import types
from aiogram.dispatcher.filters.builtin import Command
from loader import dp, db
from filters.admin import IsAdmin

@dp.message_handler(Command("admin"), IsAdmin(is_admin=True))
async def admin_panel(message: types.Message):
    total = (db.count_whispers() or [0])[0] if db.count_whispers() else 0
    text = (
        f"👑 <b>WSPBDBot Admin Panel</b>\n\n"
        f"Total whispers: <b>{total}</b>\n"
        f"Your ID: <code>{message.from_user.id}</code>\n\n"
        f"<b>Commands:</b>\n"
        f"/whispers - Last 10 whispers\n"
        f"/whisper <code>msg_id</code> - View one\n"
        f"/stats - Stats\n"
        f"/dashboard - Web dashboard link\n\n"
        f"Web Dashboard: <code>http://localhost:8000/admin</code>\n"
        f"Password: <code>ADMIN_PASSWORD</code> from .env"
    )
    await message.answer(text)

@dp.message_handler(Command("whispers"), IsAdmin(is_admin=True))
async def list_whispers(message: types.Message):
    rows = db.get_whispers(limit=10, offset=0)
    if not rows:
        await message.answer("No whispers yet.")
        return
    for r in rows:
        try:
            mid, mtext, name, uid, uname, targets, secret, fake, chat_id, created = r
        except:
            mid = r[0]; mtext = r[1]; name = r[2] if len(r)>2 else ""; uid = r[3] if len(r)>3 else ""; uname = r[4] if len(r)>4 else ""; targets = r[5] if len(r)>5 else ""; secret = r[6] if len(r)>6 else ""; fake = r[7] if len(r)>7 else ""; created = r[9] if len(r)>9 else ""
        txt = (
            f"🔹 <code>{mid[:8]}</code> | {created}\n"
            f"From: {name} (@{uname or 'none'} {uid}) → To: @{targets}\n"
            f"Secret: <code>{(secret or mtext)[:200]}</code>"
        )
        await message.answer(txt)

@dp.message_handler(Command("whisper"), IsAdmin(is_admin=True))
async def view_whisper(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Usage: /whisper <msg_id>")
        return
    row = db.select_message(message_id=args)
    if not row:
        await message.answer("Not found.")
        return
    try:
        mid, mtext, name, uid, uname, targets, secret, fake, chat_id, created = row
    except:
        mid, mtext = row[0], row[1]
        name, uid, uname, targets, secret, fake, created = "", "", "", "", "", "", ""
    txt = (
        f"🔍 <b>Whisper {mid}</b>\n"
        f"Time: {created}\n"
        f"From: {name} (@{uname} {uid})\n"
        f"To: {targets}\n\n"
        f"<b>Secret:</b>\n{secret or mtext}\n\n"
        f"<b>Fake:</b> {fake}\n"
        f"<b>Raw:</b> <code>{mtext}</code>"
    )
    await message.answer(txt)

@dp.message_handler(Command("stats"), IsAdmin(is_admin=True))
async def stats(message: types.Message):
    total = (db.count_whispers() or [0])[0]
    await message.answer(f"📊 <b>WSPBDBot Stats</b>\nTotal whispers: <b>{total}</b>")

@dp.message_handler(Command("dashboard"), IsAdmin(is_admin=True))
async def dashboard_link(message: types.Message):
    import os
    port = os.getenv("DASHBOARD_PORT", "8000")
    await message.answer(f"🌐 Dashboard: http://localhost:{port}/admin\nPassword: ADMIN_PASSWORD from .env")

@dp.message_handler(Command("whispers"), IsAdmin(is_admin=False))
@dp.message_handler(Command("whisper"), IsAdmin(is_admin=False))
@dp.message_handler(Command("admin"), IsAdmin(is_admin=False))
@dp.message_handler(Command("stats"), IsAdmin(is_admin=False))
@dp.message_handler(Command("dashboard"), IsAdmin(is_admin=False))
async def not_admin(message: types.Message):
    await message.answer("❌ You are not admin.")
