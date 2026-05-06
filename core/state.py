import sqlite3
import time


class StateStore(object):
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        request_id TEXT PRIMARY KEY,
                        sender TEXT NOT NULL,
                        target TEXT NOT NULL,
                        session_key TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        body TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        origin_project TEXT,
                        origin_session_key TEXT,
                        created_at REAL NOT NULL,
                        delivered_at REAL,
                        replied_at REAL,
                        reply_body TEXT,
                        notified_at REAL,
                        notify_error TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        request_id TEXT,
                        session_key TEXT,
                        content TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_locks (
                        session_key TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        locked_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                self._ensure_message_columns(conn)
        finally:
            conn.close()

    def _ensure_message_columns(self, conn):
        columns = set()
        for row in conn.execute("PRAGMA table_info(messages)").fetchall():
            columns.add(row["name"])

        additions = {
            "origin_project": "TEXT",
            "origin_session_key": "TEXT",
            "notified_at": "REAL",
            "notify_error": "TEXT",
        }
        for name, column_type in additions.items():
            if name in columns:
                continue
            conn.execute(
                "ALTER TABLE messages ADD COLUMN %s %s" % (name, column_type)
            )

    def insert_message(
        self,
        request_id,
        sender,
        target,
        session_key,
        provider,
        body,
        status,
        created_at,
        origin_project=None,
        origin_session_key=None,
    ):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        request_id, sender, target, session_key, provider, body, status,
                        origin_project, origin_session_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        sender,
                        target,
                        session_key,
                        provider,
                        body,
                        status,
                        origin_project,
                        origin_session_key,
                        created_at,
                    ),
                )
        finally:
            conn.close()

    def mark_delivered(self, request_id, delivered_at):
        self._update_message(
            request_id,
            "UPDATE messages SET status = ?, delivered_at = ? WHERE request_id = ?",
            ("delivered", delivered_at, request_id),
        )

    def mark_replied(self, request_id, reply_body, replied_at):
        self._update_message(
            request_id,
            "UPDATE messages SET status = ?, reply_body = ?, replied_at = ? WHERE request_id = ?",
            ("replied", reply_body, replied_at, request_id),
        )

    def mark_timeout(self, request_id, replied_at):
        self._update_message(
            request_id,
            "UPDATE messages SET status = ?, replied_at = ? WHERE request_id = ?",
            ("timeout", replied_at, request_id),
        )

    def mark_failed(self, request_id, error):
        self._update_message(
            request_id,
            "UPDATE messages SET status = ?, error = ? WHERE request_id = ?",
            ("failed", error, request_id),
        )

    def mark_notified(self, request_id, notified_at):
        self._update_message(
            request_id,
            "UPDATE messages SET notified_at = ?, notify_error = NULL WHERE request_id = ?",
            (notified_at, request_id),
        )

    def mark_notify_failed(self, request_id, error):
        self._update_message(
            request_id,
            "UPDATE messages SET notify_error = ? WHERE request_id = ?",
            (error, request_id),
        )

    def _update_message(self, request_id, sql, params):
        conn = self._connect()
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def get_message(self, request_id):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM messages WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_recent_messages(self, limit):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def append_event(self, event_type, agent_id, request_id, session_key, content, timestamp):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO events (
                        event_type, agent_id, request_id, session_key, content, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_type, agent_id, request_id, session_key, content, timestamp),
                )
        finally:
            conn.close()

    def find_latest_open_message_by_session(self, session_key):
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_key = ? AND status IN ('pending', 'delivered')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def acquire_session_lock(self, session_key, request_id, ttl_secs):
        now = time.time()
        expires_at = now + max(float(ttl_secs), 0.0)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM session_locks WHERE expires_at <= ?",
                    (now,),
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO session_locks (
                            session_key, request_id, locked_at, expires_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (session_key, request_id, now, expires_at),
                    )
                except sqlite3.IntegrityError:
                    return False
            return True
        finally:
            conn.close()

    def has_active_lock(self, session_key):
        return self.get_active_lock(session_key) is not None

    def get_active_lock(self, session_key):
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM session_locks WHERE expires_at <= ?",
                    (now,),
                )
                row = conn.execute(
                    "SELECT * FROM session_locks WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def release_session_lock(self, session_key, request_id=None):
        conn = self._connect()
        try:
            with conn:
                if request_id is None:
                    conn.execute(
                        "DELETE FROM session_locks WHERE session_key = ?",
                        (session_key,),
                    )
                else:
                    conn.execute(
                        "DELETE FROM session_locks WHERE session_key = ? AND request_id = ?",
                        (session_key, request_id),
                    )
        finally:
            conn.close()
