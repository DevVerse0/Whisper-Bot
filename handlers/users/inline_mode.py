import re
import uuid

from aiogram import types

from loader import dp, db
from keyboards.inline.WhisperKeyboard import generate_button

# Regex for PPBBot style: @username or numeric ID
MENTION_RE = re.compile(r'@(\w{5,32})')
ID_RE = re.compile(r'\b(\d{5,15})\b')

def parse_whisper_query(text: str):
    """
    Parse query for WSPBDBot - supports both styles:
    1. PPBBot style: "Hello Kemon acho @delete_ee" -> targets=[delete_ee], secret="Hello Kemon acho"
    2. Old style: "user1, user2 || secret || fake" -> targets from part0, secret part1, fake part2
    Returns (targets_str, secret, fake, is_valid, error_hint)
    """
    text = text.strip()
    if not text:
        return None, None, None, False, "Empty"

    # Old style with ||
    if "||" in text:
        lst = text.split("||")
        # lst[0]=targets, lst[1]=secret, lst[2]=fake (optional)
        targets_raw = lst[0].strip()
        secret = lst[1].strip() if len(lst) >= 2 else ""
        fake = lst[2].strip() if len(lst) >= 3 else ""
        # Normalize targets: split by comma/space, strip @
        raw_parts = re.split(r'[,\s]+', targets_raw)
        targets = []
        for p in raw_parts:
            p = p.strip().lstrip('@').lower()
            if p:
                targets.append(p)
        targets_str = ",".join(targets)
        if not targets or not secret:
            return targets_str, secret, fake, False, "Need targets and secret"
        if len(secret) > 200 or len(text) > 255:
            return targets_str, secret, fake, False, "Too long"
        return targets_str, secret, fake, True, None

    # PPBBot style: extract mentions/IDs, rest is secret
    mentions = MENTION_RE.findall(text)
    ids = []  # numeric IDs without @ - only if explicitly mentioned? we keep mentions primarily
    # For IDs like "123456789" without @ we treat as possible target if text has them
    # But to avoid false positives from normal numbers, only treat IDs that look like telegram IDs? keep simple

    targets = [m.lower() for m in mentions]
    # Remove mentions from text to get secret
    secret = MENTION_RE.sub("", text).strip()
    # Clean extra spaces / commas
    secret = re.sub(r'\s+,', ',', secret)
    secret = re.sub(r'\s{2,}', ' ', secret).strip(" ,")

    if not targets:
        return None, secret, None, False, "No @username found"
    if not secret:
        return ",".join(targets), secret, None, False, "Empty message"
    if len(secret) > 200 or len(text) > 255:
        return ",".join(targets), secret, None, False, "Too long"

    targets_str = ",".join(targets)
    return targets_str, secret, None, True, None

@dp.inline_handler()
async def empty_query(query: types.InlineQuery):
    text = query.query

    targets_str, secret, fake, is_valid, hint = parse_whisper_query(text)
    txt_len = len(text)

    if is_valid:
        msg_id = str(uuid.uuid4())
        # Store normalized + raw text
        full_text = text  # keep original for InlineHandler fallback
        # For dashboard we store parsed fields
        try:
            db.add_message(
                message_id=msg_id,
                message_text=full_text,
                name=query.from_user.full_name,
                user_id=query.from_user.id,
                tg_username=query.from_user.username,
                targets=targets_str,
                secret=secret,
                fake=fake or "",
                chat_id=str(query.from_user.id)
            )
        except Exception as e:
            # fallback without new columns (old DB)
            try:
                db.add_message(
                    message_id=msg_id,
                    message_text=full_text,
                    name=query.from_user.full_name,
                    user_id=query.from_user.id,
                    tg_username=query.from_user.username
                )
            except:
                pass

        button = await generate_button(msg_id, targets_str)

        # Professional title like PPBBot
        target_display = targets_str.split(",")[0] if targets_str else "user"
        title = f"Whisper to @{target_display}"
        desc = f"Only @{target_display} and you can read it • Tap to send ({len(secret)}/200)"

        result = types.InlineQueryResultArticle(
            id=msg_id,
            title=title,
            description=desc,
            input_message_content=types.InputTextMessageContent(
                message_text=f"🔒 Whisper for @{target_display} — Tap 'Read content' to view",
            ),
            reply_markup=button,
        )
        await query.answer(results=[result], cache_time=0, is_personal=True)
    else:
        # Professional hints - English only, minimal
        as_len = len(secret) if secret else 0
        if not text.strip():
            title = "Send a whisper"
            desc = "Example: @WSPBDBot Hello there @username"
        elif hint and "No @username" in hint:
            title = "Add a recipient"
            desc = "Type: your message @username  •  e.g. Hello there @delete_ee"
        elif hint and "Empty message" in hint:
            title = "Type a message"
            desc = f"Add text before @username • {as_len}/200"
        elif hint and "Too long" in hint:
            title = "Message too long"
            desc = f"{as_len}/200 • Keep it shorter"
        else:
            title = "Invalid format"
            desc = "Type: your message @username"
        result = types.InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=title,
            description=desc,
            input_message_content=types.InputTextMessageContent(
                message_text="Tap to learn how to send a whisper",
            )
        )
        await query.answer(results=[result], cache_time=1, is_personal=True)
