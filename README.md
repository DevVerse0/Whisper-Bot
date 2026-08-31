# WSPBDBot - Whisper Bot (PPBBot Style) 💌

**WSPBDBot** - Telegram inline whisper bot like @PPBBot. Send secret messages that only target can read.

Based on sobirjonovme/WhisperBot but upgraded to PPBBot UX + Admin Dashboard.

## Features
- **PPBBot style:** `@WSPBDBot Hello Kemon acho @delete_ee` - no `||` needed, auto detects @mentions
- **Old style still works:** `username || secret || fake` (backward compat)
- **Card UI:** `🔒 Whisper for ...` + `👁️ Read content` + `How to send a whisper?` (Image2 style)
- **Admin Dashboard:** Web panel at `http://localhost:8000/admin` - see who → whom, search, paginate, view/delete. Only `ADMINS` from .env can bypass whisper privacy + login via ADMIN_PASSWORD. Also `/admin`, `/whispers`, `/whisper <id>`, `/stats` commands in Telegram for admins.
- **SQLite** (`data/main.db`) with migration from old schema, no Postgres needed.

## Quick Start

1. **Clone & env:**
```bash
git clone <your repo>
cd WhisperBot
cp .env.dist .env
# Edit .env: BOT_TOKEN, ADMINS=your_id, ADMIN_PASSWORD, DASHBOARD_PORT
```

2. **Install:**
```bash
pip install -r requirements.txt
# or pip install aiogram==2.14.3 environs==8.0.0 fastapi uvicorn jinja2 itsdangerous python-multipart
```

3. **Run Bot:**
```bash
python app.py
# Enable inline mode in @BotFather: /mybots -> WSPBDBot -> Bot Settings -> Inline Mode ON + Inline Feedback 100%
```

4. **Run Dashboard (separate terminal):**
```bash
python dashboard.py
# or uvicorn dashboard:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000/admin  password = ADMIN_PASSWORD
```

## How to Use (PPBBot style)
In any chat type:
```
@WSPBDBot Hello Kemon acho @delete_ee
```
Bot shows `Whisper for delete_ee (@delete_ee)` preview. Tap to send. Group sees `🔒 Whisper for delete_ee. Only they can read` + `Read content` button. Target / sender / admin can see secret via alert; others see decoy ("Haaa" or fake).

Old style: `@WSPBDBot username1, username2 || secret message || fake message`

## Admin
- Set `ADMINS=123,456` in .env (comma separated Telegram IDs)
- Web: `ADMIN_PASSWORD` + `/admin/login`
- Telegram: `/admin` `/whispers` `/whisper <msg_id>` `/stats` (admin only)
- Disable leaky log: `NOTIFY_ADMINS=0` (default). Old hardcoded 1039835085 removed.
- Privacy: Add disclaimer in /start - "Admin can audit whispers"

## Deployment
- `bot.conf` supervisor: `python3 app.py` + dashboard as second program `uvicorn dashboard:app`
- Keep `data/main.db` backed up, `.env` not committed.
