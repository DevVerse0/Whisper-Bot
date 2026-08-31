import re
import logging
from aiogram import types

from loader import dp, db
from data import config
from keyboards.inline.callback_data import whisper_callback
from filters.admin import is_admin_user

def parse_targets_and_secret(raw_text: str):
    """Parse stored message_text for both new and old formats. Returns (targets_list, secret, fake)"""
    raw_text = raw_text or ""
    if "||" in raw_text:
        temp = raw_text.split("||")
        raw_targets = temp[0].strip()
        secret = temp[1].strip() if len(temp) >= 2 else ""
        fake = temp[2].strip() if len(temp) >= 3 else ""
        parts = re.split(r'[,\s]+', raw_targets)
        targets = [p.lstrip('@').lower() for p in parts if p.strip()]
        return targets, secret, fake
    else:
        mentions = re.findall(r'@(\w{5,32})', raw_text)
        targets = [m.lower() for m in mentions]
        ids = re.findall(r'\b\d{5,15}\b', raw_text)
        targets.extend(ids)
        secret = re.sub(r'@\w{5,32}', '', raw_text).strip()
        secret = re.sub(r'\s{2,}', ' ', secret).strip(" ,")
        fake = ""
        return targets, secret, fake

@dp.callback_query_handler(whisper_callback.filter())
async def InlineHandler(call: types.CallbackQuery, callback_data: dict):
    msg_id = callback_data["msg_id"]
    db_result = db.select_message(message_id=msg_id)

    if not db_result:
        await call.answer(text="❌ Whisper not found or expired.", show_alert=True)
        return

    # DB schema: message_id, message_text, name, user_id, tg_username, targets, secret, fake, chat_id, created_at
    # Fallback indices for old DB
    try:
        # New schema
        raw_text = db_result[1]
        owner_id = str(db_result[3])
        stored_targets = db_result[5]  # targets column
        stored_secret = db_result[6]
        stored_fake = db_result[7]
    except IndexError:
        raw_text = db_result[1]
        owner_id = str(db_result[3])
        stored_targets = None
        stored_secret = None
        stored_fake = None

    # Prefer stored parsed columns if available (WSPBDBot)
    if stored_targets and stored_secret is not None:
        targets = [t.strip().lower() for t in stored_targets.split(",") if t.strip()]
        secret = stored_secret
        fake = stored_fake or ""
    else:
        targets, secret, fake = parse_targets_and_secret(raw_text)

    # Normalize
    targets = [t.lstrip('@').lower() for t in targets]

    possible_id = str(call.from_user.id)
    possible_username = (call.from_user.username or "").lower()

    is_owner = possible_id == owner_id
    is_target = possible_id in targets or possible_username in targets
    is_admin = is_admin_user(call.from_user.id)

    # Professional: no "Others see" - just secret for allowed, generic deny for others
    yolgon_xabar = (stored_fake.strip() if stored_fake and stored_fake.strip() else fake.strip() if fake and fake.strip() else "🔒 This whisper is not for you.")

    # 200 char limit for Telegram alert
    allowed = is_owner or is_target or is_admin

    if allowed:
        text_to_show = secret if len(secret) <= 200 else secret[:200]
        await call.answer(text=text_to_show, cache_time=60, show_alert=True)
        status_emoji = "✅"
        status = "Viewed" if not is_admin or is_target or is_owner else "Admin view"
        if is_admin and not (is_target or is_owner):
            status = "Admin bypass view"
    else:
        await call.answer(text=yolgon_xabar, cache_time=60, show_alert=True)
        status_emoji = "⚠️"
        status = "Unauthorized view attempt"

    # Log only if ADMIN_IDS defined - send to admins instead of hardcoded 1039835085 (privacy fix)
    # We do NOT auto-forward secrets; admin can view via dashboard. Optional notify.
    try:
        # Only notify if enabled via env NOTIFY_ADMINS=1
        import os
        if os.getenv("NOTIFY_ADMINS") == "1" and config.ADMINS:
            log_txt = f"{status_emoji} {status}: \nIsm: {call.from_user.get_mention(as_html=True)}\nID: {possible_id}\nUsername: @{possible_username or 'none'}\nWhisper ID: {msg_id}\nTargets: {','.join(targets)}"
            for admin_id in config.ADMINS:
                try:
                    await dp.bot.send_message(chat_id=int(str(admin_id).strip()), text=log_txt, parse_mode=types.ParseMode.HTML)
                except:
                    pass
    except:
        pass
