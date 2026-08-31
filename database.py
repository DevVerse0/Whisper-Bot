import sqlite3
import os
import sys
import json
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env so DATABASE_URL is picked up even when running the bot standalone
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Optional PostgreSQL support (used when DATABASE_URL is set)
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    _PG_AVAILABLE = True
except Exception:
    psycopg2 = None
    RealDictCursor = None
    _PG_AVAILABLE = False

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Fix Windows encoding for emoji print statements
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PostgreSQL adapter â€” translates SQLite-flavoured SQL to Postgres
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _adapt_sqlite_to_pg(sql):
    """Convert a SQLite-flavoured statement to PostgreSQL.

    Handles: `?` placeholders -> %s, INSERT OR IGNORE -> ON CONFLICT DO NOTHING,
    INSERT OR REPLACE -> explicit ON CONFLICT upserts, datetime('now') -> NOW().
    """
    # INSERT OR REPLACE -> upsert (only two fixed statements use it)
    if "INSERT OR REPLACE INTO pending_captchas" in sql:
        return (
            "INSERT INTO pending_captchas (chat_id, user_id, captcha_type, correct_answer, join_time) "
            "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "captcha_type=excluded.captcha_type, correct_answer=excluded.correct_answer, "
            "join_time=CURRENT_TIMESTAMP"
        )
    if "INSERT OR REPLACE INTO global_bans" in sql:
        return (
            "INSERT INTO global_bans (user_id, reason, banned_by) VALUES (%s, %s, %s) "
            "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, banned_by=excluded.banned_by"
        )

    needs_ignore = "INSERT OR IGNORE INTO" in sql
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    sql = sql.replace("datetime('now')", "NOW()")
    sql = sql.replace("?", "%s")
    if needs_ignore:
        sql = sql.rstrip()
        if sql.endswith(";"):
            sql = sql[:-1]
        sql += " ON CONFLICT DO NOTHING"
    return sql


class _PGCursor:
    """Wraps a psycopg2 RealDictCursor so existing code (row['col'], rowcount,
    lastrowid, fetchone/fetchall) works unchanged."""

    def __init__(self, raw):
        self.raw = raw
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, sql, params=None):
        sql = _adapt_sqlite_to_pg(sql)
        if params is None:
            self.raw.execute(sql)
        else:
            self.raw.execute(sql, params)
        self.rowcount = self.raw.rowcount
        try:
            self.lastrowid = self.raw.lastrowid
        except Exception:
            self.lastrowid = None
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()

    def close(self):
        return self.raw.close()

    def __iter__(self):
        return iter(self.raw)


class _PGConnection:
    """Presents a psycopg2 connection with the same surface used by the code
    (cursor(), execute(), commit(), rollback(), close())."""

    def __init__(self, raw):
        self.raw = raw

    def cursor(self):
        return _PGCursor(self.raw.cursor(cursor_factory=RealDictCursor))

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        return self.raw.commit()

    def rollback(self):
        return self.raw.rollback()

    def close(self):
        return self.raw.close()

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# IN-MEMORY CACHE â€” eliminates redundant DB roundtrips
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class _Cache:
    """Thread-safe TTL cache for hot data (groups, config)."""
    def __init__(self, ttl=30):
        self._store = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry and entry[1] > _time.time():
                return entry[0]
            self._store.pop(key, None)
            return None

    def set(self, key, value, ttl=None):
        with self._lock:
            self._store[key] = (value, _time.time() + (ttl or self._ttl))

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

_group_cache  = _Cache(ttl=15)
_config_cache = _Cache(ttl=60)

class Database:
    def __init__(self, db_path="manager.db"):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = None
        if DATABASE_URL and DATABASE_URL.startswith("postgres"):
            if not _PG_AVAILABLE:
                print("âŒ DATABASE_URL is set but psycopg2 is not installed. Install psycopg2-binary.")
                self.backend = "sqlite"
            else:
                self.backend = "pg"
        else:
            self.backend = "sqlite"
        if self._connect():
            self._migrate()

    def _connect(self):
        try:
            if self.conn:
                try:
                    self.conn.close()
                except:
                    pass

            if self.backend == "pg":
                raw = psycopg2.connect(DATABASE_URL, connect_timeout=15)
                raw.autocommit = True
                self.conn = _PGConnection(raw)
                print("✅ PostgreSQL database connected successfully.")
                return True

            # Enable WAL mode for better concurrency
            self.conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA cache_size=-10000")  # 10MB cache
            self.conn.row_factory = sqlite3.Row
            print("âœ… SQLite database connected successfully.")
            return True
        except Exception as e:
            print(f"âŒ Database connection failed: {e}")
            self.conn = None
            return False

    def _check_conn(self):
        with self.lock:
            if not self.conn:
                return self._connect()

            # Verify connection is actually alive
            try:
                cur = self.conn.execute("SELECT 1")
                if hasattr(cur, "fetchone"):
                    cur.fetchone()
                return True
            except Exception:
                return self._connect()

    def _migrate(self):
        if not self._check_conn():
            return
        with self.lock:
            try:
                if self.backend == "pg":
                    self._migrate_pg()
                    return

                c = self.conn.cursor()

                # â”€â”€ CONFIG TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)

                # â”€â”€ USERS TABLE â”€â”€
                # NOTE: username has NO UNIQUE constraint â€” usernames can be recycled
                # or temporarily shared; a constraint here causes silent insert failures.
                c.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        name TEXT,
                        username TEXT,
                        warnings INTEGER DEFAULT 0,
                        role TEXT DEFAULT 'member',
                        first_seen TEXT,
                        reputation INTEGER DEFAULT 0,
                        is_banned BOOLEAN DEFAULT 0,
                        banned_reason TEXT,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        msg_count INTEGER DEFAULT 0,
                        last_group_id TEXT,
                        last_group_name TEXT,
                        join_count INTEGER DEFAULT 0
                    )
                """)

                # â”€â”€ MIGRATION: Drop UNIQUE constraint on username if it exists â”€â”€
                # SQLite doesn't support DROP CONSTRAINT; we check the DDL and rebuild the table.
                # We check the actual CREATE TABLE SQL because SQLite names auto-indexes generically
                # (e.g. 'sqlite_autoindex_users_2'), not by column name.
                try:
                    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
                    row = c.fetchone()
                    current_ddl = row[0] if row else ""
                    # Detect UNIQUE on username column in the DDL
                    has_unique_username = "username TEXT UNIQUE" in current_ddl or "username text unique" in current_ddl.lower()
                    if has_unique_username:
                        print("ðŸ”§ Migrating users table: removing UNIQUE constraint on username...")
                        c.execute("""
                            CREATE TABLE IF NOT EXISTS users_new (
                                user_id TEXT PRIMARY KEY,
                                name TEXT,
                                username TEXT,
                                warnings INTEGER DEFAULT 0,
                                role TEXT DEFAULT 'member',
                                first_seen TEXT,
                                reputation INTEGER DEFAULT 0,
                                is_banned BOOLEAN DEFAULT 0,
                                banned_reason TEXT,
                                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                msg_count INTEGER DEFAULT 0,
                                last_group_id TEXT,
                                last_group_name TEXT,
                                join_count INTEGER DEFAULT 0
                            )
                        """)
                        c.execute("""
                            INSERT OR IGNORE INTO users_new
                            SELECT user_id, name, username, warnings, role, first_seen,
                                   reputation, is_banned, banned_reason, last_active,
                                   msg_count, last_group_id, last_group_name, join_count
                            FROM users
                        """)
                        c.execute("DROP TABLE users")
                        c.execute("ALTER TABLE users_new RENAME TO users")
                        print("âœ… Username UNIQUE constraint removed successfully.")
                except Exception as mig_err:
                    print(f"âš ï¸ Username migration check: {mig_err}")

                c.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active DESC)")

                # â”€â”€ GROUPS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS groups (
                        chat_id TEXT PRIMARY KEY,
                        name TEXT DEFAULT 'Unknown Group',
                        rules TEXT DEFAULT 'No rules set yet...',
                        welcome_message TEXT DEFAULT 'Welcome {name}! ðŸ‘‹',
                        welcome_type TEXT DEFAULT 'text',
                        welcome_file_id TEXT DEFAULT '',
                        leave_message TEXT DEFAULT 'Goodbye {name}!',
                        leave_type TEXT DEFAULT 'text',
                        leave_file_id TEXT DEFAULT '',
                        antispam INTEGER DEFAULT 0,
                        antispam_auto_delete_links INTEGER DEFAULT 1,
                        approve_mode BOOLEAN DEFAULT 0,
                        message_count INTEGER DEFAULT 0,
                        member_count INTEGER DEFAULT 0,
                        filter_count INTEGER DEFAULT 0,
                        language TEXT DEFAULT 'en',
                        strict_mode BOOLEAN DEFAULT 0,
                        log_channel_id TEXT,
                        max_warnings INTEGER DEFAULT 3,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slow_mode INTEGER DEFAULT 0,
                        linked_channel TEXT,
                        slow_mode_delay INTEGER DEFAULT 0,
                        captcha INTEGER DEFAULT 0,
                        captcha_mode TEXT DEFAULT 'button',
                        captcha_rules INTEGER DEFAULT 0,
                        captcha_mute_time TEXT DEFAULT '',
                        captcha_kick INTEGER DEFAULT 0,
                        captcha_kick_time TEXT DEFAULT '',
                        captcha_text TEXT DEFAULT 'Click to prove you are human'
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_groups_last_active ON groups(last_active DESC)")

                # â”€â”€ CAPTCHA MIGRATIONS â”€â”€
                for col in [
                    "captcha INTEGER DEFAULT 0",
                    "captcha_mode TEXT DEFAULT 'button'",
                    "captcha_rules INTEGER DEFAULT 0",
                    "captcha_mute_time TEXT DEFAULT ''",
                    "captcha_kick INTEGER DEFAULT 0",
                    "captcha_kick_time TEXT DEFAULT ''",
                    "captcha_text TEXT DEFAULT 'Click to prove you are human'"
                ]:
                    try:
                        c.execute(f"ALTER TABLE groups ADD COLUMN {col}")
                    except sqlite3.OperationalError:
                        pass # Column already exists

                # â”€â”€ CHAT ACTIVITY / RANKINGS SYSTEM â”€â”€
                # Per-group feature toggles for the chat activity system
                for col in [
                    "chat_tracking INTEGER DEFAULT 1",
                    "user_milestones INTEGER DEFAULT 1",
                    "group_milestones INTEGER DEFAULT 1",
                    "leaderboard INTEGER DEFAULT 1"
                ]:
                    try:
                        c.execute(f"ALTER TABLE groups ADD COLUMN {col}")
                    except sqlite3.OperationalError:
                        pass # Column already exists

                # ── KEYWORD ALERT SYSTEM ──
                for col in [
                    "keyword_alert INTEGER DEFAULT 0",
                    "keyword_alert_words TEXT DEFAULT '@admin,admin,help,support'",
                "lock_sticker INTEGER DEFAULT 0",
                "lock_animation INTEGER DEFAULT 0",
                "lock_media INTEGER DEFAULT 0",
                "lock_url INTEGER DEFAULT 0",
                "lock_forward INTEGER DEFAULT 0",
                "lock_inline INTEGER DEFAULT 0",
                "lock_poll INTEGER DEFAULT 0",
                "lock_game INTEGER DEFAULT 0"
                ]:
                    try:
                        c.execute(f"ALTER TABLE groups ADD COLUMN {col}")
                    except sqlite3.OperationalError:
                        pass # Column already exists

                # ── GRANULAR LOCKS (Rose-style) ──
                for col in [
                    "lock_sticker INTEGER DEFAULT 0",
                    "lock_animation INTEGER DEFAULT 0",
                    "lock_media INTEGER DEFAULT 0",
                    "lock_url INTEGER DEFAULT 0",
                    "lock_forward INTEGER DEFAULT 0",
                    "lock_inline INTEGER DEFAULT 0",
                    "lock_poll INTEGER DEFAULT 0",
                    "lock_game INTEGER DEFAULT 0"
                ]:
                    try:
                        c.execute(f"ALTER TABLE groups ADD COLUMN {col}")
                    except sqlite3.OperationalError:
                        pass # Column already exists

                # Per-user overall message totals per group
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_user_stats (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        display_name TEXT DEFAULT 'Unknown',
                        username TEXT,
                        total_messages INTEGER DEFAULT 0,
                        created_at TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (group_id, user_id)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cus_group ON chat_user_stats(group_id, total_messages DESC)")

                # Combined group message totals per day
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_daily_stats (
                        group_id TEXT NOT NULL,
                        date TEXT NOT NULL,
                        total_messages INTEGER DEFAULT 0,
                        PRIMARY KEY (group_id, date)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cds_group_date ON chat_daily_stats(group_id, date)")

                # Per-user message totals per day
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_user_daily_stats (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        date TEXT NOT NULL,
                        total_messages INTEGER DEFAULT 0,
                        PRIMARY KEY (group_id, user_id, date)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cuds_group_date ON chat_user_daily_stats(group_id, date, total_messages DESC)")

                # Per-user message totals per week (week boundary = Monday)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_user_weekly_stats (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        week_start TEXT NOT NULL,
                        total_messages INTEGER DEFAULT 0,
                        PRIMARY KEY (group_id, user_id, week_start)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cuws_group_week ON chat_user_weekly_stats(group_id, week_start, total_messages DESC)")

                # Individual user milestones (announced once per group/user/milestone)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_milestones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        milestone INTEGER NOT NULL,
                        achieved_at TEXT,
                        UNIQUE(group_id, user_id, milestone)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cm_group_user ON chat_milestones(group_id, user_id)")

                # Daily group milestones (announced once per group/date/milestone)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS chat_group_milestones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        date TEXT NOT NULL,
                        milestone INTEGER NOT NULL,
                        achieved_at TEXT,
                        UNIQUE(group_id, date, milestone)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_cgm_group_date ON chat_group_milestones(group_id, date)")

                # â”€â”€ APPROVED USERS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS approved_users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        approved_by TEXT,
                        approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(chat_id, user_id)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_chat_id ON approved_users(chat_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_user_id ON approved_users(user_id)")

                # â”€â”€ PENDING CAPTCHAS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS pending_captchas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        captcha_type TEXT,
                        correct_answer TEXT,
                        join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(chat_id, user_id)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_chat_id ON pending_captchas(chat_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_user_id ON pending_captchas(user_id)")

                # â”€â”€ FILTERS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS filters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        trigger TEXT NOT NULL,
                        filter_data TEXT NOT NULL,
                        UNIQUE(chat_id, trigger)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_filters_chat_id ON filters(chat_id)")

                # â”€â”€ BAD WORDS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS bad_words (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        word TEXT NOT NULL,
                        UNIQUE(chat_id, word)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_bad_words_chat_id ON bad_words(chat_id)")

                # â”€â”€ LOGS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp DESC)")

                # â”€â”€ GLOBAL BANS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS global_bans (
                        user_id TEXT PRIMARY KEY,
                        reason TEXT,
                        banned_by TEXT,
                        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # â”€â”€ USER MUTES TABLE (time-based) â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS user_mutes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        muted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        unmute_at TIMESTAMP NOT NULL,
                        reason TEXT,
                        muted_by TEXT,
                        is_active BOOLEAN DEFAULT 1,
                        UNIQUE(chat_id, user_id)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_chat_id ON user_mutes(chat_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_user_id ON user_mutes(user_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_unmute_at ON user_mutes(unmute_at)")

                # â”€â”€ USER BANS TABLE (time-based) â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS user_bans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        unban_at TIMESTAMP,
                        reason TEXT,
                        banned_by TEXT,
                        is_active BOOLEAN DEFAULT 1,
                        UNIQUE(chat_id, user_id)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_chat_id ON user_bans(chat_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_user_id ON user_bans(user_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_unban_at ON user_bans(unban_at)")

                # â”€â”€ INFRACTION HISTORY TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS infraction_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        action_type TEXT NOT NULL,
                        duration_seconds INTEGER,
                        reason TEXT,
                        issued_by TEXT,
                        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by TEXT
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_chat_user ON infraction_history(chat_id, user_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_issued_at ON infraction_history(issued_at DESC)")

                # â”€â”€ SCHEDULED MESSAGES TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS scheduled_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        message TEXT NOT NULL,
                        scheduled_at TIMESTAMP NOT NULL,
                        message_type TEXT DEFAULT 'text',
                        file_id TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(chat_id, scheduled_at)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat_id ON scheduled_messages(chat_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_scheduled_at ON scheduled_messages(scheduled_at)")

                # â”€â”€ USER STATS TABLE â”€â”€
                c.execute("""
                    CREATE TABLE IF NOT EXISTS user_stats (
                        user_id TEXT PRIMARY KEY,
                        messages_sent INTEGER DEFAULT 0,
                        links_shared INTEGER DEFAULT 0,
                        warnings_received INTEGER DEFAULT 0,
                        infractions_count INTEGER DEFAULT 0,
                        last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats(user_id)")

                # ── WHISPER MESSAGES (WSPBDBot) ──
                c.execute("""
                    CREATE TABLE IF NOT EXISTS Messages (
                        message_id TEXT PRIMARY KEY,
                        message_text TEXT NOT NULL,
                        name TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        tg_username TEXT,
                        targets TEXT,
                        secret TEXT,
                        fake TEXT,
                        chat_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON Messages(user_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_targets ON Messages(targets)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON Messages(created_at)")

                self.conn.commit()
                print("✅ Database migration check complete.")
            except Exception as e:
                print(f"❌ Migration failed: {e}")

    def _migrate_pg(self):
        """Create the same schema on PostgreSQL (Postgres-flavoured DDL)."""
        try:
            c = self.conn.cursor()

            # â”€â”€ CONFIG TABLE â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # â”€â”€ USERS TABLE â”€â”€ (no UNIQUE on username â€” usernames can be recycled)
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    username TEXT,
                    warnings INTEGER DEFAULT 0,
                    role TEXT DEFAULT 'member',
                    first_seen TEXT,
                    reputation INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    banned_reason TEXT,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    msg_count INTEGER DEFAULT 0,
                    last_group_id TEXT,
                    last_group_name TEXT,
                    join_count INTEGER DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active DESC)")

            # â”€â”€ GROUPS TABLE â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT DEFAULT 'Unknown Group',
                    rules TEXT DEFAULT 'No rules set yet...',
                    welcome_message TEXT DEFAULT 'Welcome {name}! ðŸ‘‹',
                    welcome_type TEXT DEFAULT 'text',
                    welcome_file_id TEXT DEFAULT '',
                    leave_message TEXT DEFAULT 'Goodbye {name}!',
                    leave_type TEXT DEFAULT 'text',
                    leave_file_id TEXT DEFAULT '',
                    antispam INTEGER DEFAULT 0,
                    antispam_auto_delete_links INTEGER DEFAULT 1,
                    approve_mode INTEGER DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    member_count INTEGER DEFAULT 0,
                    filter_count INTEGER DEFAULT 0,
                    language TEXT DEFAULT 'en',
                    strict_mode INTEGER DEFAULT 0,
                    log_channel_id TEXT,
                    max_warnings INTEGER DEFAULT 3,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    slow_mode INTEGER DEFAULT 0,
                    linked_channel TEXT,
                    slow_mode_delay INTEGER DEFAULT 0,
                    captcha INTEGER DEFAULT 0,
                    captcha_mode TEXT DEFAULT 'button',
                    captcha_rules INTEGER DEFAULT 0,
                    captcha_mute_time TEXT DEFAULT '',
                    captcha_kick INTEGER DEFAULT 0,
                    captcha_kick_time TEXT DEFAULT '',
                    captcha_text TEXT DEFAULT 'Click to prove you are human'
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_groups_last_active ON groups(last_active DESC)")

            # â”€â”€ CAPTCHA + CHAT ACTIVITY COLUMN MIGRATIONS â”€â”€
            for col in [
                "captcha INTEGER DEFAULT 0",
                "captcha_mode TEXT DEFAULT 'button'",
                "captcha_rules INTEGER DEFAULT 0",
                "captcha_mute_time TEXT DEFAULT ''",
                "captcha_kick INTEGER DEFAULT 0",
                "captcha_kick_time TEXT DEFAULT ''",
                "captcha_text TEXT DEFAULT 'Click to prove you are human'",
                "chat_tracking INTEGER DEFAULT 1",
                "user_milestones INTEGER DEFAULT 1",
                "group_milestones INTEGER DEFAULT 1",
                "leaderboard INTEGER DEFAULT 1"
            ]:
                c.execute(f"ALTER TABLE groups ADD COLUMN IF NOT EXISTS {col}")

            # â”€â”€ CHAT ACTIVITY / RANKINGS TABLES â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_user_stats (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT DEFAULT 'Unknown',
                    username TEXT,
                    total_messages INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (group_id, user_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cus_group ON chat_user_stats(group_id, total_messages DESC)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_daily_stats (
                    group_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    total_messages INTEGER DEFAULT 0,
                    PRIMARY KEY (group_id, date)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_group_date ON chat_daily_stats(group_id, date)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_user_daily_stats (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    total_messages INTEGER DEFAULT 0,
                    PRIMARY KEY (group_id, user_id, date)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cuds_group_date ON chat_user_daily_stats(group_id, date, total_messages DESC)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_user_weekly_stats (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    total_messages INTEGER DEFAULT 0,
                    PRIMARY KEY (group_id, user_id, week_start)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cuws_group_week ON chat_user_weekly_stats(group_id, week_start, total_messages DESC)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_milestones (
                    id BIGSERIAL PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    milestone INTEGER NOT NULL,
                    achieved_at TEXT,
                    UNIQUE(group_id, user_id, milestone)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cm_group_user ON chat_milestones(group_id, user_id)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS chat_group_milestones (
                    id BIGSERIAL PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    milestone INTEGER NOT NULL,
                    achieved_at TEXT,
                    UNIQUE(group_id, date, milestone)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cgm_group_date ON chat_group_milestones(group_id, date)")

            # â”€â”€ APPROVED USERS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS approved_users (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_chat_id ON approved_users(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_user_id ON approved_users(user_id)")

            # â”€â”€ PENDING CAPTCHAS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS pending_captchas (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    captcha_type TEXT,
                    correct_answer TEXT,
                    join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_chat_id ON pending_captchas(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_user_id ON pending_captchas(user_id)")

            # â”€â”€ FILTERS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS filters (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    filter_data TEXT NOT NULL,
                    UNIQUE(chat_id, trigger)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_filters_chat_id ON filters(chat_id)")

            # â”€â”€ BAD WORDS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS bad_words (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    word TEXT NOT NULL,
                    UNIQUE(chat_id, word)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_bad_words_chat_id ON bad_words(chat_id)")

            # â”€â”€ LOGS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id BIGSERIAL PRIMARY KEY,
                    event TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp DESC)")

            # â”€â”€ GLOBAL BANS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS global_bans (
                    user_id TEXT PRIMARY KEY,
                    reason TEXT,
                    banned_by TEXT,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # â”€â”€ USER MUTES (time-based) â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_mutes (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    muted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    unmute_at TIMESTAMP NOT NULL,
                    reason TEXT,
                    muted_by TEXT,
                    is_active INTEGER DEFAULT 1,
                    UNIQUE(chat_id, user_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_chat_id ON user_mutes(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_user_id ON user_mutes(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_unmute_at ON user_mutes(unmute_at)")

            # â”€â”€ USER BANS (time-based) â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_bans (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    unban_at TIMESTAMP,
                    reason TEXT,
                    banned_by TEXT,
                    is_active INTEGER DEFAULT 1,
                    UNIQUE(chat_id, user_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_chat_id ON user_bans(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_user_id ON user_bans(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_unban_at ON user_bans(unban_at)")

            # â”€â”€ INFRACTION HISTORY â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS infraction_history (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    duration_seconds INTEGER,
                    reason TEXT,
                    issued_by TEXT,
                    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_chat_user ON infraction_history(chat_id, user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_issued_at ON infraction_history(issued_at DESC)")

            # â”€â”€ SCHEDULED MESSAGES â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    message_type TEXT DEFAULT 'text',
                    file_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, scheduled_at)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat_id ON scheduled_messages(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_scheduled_at ON scheduled_messages(scheduled_at)")

            # â”€â”€ USER STATS â”€â”€
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id TEXT PRIMARY KEY,
                    messages_sent INTEGER DEFAULT 0,
                    links_shared INTEGER DEFAULT 0,
                    warnings_received INTEGER DEFAULT 0,
                    infractions_count INTEGER DEFAULT 0,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats(user_id)")

            self.conn.commit()
            print("âœ… PostgreSQL migration check complete.")
        except Exception as e:
            print(f"âŒ PostgreSQL migration failed: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def get_config(self):
        cached = _config_cache.get("config")
        if cached is not None:
            return dict(cached)

        if not self._check_conn():
            return {}
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT key, value FROM config")
                conf = {
                    "bot_token": "",
                    "is_running": False,
                    "owner_username": "",
                    "support_channel": "",
                    "main_group_link": ""
                }
                for row in c.fetchall():
                    try:
                        conf[row['key']] = json.loads(row['value'])
                    except:
                        conf[row['key']] = row['value']

                # Environment variable overrides (if set)
                env_token   = os.getenv('BOT_TOKEN')
                env_owner   = os.getenv('OWNER_USERNAME')
                env_running = os.getenv('IS_RUNNING')
                env_support = os.getenv('SUPPORT_CHANNEL')

                if env_token: conf["bot_token"] = env_token
                if env_owner: conf["owner_username"] = env_owner.replace("@", "")
                if env_running and env_running.lower() in ("1", "true", "yes"): conf["is_running"] = True
                if env_support: conf["support_channel"] = env_support.replace("@", "")

                _config_cache.set("config", conf)
                return conf
            except Exception as e:
                print(f"Error in get_config: {e}")
                return {}

    def update_config(self, key, value):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value))
                )
                self.conn.commit()
                _config_cache.invalidate("config")
            except Exception as e:
                print(f"Error in update_config: {e}")

    def ensure_group(self, chat_id, name=None):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT OR IGNORE INTO groups (chat_id, name, max_warnings, strict_mode, language) VALUES (?, ?, 3, 0, 'en')",
                    (str_id, name or 'Unknown Group')
                )
                if name:
                    c.execute(
                        "UPDATE groups SET name=? WHERE chat_id=? AND (name IS NULL OR name='Unknown Group')",
                        (name, str_id)
                    )
                self.conn.commit()
            except Exception as e:
                print(f"Error in ensure_group: {e}")

    def get_group(self, chat_id):
        str_id = str(chat_id)
        cached = _group_cache.get(f"group:{str_id}")
        if cached is not None:
            return dict(cached)

        if not self._check_conn():
            return {}
        self.ensure_group(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM groups WHERE chat_id=?", (str_id,))
                row = c.fetchone()
                if not row:
                    return {}

                group = dict(row)
                # Filters
                c.execute("SELECT trigger, filter_data FROM filters WHERE chat_id=?", (str_id,))
                group["filters"] = {r['trigger']: json.loads(r['filter_data']) for r in c.fetchall()}
                # Bad Words
                c.execute("SELECT word FROM bad_words WHERE chat_id=?", (str_id,))
                group["bad_words"] = [r['word'] for r in c.fetchall()]

                _group_cache.set(f"group:{str_id}", group)
                return group
            except Exception as e:
                print(f"Error in get_group: {e}")
                return {}

    def update_group_setting(self, chat_id, key, value):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if key == "bad_words":
                    c.execute("DELETE FROM bad_words WHERE chat_id=?", (str_id,))
                    for w in value:
                        c.execute("INSERT OR IGNORE INTO bad_words (chat_id, word) VALUES (?, ?)", (str_id, w.lower()))
                elif key == "filters":
                    c.execute("DELETE FROM filters WHERE chat_id=?", (str_id,))
                    for trigger, data in value.items():
                        c.execute("INSERT INTO filters (chat_id, trigger, filter_data) VALUES (?, ?, ?)",
                                (str_id, trigger.lower(), json.dumps(data)))
                else:
                    valid_keys = {"antispam", "antispam_auto_delete_links", "rules", "name", "welcome_message",
                                "welcome_type", "welcome_file_id", "leave_message", "leave_type", "leave_file_id",
                                "strict_mode", "max_warnings", "language", "member_count", "message_count",
                                "filter_count", "approve_mode", "slow_mode", "linked_channel", "slow_mode_delay",
                                "log_channel_id", "captcha", "captcha_mode", "captcha_rules", "captcha_mute_time",
                                "captcha_kick", "captcha_kick_time", "captcha_text",
                                "chat_tracking", "user_milestones", "group_milestones", "leaderboard",
                                "keyword_alert", "keyword_alert_words",
                                "lock_sticker", "lock_animation", "lock_media", "lock_url", "lock_forward", "lock_inline", "lock_poll", "lock_game"}
                    if key in valid_keys:
                        try:
                            c.execute(f"UPDATE groups SET {key}=? WHERE chat_id=?", (value, str_id))
                        except Exception as upd_e:
                            msg = str(upd_e).lower()
                            if "no such column" in msg or "does not exist" in msg or "column" in msg:
                                # Auto-migrate missing column then retry once
                                try:
                                    # Try PG syntax first, fallback to SQLite
                                    try:
                                        c.execute(f"ALTER TABLE groups ADD COLUMN IF NOT EXISTS {key} INTEGER DEFAULT 0")
                                    except:
                                        c.execute(f"ALTER TABLE groups ADD COLUMN {key} INTEGER DEFAULT 0")
                                    self.conn.commit()
                                    c.execute(f"UPDATE groups SET {key}=? WHERE chat_id=?", (value, str_id))
                                    print(f"Auto-migrated column {key} and updated")
                                except Exception as e2:
                                    print(f"Auto-migrate failed for {key}: {e2} / original: {upd_e}")
                                    raise
                            else:
                                raise
                self.conn.commit()
                _group_cache.invalidate(f"group:{str_id}")
            except Exception as e:
                print(f"Error in update_group_setting: {e}")

    # â”€â”€ CAPTCHA MANAGEMENT â”€â”€
    def add_pending_captcha(self, chat_id, user_id, captcha_type, correct_answer):
        if not self._check_conn(): return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO pending_captchas (chat_id, user_id, captcha_type, correct_answer, join_time) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (str(chat_id), str(user_id), captcha_type, str(correct_answer))
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error add_pending_captcha: {e}")

    def get_pending_captcha(self, chat_id, user_id):
        if not self._check_conn(): return None
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM pending_captchas WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
                row = c.fetchone()
                return dict(row) if row else None
            except Exception as e:
                print(f"Error get_pending_captcha: {e}")
                return None

    def remove_pending_captcha(self, chat_id, user_id):
        if not self._check_conn(): return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM pending_captchas WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
                self.conn.commit()
            except Exception as e:
                print(f"Error remove_pending_captcha: {e}")

    def get_all_pending_captchas(self):
        if not self._check_conn(): return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM pending_captchas")
                return [dict(row) for row in c.fetchall()]
            except Exception as e:
                print(f"Error get_all_pending_captchas: {e}")
                return []

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # APPROVAL SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def is_user_approved(self, chat_id, user_id):
        if not self._check_conn():
            return True  # fail-open
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT 1 FROM approved_users WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
                return c.fetchone() is not None
            except:
                return True

    def approve_user(self, chat_id, user_id, approved_by=None):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT OR IGNORE INTO approved_users (chat_id, user_id, approved_by) VALUES (?, ?, ?)",
                    (str(chat_id), str(user_id), str(approved_by) if approved_by else None)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in approve_user: {e}")

    def disapprove_user(self, chat_id, user_id):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM approved_users WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
                self.conn.commit()
            except Exception as e:
                print(f"Error in disapprove_user: {e}")

    def get_approved_users(self, chat_id):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT user_id, approved_by, approved_at FROM approved_users WHERE chat_id=? ORDER BY approved_at DESC", (str(chat_id),))
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def add_filter(self, chat_id, trigger, filter_data):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO filters (chat_id, trigger, filter_data) VALUES (?, ?, ?) ON CONFLICT(chat_id, trigger) DO UPDATE SET filter_data=excluded.filter_data",
                    (str_id, trigger.lower(), json.dumps(filter_data))
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in add_filter: {e}")

    def remove_filter(self, chat_id, trigger):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM filters WHERE chat_id=? AND trigger=?", (str_id, trigger.lower()))
                self.conn.commit()
            except Exception as e:
                print(f"Error in remove_filter: {e}")

    def get_user_info(self, user_id):
        if not self._check_conn():
            return {}
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT name, username, reputation, is_banned, banned_reason, warnings FROM users WHERE user_id=?", (str_id,))
                row = c.fetchone()
                return dict(row) if row else {"name": "Unknown", "username": None, "reputation": 0, "is_banned": False, "warnings": 0}
            except:
                return {}

    def ensure_user(self, user_id, name="Unknown", username=None, role="member", chat_id=None, chat_name=None, increment_msg=False, is_join=False):
        """
        Ensure a user exists in the database and update their profile.
        - increment_msg: True when called from a message event (increments msg_count)
        - is_join:       True when called from a join event (increments join_count)
        """
        if not self._check_conn():
            return
        str_id = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT user_id FROM users WHERE user_id=?", (str_id,))
                exists = c.fetchone()

                if not exists:
                    c.execute(
                        """INSERT INTO users (user_id, name, username, role, first_seen, last_active, last_group_id, last_group_name, msg_count, join_count)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str_id,
                            name or "Unknown",
                            username or None,
                            role,
                            now,
                            now,
                            str(chat_id) if chat_id else None,
                            chat_name,
                            1 if increment_msg else 0,
                            1 if is_join else 0
                        )
                    )
                else:
                    c.execute(
                        """UPDATE users SET
                           name=?,
                           username=?,
                           last_active=?,
                           last_group_id=COALESCE(?, last_group_id),
                           last_group_name=COALESCE(?, last_group_name),
                           msg_count=msg_count + ?,
                           join_count=join_count + ?
                           WHERE user_id=?""",
                        (
                            name or "Unknown",
                            username or None,
                            now,
                            str(chat_id) if chat_id else None,
                            chat_name,
                            1 if increment_msg else 0,
                            1 if is_join else 0,
                            str_id
                        )
                    )
                self.conn.commit()
            except Exception as e:
                print(f"Error in ensure_user (user_id={str_id}): {e}")

    def add_message_count(self, chat_id):
        if not self._check_conn():
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE groups SET message_count = message_count + 1, last_active = ? WHERE chat_id=?", (now, str(chat_id)))
                self.conn.commit()
            except:
                pass

    def get_extra_group_info(self):
        if not self._check_conn():
            return {}
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT chat_id, name, message_count, member_count, last_active, antispam, strict_mode, max_warnings FROM groups")
                return {row['chat_id']: dict(row) for row in c.fetchall()}
            except:
                return {}

    def get_user(self, user_id):
        if not self._check_conn():
            return {}
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM users WHERE user_id=?", (str_id,))
                row = c.fetchone()
                return dict(row) if row else {}
            except:
                return {}

    def get_user_by_username(self, username):
        if not self._check_conn():
            return {}
        uname = username.replace("@", "").strip().lower()
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM users WHERE LOWER(username)=?", (uname,))
                row = c.fetchone()
                return dict(row) if row else {}
            except:
                return {}

    def add_warning(self, user_id, name="Unknown"):
        if not self._check_conn():
            return 0
        self.ensure_user(user_id, name)
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = warnings + 1 WHERE user_id=?", (str_id,))
                c.execute("SELECT warnings FROM users WHERE user_id=?", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return row['warnings'] if row else 0
            except:
                return 0

    def remove_warning(self, user_id):
        """Remove one warning (decrement by 1, not below 0). Returns new count."""
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = CASE WHEN warnings > 0 THEN warnings - 1 ELSE 0 END WHERE user_id=?", (str_id,))
                c.execute("SELECT warnings FROM users WHERE user_id=?", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return int(row['warnings']) if row else 0
            except:
                return 0

    def reset_warnings(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = 0 WHERE user_id=?", (str_id,))
                self.conn.commit()
            except:
                pass

    def get_all_stats(self):
        base = {"total_users": 0, "total_groups": 0, "total_messages": 0, "total_logs": 0,
                "total_filters": 0, "total_bad_words": 0, "total_warned_users": 0,
                "total_mutes": 0, "total_bans": 0, "total_scheduled": 0}
        if not self._check_conn():
            return base
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT COUNT(*) as cu FROM users")
                base['total_users'] = c.fetchone()['cu']
                c.execute("SELECT COUNT(*) as cg FROM groups")
                base['total_groups'] = c.fetchone()['cg']
                c.execute("SELECT SUM(message_count) as cm FROM groups")
                row = c.fetchone()
                base['total_messages'] = row['cm'] if row['cm'] else 0
                c.execute("SELECT COUNT(*) as cl FROM logs")
                base['total_logs'] = c.fetchone()['cl']
                c.execute("SELECT COUNT(*) as cf FROM filters")
                base['total_filters'] = c.fetchone()['cf']
                c.execute("SELECT COUNT(*) as cb FROM bad_words")
                base['total_bad_words'] = c.fetchone()['cb']
                c.execute("SELECT COUNT(*) as cw FROM users WHERE warnings > 0")
                base['total_warned_users'] = c.fetchone()['cw']
                c.execute("SELECT COUNT(*) as cm FROM user_mutes WHERE is_active=1")
                base['total_mutes'] = c.fetchone()['cm']
                c.execute("SELECT COUNT(*) as cb FROM user_bans WHERE is_active=1")
                base['total_bans'] = c.fetchone()['cb']
                c.execute("SELECT COUNT(*) as cs FROM scheduled_messages WHERE status='pending'")
                base['total_scheduled'] = c.fetchone()['cs']
                return base
            except:
                return base

    def get_all_users(self):
        if not self._check_conn():
            return {}
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM users ORDER BY first_seen DESC")
                return {row['user_id']: dict(row) for row in c.fetchall()}
            except:
                return {}

    def get_all_groups(self):
        if not self._check_conn():
            return {}
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    SELECT g.chat_id, g.name, g.message_count, g.member_count,
                           g.last_active, g.antispam, g.antispam_auto_delete_links,
                           g.keyword_alert, g.keyword_alert_words,
                           g.lock_sticker, g.lock_animation, g.lock_media, g.lock_url,
                           g.lock_forward, g.lock_inline, g.lock_poll, g.lock_game,
                           g.welcome_message, g.welcome_type, g.welcome_file_id,
                           g.leave_message, g.leave_type, g.leave_file_id, g.strict_mode, g.max_warnings,
                           (SELECT COUNT(*) FROM filters WHERE chat_id = g.chat_id) as filter_count,
                           (SELECT COUNT(*) FROM bad_words WHERE chat_id = g.chat_id) as bad_words_count
                    FROM groups g ORDER BY g.last_active DESC
                """)
                return {row['chat_id']: dict(row) for row in c.fetchall()}
            except Exception as e:
                # Fallback for DBs not yet migrated (old columns missing)
                try:
                    c = self.conn.cursor()
                    c.execute("""
                        SELECT g.chat_id, g.name, g.message_count, g.member_count,
                               g.last_active, g.antispam, g.welcome_message, g.welcome_type, g.welcome_file_id,
                               g.leave_message, g.leave_type, g.leave_file_id, g.strict_mode, g.max_warnings,
                               (SELECT COUNT(*) FROM filters WHERE chat_id = g.chat_id) as filter_count,
                               (SELECT COUNT(*) FROM bad_words WHERE chat_id = g.chat_id) as bad_words_count
                        FROM groups g ORDER BY g.last_active DESC
                    """)
                    result = {}
                    for row in c.fetchall():
                        d = dict(row)
                        d.setdefault("antispam_auto_delete_links", 1)
                        d.setdefault("keyword_alert", 0)
                        d.setdefault("keyword_alert_words", "@admin,admin,help,support")
                        d.setdefault("lock_sticker", 0)
                        d.setdefault("lock_animation", 0)
                        d.setdefault("lock_media", 0)
                        d.setdefault("lock_url", 0)
                        d.setdefault("lock_forward", 0)
                        d.setdefault("lock_inline", 0)
                        d.setdefault("lock_poll", 0)
                        d.setdefault("lock_game", 0)
                        result[d["chat_id"]] = d
                    # Trigger migration for next request
                    try:
                        self._migrate()
                    except:
                        pass
                    return result
                except:
                    return {}

    def search_items(self, query):
        if not self._check_conn():
            return {}, {}
        with self.lock:
            try:
                c = self.conn.cursor()
                q = f"%{query}%"
                c.execute("SELECT * FROM users WHERE user_id LIKE ? OR name LIKE ? OR username LIKE ?", (q, q, q))
                users = {row['user_id']: dict(row) for row in c.fetchall()}
                c.execute("SELECT * FROM groups WHERE chat_id LIKE ? OR name LIKE ?", (q, q))
                groups = {row['chat_id']: dict(row) for row in c.fetchall()}
                return users, groups
            except:
                return {}, {}

    def get_warnings_leaderboard(self, limit=10):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT user_id, name, warnings FROM users WHERE warnings > 0 ORDER BY warnings DESC LIMIT ?", (limit,))
                return [dict(row) for row in c.fetchall()]
            except:
                return []

    def delete_user(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM users WHERE user_id=?", (str_id,))
                self.conn.commit()
            except:
                pass

    def delete_group(self, chat_id):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM groups WHERE chat_id=?", (str_id,))
                c.execute("DELETE FROM filters WHERE chat_id=?", (str_id,))
                c.execute("DELETE FROM bad_words WHERE chat_id=?", (str_id,))
                c.execute("DELETE FROM approved_users WHERE chat_id=?", (str_id,))
                self.conn.commit()
            except:
                pass

    def global_ban_user(self, user_id, reason="Admin banned", banner="System"):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET is_banned=1, banned_reason=? WHERE user_id=?", (reason, str_id))
                c.execute("INSERT OR REPLACE INTO global_bans (user_id, reason, banned_by) VALUES (?, ?, ?)", (str_id, reason, banner))
                self.conn.commit()
            except:
                pass

    def global_unban_user(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET is_banned=0, banned_reason=NULL WHERE user_id=?", (str_id,))
                c.execute("DELETE FROM global_bans WHERE user_id=?", (str_id,))
                self.conn.commit()
            except:
                pass

    def get_global_bans(self):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM global_bans ORDER BY banned_at DESC")
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def update_reputation(self, user_id, amount):
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id=?", (amount, str_id))
                c.execute("SELECT reputation FROM users WHERE user_id=?", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return row['reputation'] if row else 0
            except:
                return 0

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # TIME-BASED MUTE/BAN SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def mute_user(self, chat_id, user_id, duration_seconds, reason="Spam", muted_by=None):
        """Add a temporary mute for user in chat"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        unmute_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO user_mutes (chat_id, user_id, unmute_at, reason, muted_by, is_active)
                       VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET unmute_at=excluded.unmute_at, reason=excluded.reason, is_active=1""",
                    (str_chat, str_user, unmute_at, reason, muted_by)
                )
                self.conn.commit()
                self.log_event(f"ðŸ”‡ Mute: {str_user} in {str_chat} for {duration_seconds}s by {muted_by}")
            except Exception as e:
                print(f"Error in mute_user: {e}")

    def unmute_user(self, chat_id, user_id):
        """Manually unmute user"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM user_mutes WHERE chat_id=? AND user_id=?", (str_chat, str_user))
                self.conn.commit()
            except Exception as e:
                print(f"Error in unmute_user: {e}")

    def is_user_muted(self, chat_id, user_id):
        """Check if user is currently muted"""
        if not self._check_conn():
            return False
        str_chat = str(chat_id)
        str_user = str(user_id)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_mutes WHERE chat_id=? AND user_id=? AND is_active=1 AND unmute_at > ?", (str_chat, str_user, now_str))
                return c.fetchone() is not None
            except:
                return False

    def get_expired_mutes(self):
        """Get all mutes that have expired (for auto-unmute)"""
        if not self._check_conn():
            return []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_mutes WHERE is_active=1 AND unmute_at <= ?", (now_str,))
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def ban_user(self, chat_id, user_id, duration_seconds=None, reason="Violation", banned_by=None):
        """Add a temporary/permanent ban for user in chat"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        unban_at = None
        if duration_seconds:
            unban_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO user_bans (chat_id, user_id, unban_at, reason, banned_by, is_active)
                       VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET unban_at=excluded.unban_at, reason=excluded.reason, is_active=1""",
                    (str_chat, str_user, unban_at, reason, banned_by)
                )
                self.conn.commit()
                dur_str = f"{duration_seconds}s" if duration_seconds else "permanent"
                self.log_event(f"ðŸš« Ban: {str_user} in {str_chat} ({dur_str}) by {banned_by}")
            except Exception as e:
                print(f"Error in ban_user: {e}")

    def unban_user(self, chat_id, user_id):
        """Manually unban user"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM user_bans WHERE chat_id=? AND user_id=?", (str_chat, str_user))
                self.conn.commit()
            except Exception as e:
                print(f"Error in unban_user: {e}")

    def is_user_banned(self, chat_id, user_id):
        """Check if user is currently banned (temp or permanent)"""
        if not self._check_conn():
            return False
        str_chat = str(chat_id)
        str_user = str(user_id)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_bans WHERE chat_id=? AND user_id=? AND is_active=1 AND (unban_at IS NULL OR unban_at > ?)", (str_chat, str_user, now_str))
                return c.fetchone() is not None
            except:
                return False

    def get_expired_bans(self):
        """Get all bans that have expired (for auto-unban)"""
        if not self._check_conn():
            return []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_bans WHERE is_active=1 AND unban_at IS NOT NULL AND unban_at <= ?", (now_str,))
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def get_mutes(self, chat_id):
        """Get all active mutes in a chat"""
        if not self._check_conn():
            return []
        str_chat = str(chat_id)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_mutes WHERE chat_id=? AND is_active=1 AND unmute_at > ? ORDER BY unmute_at ASC", (str_chat, now_str))
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def get_bans(self, chat_id):
        """Get all active bans in a chat"""
        if not self._check_conn():
            return []
        str_chat = str(chat_id)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_bans WHERE chat_id=? AND is_active=1 AND (unban_at IS NULL OR unban_at > ?) ORDER BY banned_at DESC", (str_chat, now_str))
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def get_all_active_mutes(self):
        """Get all active mutes across all chats"""
        if not self._check_conn():
            return []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    SELECT m.*, g.name as group_name, u.name as user_name 
                    FROM user_mutes m
                    LEFT JOIN groups g ON m.chat_id = g.chat_id
                    LEFT JOIN users u ON m.user_id = u.user_id
                    WHERE m.is_active=1 AND m.unmute_at > ?
                    ORDER BY m.unmute_at ASC
                """, (now_str,))
                return [dict(r) for r in c.fetchall()]
            except Exception as e:
                print(f"Error in get_all_active_mutes: {e}")
                return []

    def get_all_active_bans(self):
        """Get all active bans across all chats"""
        if not self._check_conn():
            return []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    SELECT b.*, g.name as group_name, u.name as user_name 
                    FROM user_bans b
                    LEFT JOIN groups g ON b.chat_id = g.chat_id
                    LEFT JOIN users u ON b.user_id = u.user_id
                    WHERE b.is_active=1 AND (b.unban_at IS NULL OR b.unban_at > ?)
                    ORDER BY b.banned_at DESC
                """, (now_str,))
                return [dict(r) for r in c.fetchall()]
            except Exception as e:
                print(f"Error in get_all_active_bans: {e}")
                return []

    def log_infraction(self, chat_id, user_id, action_type, duration_seconds=None, reason=None, issued_by=None):
        """Log moderation action for audit trail"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO infraction_history (chat_id, user_id, action_type, duration_seconds, reason, issued_by)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (str_chat, str_user, action_type, duration_seconds, reason, issued_by)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in log_infraction: {e}")

    def get_user_infractions(self, chat_id, user_id, limit=10):
        """Get infraction history for user in chat"""
        if not self._check_conn():
            return []
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "SELECT * FROM infraction_history WHERE chat_id=? AND user_id=? ORDER BY issued_at DESC LIMIT ?",
                    (str_chat, str_user, limit)
                )
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SCHEDULED MESSAGES SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def add_scheduled_message(self, chat_id, message, scheduled_at, message_type="text", file_id=None):
        """Add a scheduled message to the database"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO scheduled_messages (chat_id, message, scheduled_at, message_type, file_id, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (str_chat, message, scheduled_at, message_type, file_id)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in add_scheduled_message: {e}")

    def get_pending_scheduled_messages(self):
        """Get all pending scheduled messages that are due"""
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM scheduled_messages WHERE status='pending' AND scheduled_at <= datetime('now') ORDER BY scheduled_at ASC")
                return [dict(r) for r in c.fetchall()]
            except:
                return []

    def update_scheduled_message_status(self, message_id, status):
        """Update the status of a scheduled message"""
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE scheduled_messages SET status=? WHERE id=?", (status, message_id))
                self.conn.commit()
            except Exception as e:
                print(f"Error in update_scheduled_message_status: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # LOG SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def log_event(self, event):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("INSERT INTO logs (event) VALUES (?)", (str(event),))
                self.conn.commit()
            except Exception as e:
                print(f"Error in log_event: {e}")

    def get_recent_logs(self, limit=50):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT event, timestamp FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,))
                return [dict(row) for row in c.fetchall()]
            except:
                return []

    def clear_logs(self):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM logs")
                self.conn.commit()
            except Exception as e:
                print(f"Error in clear_logs: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # TRACKING SYSTEM CONFIG
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_tracking_config(self):
        cached = _config_cache.get("tracking_config")
        if cached is not None:
            return dict(cached)
            
        config = self.get_config()
        tracking = {
            "tracking_destination_group": config.get("tracking_destination_group", ""),
            "tracked_users": config.get("tracked_users", []),
            "tracked_groups": config.get("tracked_groups", [])
        }
        _config_cache.set("tracking_config", tracking)
        return tracking
        
    def add_tracked_user(self, user_id):
        tracking = self.get_tracking_config()
        if str(user_id) not in tracking["tracked_users"]:
            tracking["tracked_users"].append(str(user_id))
            self.update_config("tracked_users", tracking["tracked_users"])
            _config_cache.invalidate("tracking_config")
            
    def remove_tracked_user(self, user_id):
        tracking = self.get_tracking_config()
        if str(user_id) in tracking["tracked_users"]:
            tracking["tracked_users"].remove(str(user_id))
            self.update_config("tracked_users", tracking["tracked_users"])
            _config_cache.invalidate("tracking_config")
            
    def add_tracked_group(self, group_id):
        tracking = self.get_tracking_config()
        if str(group_id) not in tracking["tracked_groups"]:
            tracking["tracked_groups"].append(str(group_id))
            self.update_config("tracked_groups", tracking["tracked_groups"])
            _config_cache.invalidate("tracking_config")
            
    def remove_tracked_group(self, group_id):
        tracking = self.get_tracking_config()
        if str(group_id) in tracking["tracked_groups"]:
            tracking["tracked_groups"].remove(str(group_id))
            self.update_config("tracked_groups", tracking["tracked_groups"])
            _config_cache.invalidate("tracking_config")
            
    def set_tracking_destination(self, dest_id):
        self.update_config("tracking_destination_group", str(dest_id))
        _config_cache.invalidate("tracking_config")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # USER STATS SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def update_user_stats(self, user_id, messages_sent=0, links_shared=0):
        """Update user statistics"""
        if not self._check_conn():
            return
        str_id = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    INSERT INTO user_stats (user_id, messages_sent, links_shared, last_activity)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                    messages_sent = messages_sent + ?,
                    links_shared = links_shared + ?,
                    last_activity = ?
                """, (str_id, messages_sent, links_shared, now, messages_sent, links_shared, now))
                self.conn.commit()
            except Exception as e:
                print(f"Error in update_user_stats: {e}")

    def get_user_stats(self, user_id):
        """Get user statistics"""
        if not self._check_conn():
            return {}
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM user_stats WHERE user_id=?", (str_id,))
                row = c.fetchone()
                return dict(row) if row else {"user_id": str_id, "messages_sent": 0, "links_shared": 0, "warnings_received": 0, "infractions_count": 0, "last_activity": None}
            except:
                return {}

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # BATCH OPERATIONS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def batch_update_users(self, updates):
        """Batch update multiple users"""
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                for user_id, data in updates.items():
                    str_id = str(user_id)
                    c.execute("""
                        UPDATE users SET
                            name=?,
                            username=?
                        WHERE user_id=?
                    """, (data.get('name'), data.get('username'), str_id))
                self.conn.commit()
            except Exception as e:
                print(f"Error in batch_update_users: {e}")

    def export_database(self, format='json'):
        """Export database data in specified format"""
        if not self._check_conn():
            return None
        with self.lock:
            try:
                c = self.conn.cursor()
                data = {
                    'users': [],
                    'groups': [],
                    'filters': [],
                    'bad_words': [],
                    'global_bans': [],
                    'scheduled_messages': []
                }

                # Export users
                c.execute("SELECT * FROM users")
                data['users'] = [dict(row) for row in c.fetchall()]

                # Export groups
                c.execute("SELECT * FROM groups")
                data['groups'] = [dict(row) for row in c.fetchall()]

                # Export filters
                c.execute("SELECT * FROM filters")
                data['filters'] = [dict(row) for row in c.fetchall()]

                # Export bad words
                c.execute("SELECT * FROM bad_words")
                data['bad_words'] = [dict(row) for row in c.fetchall()]

                # Export global bans
                c.execute("SELECT * FROM global_bans")
                data['global_bans'] = [dict(row) for row in c.fetchall()]

                # Export scheduled messages
                c.execute("SELECT * FROM scheduled_messages")
                data['scheduled_messages'] = [dict(row) for row in c.fetchall()]

                return data
            except Exception as e:
                print(f"Error in export_database: {e}")
                return None

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # UTILITY METHODS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_chat_members(self, chat_id, limit=100):
        """Get list of members in a chat (stub - requires Telegram API)"""
        # This would require making API calls to get actual members
        # For now, return empty list
        return []

    def replace_database(self, backup_file):
        """Replace current database with a backup file (SQLite only)."""
        if self.backend == "pg":
            print("âŒ replace_database is not supported on PostgreSQL.")
            return False
        if not self._check_conn():
            return False
        try:
            # Close current connection
            if self.conn:
                self.conn.close()
                self.conn = None

            # Replace database file
            import shutil
            shutil.copy2(backup_file, self.db_path)

            # Reconnect
            self._connect()
            self._migrate()
            return True
        except Exception as e:
            print(f"Error replacing database: {e}")
            return False

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # CHAT ACTIVITY / RANKINGS SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def track_chat_message(self, chat_id, user_id, display_name, username, today_str, week_start,
                           user_milestones, group_milestones):
        """
        Atomically record a single valid group message and check milestones.

        user_milestones / group_milestones: lists of thresholds to evaluate
        (pass empty lists to skip milestone recording for a category).

        Returns a dict:
          {user_total, group_today_total, user_milestones:[...], group_milestones:[...]}
        Only milestones that were newly achieved (recorded for the first time)
        are included in the returned lists, so announcements never duplicate.
        """
        result = {"user_total": 0, "group_today_total": 0, "user_milestones": [], "group_milestones": []}
        if not self._check_conn():
            return result
        gid = str(chat_id)
        uid = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()

                # 1) Overall per-user total
                c.execute("""
                    INSERT INTO chat_user_stats (group_id, user_id, display_name, username, total_messages, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        username = CASE WHEN excluded.username IS NOT NULL THEN excluded.username ELSE chat_user_stats.username END,
                        total_messages = chat_user_stats.total_messages + 1,
                        updated_at = excluded.updated_at
                """, (gid, uid, display_name or "Unknown", username, now, now))

                # 2) Daily group total
                c.execute("""
                    INSERT INTO chat_daily_stats (group_id, date, total_messages)
                    VALUES (?, ?, 1)
                    ON CONFLICT(group_id, date) DO UPDATE SET
                        total_messages = chat_daily_stats.total_messages + 1
                """, (gid, today_str))
                c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=? AND date=?", (gid, today_str))
                row = c.fetchone()
                result["group_today_total"] = int(row["total_messages"]) if row else 1

                # 3) Daily per-user total
                c.execute("""
                    INSERT INTO chat_user_daily_stats (group_id, user_id, date, total_messages)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id, user_id, date) DO UPDATE SET
                        total_messages = chat_user_daily_stats.total_messages + 1
                """, (gid, uid, today_str))

                # 4) Weekly per-user total
                c.execute("""
                    INSERT INTO chat_user_weekly_stats (group_id, user_id, week_start, total_messages)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id, user_id, week_start) DO UPDATE SET
                        total_messages = chat_user_weekly_stats.total_messages + 1
                """, (gid, uid, week_start))

                # 5) Overall user total (for milestone checks)
                c.execute("SELECT total_messages FROM chat_user_stats WHERE group_id=? AND user_id=?", (gid, uid))
                row = c.fetchone()
                result["user_total"] = int(row["total_messages"]) if row else 1

                # 6) User milestones â€” recorded once ever per group/user/milestone.
                #    INSERT OR IGNORE + rowcount makes this safe under concurrency.
                for m in user_milestones:
                    if result["user_total"] >= m:
                        c.execute(
                            "INSERT OR IGNORE INTO chat_milestones (group_id, user_id, milestone, achieved_at) VALUES (?, ?, ?, ?)",
                            (gid, uid, m, now)
                        )
                        if c.rowcount > 0:
                            result["user_milestones"].append(m)

                # 7) Daily group milestones â€” recorded once per group/date/milestone.
                for m in group_milestones:
                    if result["group_today_total"] >= m:
                        c.execute(
                            "INSERT OR IGNORE INTO chat_group_milestones (group_id, date, milestone, achieved_at) VALUES (?, ?, ?, ?)",
                            (gid, today_str, m, now)
                        )
                        if c.rowcount > 0:
                            result["group_milestones"].append(m)

                self.conn.commit()
            except Exception as e:
                print(f"Error in track_chat_message: {e}")
        return result

    def get_chat_rankings(self, chat_id, mode="overall", ref_key=None, limit=10, offset=0):
        """
        Fetch ranked rows for a group.
        mode: 'overall' | 'today' | 'week'
        ref_key: today date string (today) or week_start string (week); ignored for overall.
        """
        if not self._check_conn():
            return []
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if mode == "today":
                    c.execute("""
                        SELECT s.user_id,
                               COALESCE(u.display_name, 'Unknown') AS display_name,
                               u.username,
                               s.total_messages
                        FROM chat_user_daily_stats s
                        LEFT JOIN chat_user_stats u ON u.group_id = s.group_id AND u.user_id = s.user_id
                        WHERE s.group_id = ? AND s.date = ?
                        ORDER BY s.total_messages DESC, s.user_id
                        LIMIT ? OFFSET ?
                    """, (gid, ref_key, limit, offset))
                elif mode == "week":
                    c.execute("""
                        SELECT s.user_id,
                               COALESCE(u.display_name, 'Unknown') AS display_name,
                               u.username,
                               s.total_messages
                        FROM chat_user_weekly_stats s
                        LEFT JOIN chat_user_stats u ON u.group_id = s.group_id AND u.user_id = s.user_id
                        WHERE s.group_id = ? AND s.week_start = ?
                        ORDER BY s.total_messages DESC, s.user_id
                        LIMIT ? OFFSET ?
                    """, (gid, ref_key, limit, offset))
                else:
                    c.execute("""
                        SELECT user_id, display_name, username, total_messages
                        FROM chat_user_stats
                        WHERE group_id = ?
                        ORDER BY total_messages DESC, user_id
                        LIMIT ? OFFSET ?
                    """, (gid, limit, offset))
                return [dict(r) for r in c.fetchall()]
            except Exception as e:
                print(f"Error in get_chat_rankings: {e}")
                return []

    def count_chat_rankings(self, chat_id, mode="overall", ref_key=None):
        """Count ranked users for a group (used for pagination)."""
        if not self._check_conn():
            return 0
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if mode == "today":
                    c.execute("SELECT COUNT(*) AS n FROM chat_user_daily_stats WHERE group_id=? AND date=?", (gid, ref_key))
                elif mode == "week":
                    c.execute("SELECT COUNT(*) AS n FROM chat_user_weekly_stats WHERE group_id=? AND week_start=?", (gid, ref_key))
                else:
                    c.execute("SELECT COUNT(*) AS n FROM chat_user_stats WHERE group_id=?", (gid,))
                row = c.fetchone()
                return int(row["n"]) if row else 0
            except Exception as e:
                print(f"Error in count_chat_rankings: {e}")
                return 0

    def get_chat_total_messages(self, chat_id, mode="overall", ref_key=None):
        """Total message count for a group in the given mode."""
        if not self._check_conn():
            return 0
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if mode == "today":
                    c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=? AND date=?", (gid, ref_key))
                    row = c.fetchone()
                    return int(row["total_messages"]) if row else 0
                elif mode == "week":
                    c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_weekly_stats WHERE group_id=? AND week_start=?", (gid, ref_key))
                else:
                    c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_stats WHERE group_id=?", (gid,))
                row = c.fetchone()
                return int(row["n"]) if row else 0
            except Exception as e:
                print(f"Error in get_chat_total_messages: {e}")
                return 0

    def get_user_chat_stats(self, chat_id, user_id, today_str, week_start):
        """Personal stats for one user in one group (overall/today/week + overall rank).

        rank is the user's 1-based overall position among all tracked users in the
        group (tie-safe: users with equal totals share the rank determined by the
        number of users strictly above). Returns rank=None when the user has no
        tracked messages at all.
        """
        result = {
            "total_messages": 0,
            "today_messages": 0,
            "week_messages": 0,
            "rank": None,
            "display_name": "Unknown",
            "username": None,
        }
        if not self._check_conn():
            return result
        gid = str(chat_id)
        uid = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                has_total = False
                c.execute(
                    "SELECT display_name, username, total_messages FROM chat_user_stats WHERE group_id=? AND user_id=?",
                    (gid, uid),
                )
                row = c.fetchone()
                if row:
                    has_total = True
                    result["display_name"] = row["display_name"] or "Unknown"
                    result["username"] = row["username"]
                    result["total_messages"] = int(row["total_messages"])

                c.execute(
                    "SELECT total_messages FROM chat_user_daily_stats WHERE group_id=? AND user_id=? AND date=?",
                    (gid, uid, today_str),
                )
                row = c.fetchone()
                if row:
                    result["today_messages"] = int(row["total_messages"])

                c.execute(
                    "SELECT total_messages FROM chat_user_weekly_stats WHERE group_id=? AND user_id=? AND week_start=?",
                    (gid, uid, week_start),
                )
                row = c.fetchone()
                if row:
                    result["week_messages"] = int(row["total_messages"])

                if has_total:
                    c.execute(
                        """
                        SELECT COUNT(*) AS n FROM chat_user_stats
                        WHERE group_id=? AND total_messages > ?
                        """,
                        (gid, result["total_messages"]),
                    )
                    row = c.fetchone()
                    result["rank"] = (int(row["n"]) if row else 0) + 1
                return result
            except Exception as e:
                print(f"Error in get_user_chat_stats: {e}")
                return result

    def get_chat_group_stats(self, chat_id, today_str, week_start):
        """Aggregate chat stats for a group (total / today / week / active-today / top chatter)."""
        result = {
            "total_messages": 0,
            "today_messages": 0,
            "week_messages": 0,
            "active_today": 0,
            "top_chatter": None,
        }
        if not self._check_conn():
            return result
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()

                c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_stats WHERE group_id=?", (gid,))
                row = c.fetchone()
                result["total_messages"] = int(row["n"]) if row else 0

                c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=? AND date=?", (gid, today_str))
                row = c.fetchone()
                result["today_messages"] = int(row["total_messages"]) if row else 0

                c.execute(
                    "SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_weekly_stats WHERE group_id=? AND week_start=?",
                    (gid, week_start),
                )
                row = c.fetchone()
                result["week_messages"] = int(row["n"]) if row else 0

                c.execute(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM chat_user_daily_stats WHERE group_id=? AND date=?",
                    (gid, today_str),
                )
                row = c.fetchone()
                result["active_today"] = int(row["n"]) if row else 0

                c.execute(
                    """
                    SELECT display_name, username FROM chat_user_stats
                    WHERE group_id=? ORDER BY total_messages DESC, user_id LIMIT 1
                    """,
                    (gid,),
                )
                row = c.fetchone()
                if row:
                    result["top_chatter"] = {
                        "display_name": row["display_name"] or "Unknown",
                        "username": row["username"],
                    }
                return result
            except Exception as e:
                print(f"Error in get_chat_group_stats: {e}")
                return result

    def reset_chat_user_stats(self, chat_id, user_id=None):
        """Administrator reset for a group's (or one user's) chat statistics."""
        if not self._check_conn():
            return
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if user_id is not None:
                    uid = str(user_id)
                    c.execute("DELETE FROM chat_user_stats WHERE group_id=? AND user_id=?", (gid, uid))
                    c.execute("DELETE FROM chat_user_daily_stats WHERE group_id=? AND user_id=?", (gid, uid))
                    c.execute("DELETE FROM chat_user_weekly_stats WHERE group_id=? AND user_id=?", (gid, uid))
                    c.execute("DELETE FROM chat_milestones WHERE group_id=? AND user_id=?", (gid, uid))
                else:
                    c.execute("DELETE FROM chat_user_stats WHERE group_id=?", (gid,))
                    c.execute("DELETE FROM chat_daily_stats WHERE group_id=?", (gid,))
                    c.execute("DELETE FROM chat_user_daily_stats WHERE group_id=?", (gid,))
                    c.execute("DELETE FROM chat_user_weekly_stats WHERE group_id=?", (gid,))
                    c.execute("DELETE FROM chat_milestones WHERE group_id=?", (gid,))
                    c.execute("DELETE FROM chat_group_milestones WHERE group_id=?", (gid,))
                self.conn.commit()
            except Exception as e:
                print(f"Error in reset_chat_user_stats: {e}")

    # ── WHISPER (WSPBDBot) EXTENSION ──
    def create_table_messages(self):
        """Create Messages table for whispers (compatible with old sqlite.py). Called by WSPBDBot."""
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    CREATE TABLE IF NOT EXISTS Messages (
                        message_id TEXT PRIMARY KEY,
                        message_text TEXT NOT NULL,
                        name TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        tg_username TEXT,
                        targets TEXT,
                        secret TEXT,
                        fake TEXT,
                        chat_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Migration for old DBs missing columns
                for col, typ in [("targets","TEXT"),("secret","TEXT"),("fake","TEXT"),("chat_id","TEXT"),("created_at","TIMESTAMP")]:
                    try:
                        c.execute(f"ALTER TABLE Messages ADD COLUMN {col} {typ}")
                    except:
                        pass
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON Messages(user_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON Messages(created_at)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_messages_targets ON Messages(targets)")
                try:
                    c.execute("UPDATE Messages SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
                except: pass
                self.conn.commit()
            except Exception as e:
                print(f"create_table_messages error: {e}")

    def add_message(self, message_id: str, message_text: str, name: str, user_id: str, tg_username: str = None, targets: str = None, secret: str = None, fake: str = None, chat_id: str = None):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    INSERT INTO Messages(message_id, message_text, name, user_id, tg_username, targets, secret, fake, chat_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (message_id, message_text, name, str(user_id), tg_username, targets, secret, fake, str(chat_id) if chat_id else None))
                self.conn.commit()
                # Also update users table for sender
                try:
                    self.ensure_user(str(user_id), name=name, username=tg_username)
                except: pass
            except Exception as e:
                print(f"add_message error: {e}")

    def select_message(self, **kwargs):
        if not self._check_conn():
            return None
        with self.lock:
            try:
                c = self.conn.cursor()
                if "message_id" in kwargs:
                    c.execute("SELECT * FROM Messages WHERE message_id=?", (kwargs["message_id"],))
                    return c.fetchone()
                # generic fallback
                where = " AND ".join([f"{k}=?" for k in kwargs])
                c.execute(f"SELECT * FROM Messages WHERE {where}", tuple(str(v) for v in kwargs.values()))
                return c.fetchone()
            except Exception as e:
                print(f"select_message error: {e}")
                return None

    def select_all_messages(self):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT * FROM Messages ORDER BY created_at DESC")
                return c.fetchall()
            except: return []

    def get_whispers(self, limit=20, offset=0, search: str = None):
        if not self._check_conn():
            return []
        with self.lock:
            try:
                c = self.conn.cursor()
                if search:
                    like = f"%{search}%"
                    c.execute("""
                        SELECT * FROM Messages WHERE targets LIKE ? OR secret LIKE ? OR message_text LIKE ? OR name LIKE ? OR tg_username LIKE ? OR user_id LIKE ?
                        ORDER BY COALESCE(created_at,'1970-01-01') DESC, rowid DESC LIMIT ? OFFSET ?
                    """, (like,like,like,like,like,like, limit, offset))
                else:
                    c.execute("SELECT * FROM Messages ORDER BY COALESCE(created_at,'1970-01-01') DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset))
                return c.fetchall()
            except Exception as e:
                print(f"get_whispers error: {e}")
                return []

    def count_whispers(self, search: str = None):
        if not self._check_conn():
            return (0,)
        with self.lock:
            try:
                c = self.conn.cursor()
                if search:
                    like = f"%{search}%"
                    c.execute("SELECT COUNT(*) FROM Messages WHERE targets LIKE ? OR secret LIKE ? OR message_text LIKE ? OR name LIKE ? OR tg_username LIKE ? OR user_id LIKE ?", (like,like,like,like,like,like))
                else:
                    c.execute("SELECT COUNT(*) FROM Messages")
                return c.fetchone()
            except: return (0,)

    def delete_whisper(self, message_id: str):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM Messages WHERE message_id=?", (message_id,))
                self.conn.commit()
            except: pass

    def count_users(self):
        try:
            return self.count_whispers()
        except: return (0,)

    def delete_users(self):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM Messages")
                self.conn.commit()
            except: pass

# Initialize database instance
# Use shared DB path: prefer data/manager.db (copied from Group-manager) else manager.db
import pathlib
_db_path = os.getenv("WHISPER_DB_PATH", "")
if not _db_path:
    for cand in ["data/manager.db", "data/main.db", "manager.db"]:
        if pathlib.Path(cand).exists():
            _db_path = cand
            break
    if not _db_path:
        _db_path = "data/manager.db"
db = Database(db_path=_db_path)
