import sqlite3
import time


def _sanitize_text(value):
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _sanitize_params(params):
    return tuple(_sanitize_text(value) for value in params)


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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notification_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        request_id TEXT,
                        origin_project TEXT NOT NULL,
                        origin_session_key TEXT NOT NULL,
                        target TEXT NOT NULL,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        next_attempt_at REAL NOT NULL,
                        last_attempt_at REAL,
                        delivered_at REAL,
                        last_error TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS notification_outbox_pending_idx
                    ON notification_outbox (
                        status, next_attempt_at, origin_project, origin_session_key, id
                    )
                    """
                )
                self._ensure_message_columns(conn)
                self._ensure_notification_columns(conn)
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

    def _ensure_notification_columns(self, conn):
        columns = set()
        for row in conn.execute("PRAGMA table_info(notification_outbox)").fetchall():
            columns.add(row["name"])

        additions = {
            "last_attempt_idle_token": "TEXT",
        }
        for name, column_type in additions.items():
            if name in columns:
                continue
            conn.execute(
                "ALTER TABLE notification_outbox ADD COLUMN %s %s"
                % (name, column_type)
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
                        _sanitize_text(request_id),
                        _sanitize_text(sender),
                        _sanitize_text(target),
                        _sanitize_text(session_key),
                        _sanitize_text(provider),
                        _sanitize_text(body),
                        _sanitize_text(status),
                        _sanitize_text(origin_project),
                        _sanitize_text(origin_session_key),
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
                conn.execute(sql, _sanitize_params(params))
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
                    _sanitize_params((event_type, agent_id, request_id, session_key, content, timestamp)),
                )
        finally:
            conn.close()

    def enqueue_notification(
        self,
        dedupe_key,
        request_id,
        origin_project,
        origin_session_key,
        target,
        content,
        created_at,
    ):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox (
                        dedupe_key, request_id, origin_project, origin_session_key,
                        target, content, status, attempts, created_at, next_attempt_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    _sanitize_params((
                        dedupe_key,
                        request_id,
                        origin_project,
                        origin_session_key,
                        target,
                        content,
                        created_at,
                        created_at,
                    )),
                )
                row = conn.execute(
                    "SELECT * FROM notification_outbox WHERE dedupe_key = ?",
                    (_sanitize_text(dedupe_key),),
                ).fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def get_notification(self, notification_id):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?",
                (notification_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_notifications(
        self,
        status=None,
        origin_project=None,
        origin_session_key=None,
        due_at=None,
        limit=100,
    ):
        clauses = []
        params = []
        if status == "open":
            clauses.append("status IN ('pending', 'sending')")
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if origin_project:
            clauses.append("origin_project = ?")
            params.append(origin_project)
        if origin_session_key:
            clauses.append("origin_session_key = ?")
            params.append(origin_session_key)
        if due_at is not None:
            clauses.append("next_attempt_at <= ?")
            params.append(due_at)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM notification_outbox
                %s
                ORDER BY id ASC
                LIMIT ?
                """ % where,
                _sanitize_params(tuple(params)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def claim_notifications(
        self,
        origin_project=None,
        origin_session_key=None,
        due_at=None,
        limit=100,
        stale_after_secs=30.0,
    ):
        now = time.time() if due_at is None else due_at
        clauses = ["status = 'pending'"]
        params = []
        if origin_project:
            clauses.append("origin_project = ?")
            params.append(origin_project)
        if origin_session_key:
            clauses.append("origin_session_key = ?")
            params.append(origin_session_key)
        if due_at is not None:
            clauses.append("next_attempt_at <= ?")
            params.append(due_at)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'pending'
                WHERE status = 'sending'
                  AND (last_attempt_at IS NULL OR last_attempt_at <= ?)
                """,
                (now - max(float(stale_after_secs), 0.0),),
            )
            rows = conn.execute(
                """
                SELECT * FROM notification_outbox
                WHERE %s
                ORDER BY id ASC
                LIMIT ?
                """ % " AND ".join(clauses),
                _sanitize_params(tuple(params + [int(limit)])),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for notification_id in ids:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'sending', last_attempt_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now, notification_id),
                )
            conn.commit()
            return [self.get_notification(notification_id) for notification_id in ids]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_notification_claim(self, notification_id):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'pending'
                    WHERE id = ? AND status = 'sending'
                    """,
                    (notification_id,),
                )
        finally:
            conn.close()

    def mark_notification_delivered(
        self,
        notification_id,
        delivered_at,
        idle_token=None,
        count_attempt=True,
    ):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'delivered', delivered_at = ?, last_attempt_at = ?,
                        attempts = attempts + ?, last_error = NULL,
                        last_attempt_idle_token = ?
                    WHERE id = ?
                    """,
                    _sanitize_params((
                        delivered_at,
                        delivered_at,
                        1 if count_attempt else 0,
                        idle_token,
                        notification_id,
                    )),
                )
        finally:
            conn.close()

    def mark_notification_failed(
        self,
        notification_id,
        error,
        attempted_at,
        next_attempt_at,
        idle_token=None,
    ):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'pending', attempts = attempts + 1,
                        last_attempt_at = ?, next_attempt_at = ?, last_error = ?,
                        last_attempt_idle_token = ?
                    WHERE id = ?
                    """,
                    _sanitize_params((
                        attempted_at,
                        next_attempt_at,
                        error,
                        idle_token,
                        notification_id,
                    )),
                )
        finally:
            conn.close()

    def list_open_messages(self, max_age_secs=None, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            if max_age_secs is None:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE status IN ('pending', 'delivered')
                    ORDER BY created_at ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE status IN ('pending', 'delivered') AND created_at >= ?
                    ORDER BY created_at ASC
                    """,
                    (now - float(max_age_secs),),
                ).fetchall()
            return [dict(row) for row in rows]
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
