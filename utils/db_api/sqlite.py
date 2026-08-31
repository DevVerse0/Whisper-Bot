

import sqlite3


class Database:
    def __init__(self, path_to_db="main.db"):
        self.path_to_db = path_to_db

    @property
    def connection(self):
        return sqlite3.connect(self.path_to_db)

    def execute(self, sql: str, parameters: tuple = None, fetchone=False, fetchall=False, commit=False):
        if not parameters:
            parameters = ()
        connection = self.connection
        connection.set_trace_callback(logger)
        cursor = connection.cursor()
        data = None
        cursor.execute(sql, parameters)

        if commit:
            connection.commit()
        if fetchall:
            data = cursor.fetchall()
        if fetchone:
            data = cursor.fetchone()
        connection.close()
        return data

    def create_table_messages(self):
        sql = """
        CREATE TABLE Messages (
            message_id varchar(50) NOT NULL,
            message_text varchar(2000) NOT NULL,
            name varchar(255) NOT NULL,
            user_id varchar(20) NOT NULL,
            tg_username varchar(50),
            targets TEXT,
            secret TEXT,
            fake TEXT,
            chat_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id)
            );
"""
        try:
            self.execute(sql, commit=True)
        except Exception:
            pass
        # Migration for existing DB: add missing columns if table already exists
        for col_def in [
            ("targets", "TEXT"),
            ("secret", "TEXT"),
            ("fake", "TEXT"),
            ("chat_id", "TEXT"),
            ("created_at", "TIMESTAMP"),
        ]:
            try:
                self.execute(f"ALTER TABLE Messages ADD COLUMN {col_def[0]} {col_def[1]}", commit=True)
            except Exception:
                pass
        try:
            self.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON Messages(user_id)", commit=True)
            self.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON Messages(created_at)", commit=True)
        except Exception:
            pass
        # Ensure existing rows have created_at
        try:
            self.execute("UPDATE Messages SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL", commit=True)
        except Exception:
            pass

    @staticmethod
    def format_args(sql, parameters: dict):
        sql += " AND ".join([
            f"{item} = ?" for item in parameters
        ])
        return sql, tuple(parameters.values())

    def add_message(self, message_id: str, message_text: str , name: str, user_id: str, tg_username: str = None,
                  targets: str = None, secret: str = None, fake: str = None, chat_id: str = None):
        # Extended for WSPBDBot: store parsed targets/secret/fake for dashboard
        sql = """
        INSERT INTO Messages(message_id, message_text, name, user_id, tg_username, targets, secret, fake, chat_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.execute(sql, parameters=(message_id, message_text, name, user_id, tg_username, targets, secret, fake, chat_id), commit=True)

    def select_all_messages(self):
        sql = """
        SELECT * FROM Messages
        """
        return self.execute(sql, fetchall=True)

    def select_message(self, **kwargs):
        # SQL_EXAMPLE = "SELECT * FROM Users where id=1 AND Name='John'"
        sql = "SELECT * FROM Messages WHERE "
        sql, parameters = self.format_args(sql, kwargs)

        return self.execute(sql, parameters=parameters, fetchone=True)

    def get_whispers(self, limit=20, offset=0, search: str = None):
        # Dashboard: paginated + search by target/secret/sender
        if search:
            like = f"%{search}%"
            sql = """
            SELECT * FROM Messages WHERE targets LIKE ? OR secret LIKE ? OR message_text LIKE ? OR name LIKE ? OR tg_username LIKE ? OR user_id LIKE ?
            ORDER BY COALESCE(created_at, '1970-01-01') DESC, rowid DESC LIMIT ? OFFSET ?
            """
            return self.execute(sql, parameters=(like, like, like, like, like, like, limit, offset), fetchall=True)
        sql = "SELECT * FROM Messages ORDER BY COALESCE(created_at, '1970-01-01') DESC, rowid DESC LIMIT ? OFFSET ?"
        return self.execute(sql, parameters=(limit, offset), fetchall=True)

    def count_whispers(self, search: str = None):
        if search:
            like = f"%{search}%"
            sql = "SELECT COUNT(*) FROM Messages WHERE targets LIKE ? OR secret LIKE ? OR message_text LIKE ? OR name LIKE ? OR tg_username LIKE ? OR user_id LIKE ?"
            return self.execute(sql, parameters=(like, like, like, like, like, like), fetchone=True)
        return self.execute("SELECT COUNT(*) FROM Messages;", fetchone=True)

    def count_users(self):
        return self.execute("SELECT COUNT(*) FROM Messages;", fetchone=True)

    def delete_users(self):
        self.execute("DELETE FROM Messages WHERE TRUE", commit=True)

    def delete_whisper(self, message_id: str):
        self.execute("DELETE FROM Messages WHERE message_id = ?", parameters=(message_id,), commit=True)


def logger(statement):
    # Disabled in production to avoid leaking whisper secrets to stdout
    # Enable by setting env DEBUG_SQL=1
    import os
    if os.getenv("DEBUG_SQL") == "1":
        print(f"""
_____________________________________________________        
Executing: 
{statement}
_____________________________________________________
""")