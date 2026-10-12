import json
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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS peers (
                        node_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        addrs TEXT NOT NULL DEFAULT '[]',
                        best_addr TEXT,
                        path TEXT,
                        rtt_ms REAL,
                        snapshot_version INTEGER NOT NULL DEFAULT 0,
                        added_at REAL NOT NULL,
                        last_seen_at REAL,
                        last_probe_at REAL,
                        last_direct_ok_at REAL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS remote_agents (
                        node_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        type TEXT,
                        provider TEXT,
                        groups TEXT NOT NULL DEFAULT '[]',
                        work_dir TEXT,
                        has_session INTEGER NOT NULL DEFAULT 0,
                        health TEXT,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (node_id, name)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS peer_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        envelope_id TEXT NOT NULL UNIQUE,
                        to_node TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        envelope TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        next_attempt_at REAL NOT NULL,
                        expires_at REAL,
                        last_attempt_at REAL,
                        delivered_at REAL,
                        last_error TEXT,
                        via TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS peer_outbox_pending_idx
                    ON peer_outbox (status, next_attempt_at, to_node, id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS peer_inbox_seen (
                        envelope_id TEXT PRIMARY KEY,
                        kind TEXT,
                        from_node TEXT,
                        received_at REAL NOT NULL,
                        result TEXT
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
            "direction": "TEXT",
            "origin_node": "TEXT",
            "target_node": "TEXT",
            "envelope_id": "TEXT",
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
        direction=None,
        origin_node=None,
        target_node=None,
        envelope_id=None,
    ):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        request_id, sender, target, session_key, provider, body, status,
                        origin_project, origin_session_key, created_at,
                        direction, origin_node, target_node, envelope_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _sanitize_text(direction),
                        _sanitize_text(origin_node),
                        _sanitize_text(target_node),
                        _sanitize_text(envelope_id),
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

    # ------------------------------------------------------------------
    # Cross-node: peers, remote agents, peer outbox, inbound dedupe
    # ------------------------------------------------------------------
    def upsert_peer(self, node_id, name=None, addrs=None, now=None, snapshot_version=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                row = conn.execute("SELECT * FROM peers WHERE node_id = ?", (node_id,)).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO peers (node_id, name, addrs, snapshot_version, added_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            node_id,
                            name or node_id,
                            json.dumps(addrs or []),
                            int(snapshot_version or 0),
                            now,
                            now,
                        ),
                    )
                else:
                    merged_addrs = json.loads(row["addrs"] or "[]")
                    if addrs is not None:
                        merged_addrs = list(addrs)
                    conn.execute(
                        """
                        UPDATE peers SET name = ?, addrs = ?, last_seen_at = ?,
                            snapshot_version = MAX(snapshot_version, ?)
                        WHERE node_id = ?
                        """,
                        (
                            name or row["name"],
                            json.dumps(merged_addrs),
                            now,
                            int(snapshot_version or 0),
                            node_id,
                        ),
                    )
                row = conn.execute("SELECT * FROM peers WHERE node_id = ?", (node_id,)).fetchone()
                return self._peer_row(row)
        finally:
            conn.close()

    def add_peer_addr(self, node_id, addr, name=None, now=None):
        peer = self.get_peer(node_id)
        addrs = list(peer["addrs"]) if peer else []
        if addr not in addrs:
            addrs.insert(0, addr)
        return self.upsert_peer(node_id, name=name or (peer or {}).get("name"), addrs=addrs, now=now)

    def set_peer_probe(self, node_id, best_addr, path, rtt_ms, ok, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                if ok:
                    conn.execute(
                        """
                        UPDATE peers SET best_addr = ?, path = ?, rtt_ms = ?, last_probe_at = ?,
                            last_direct_ok_at = ?
                        WHERE node_id = ?
                        """,
                        (best_addr, path, rtt_ms, now, now, node_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE peers SET best_addr = NULL, path = ?, rtt_ms = NULL, last_probe_at = ?
                        WHERE node_id = ?
                        """,
                        (path, now, node_id),
                    )
        finally:
            conn.close()

    def _peer_row(self, row):
        if row is None:
            return None
        item = dict(row)
        try:
            item["addrs"] = json.loads(item.get("addrs") or "[]")
        except ValueError:
            item["addrs"] = []
        return item

    def get_peer(self, node_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM peers WHERE node_id = ?", (node_id,)).fetchone()
            return self._peer_row(row)
        finally:
            conn.close()

    def find_peer_by_name(self, name):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM peers").fetchall()
            peers = [self._peer_row(row) for row in rows]
        finally:
            conn.close()
        exact = [p for p in peers if p["name"] == name or p["node_id"] == name]
        if exact:
            return exact[0]
        prefix = [p for p in peers if p["node_id"].startswith(name) or p["node_id"].startswith("n_" + name)]
        if len(prefix) == 1:
            return prefix[0]
        return None

    def list_peers(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM peers ORDER BY name, node_id").fetchall()
            return [self._peer_row(row) for row in rows]
        finally:
            conn.close()

    def remove_peer(self, node_id):
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
                conn.execute("DELETE FROM remote_agents WHERE node_id = ?", (node_id,))
        finally:
            conn.close()

    def replace_remote_agents(self, node_id, agents, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM remote_agents WHERE node_id = ?", (node_id,))
                for agent in agents or []:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO remote_agents (
                            node_id, name, type, provider, groups, work_dir, has_session, health, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            node_id,
                            _sanitize_text(agent.get("name")),
                            _sanitize_text(agent.get("type")),
                            _sanitize_text(agent.get("provider")),
                            json.dumps(agent.get("groups") or []),
                            _sanitize_text(agent.get("work_dir")),
                            1 if agent.get("has_session") else 0,
                            _sanitize_text(agent.get("health")),
                            now,
                        ),
                    )
        finally:
            conn.close()

    def list_remote_agents(self, node_id=None):
        conn = self._connect()
        try:
            if node_id:
                rows = conn.execute(
                    """
                    SELECT ra.*, p.name AS node_name, p.last_seen_at AS node_last_seen_at, p.path AS node_path
                    FROM remote_agents ra LEFT JOIN peers p ON p.node_id = ra.node_id
                    WHERE ra.node_id = ? ORDER BY ra.name
                    """,
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT ra.*, p.name AS node_name, p.last_seen_at AS node_last_seen_at, p.path AS node_path
                    FROM remote_agents ra LEFT JOIN peers p ON p.node_id = ra.node_id
                    ORDER BY p.name, ra.name
                    """
                ).fetchall()
        finally:
            conn.close()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["groups"] = json.loads(item.get("groups") or "[]")
            except ValueError:
                item["groups"] = []
            item["has_session"] = bool(item.get("has_session"))
            items.append(item)
        return items

    def enqueue_peer_envelope(self, envelope, now=None, ttl_secs=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO peer_outbox (
                        envelope_id, to_node, kind, envelope, status, attempts,
                        created_at, next_attempt_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        envelope["envelope_id"],
                        envelope["to_node"],
                        envelope["kind"],
                        json.dumps(envelope, ensure_ascii=False),
                        now,
                        now,
                        (now + float(ttl_secs)) if ttl_secs else None,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM peer_outbox WHERE envelope_id = ?", (envelope["envelope_id"],)
                ).fetchone()
                return self._outbox_row(row)
        finally:
            conn.close()

    def _outbox_row(self, row):
        if row is None:
            return None
        item = dict(row)
        try:
            item["envelope"] = json.loads(item["envelope"])
        except (ValueError, TypeError):
            item["envelope"] = None
        return item

    def claim_peer_outbox(self, to_node=None, due_at=None, limit=50):
        conn = self._connect()
        try:
            with conn:
                query = "SELECT * FROM peer_outbox WHERE status = 'pending'"
                params = []
                if to_node:
                    query += " AND to_node = ?"
                    params.append(to_node)
                if due_at is not None:
                    query += " AND next_attempt_at <= ?"
                    params.append(due_at)
                query += " ORDER BY id LIMIT ?"
                params.append(int(limit))
                rows = conn.execute(query, params).fetchall()
                return [self._outbox_row(row) for row in rows]
        finally:
            conn.close()

    def get_peer_outbox(self, envelope_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM peer_outbox WHERE envelope_id = ?", (envelope_id,)).fetchone()
            return self._outbox_row(row)
        finally:
            conn.close()

    def list_peer_outbox(self, status=None, limit=100):
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM peer_outbox WHERE status = ? ORDER BY id DESC LIMIT ?", (status, int(limit))
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM peer_outbox ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [self._outbox_row(row) for row in rows]
        finally:
            conn.close()

    def mark_peer_outbox_attempt(self, envelope_id, next_attempt_at, error=None, via=None, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE peer_outbox SET attempts = attempts + 1, last_attempt_at = ?,
                        next_attempt_at = ?, last_error = ?, via = COALESCE(?, via)
                    WHERE envelope_id = ? AND status = 'pending'
                    """,
                    (now, next_attempt_at, _sanitize_text(error), via, envelope_id),
                )
        finally:
            conn.close()

    def mark_peer_outbox_delivered(self, envelope_id, via=None, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE peer_outbox SET status = 'delivered', delivered_at = ?, last_error = NULL,
                        via = COALESCE(?, via)
                    WHERE envelope_id = ? AND status = 'pending'
                    """,
                    (now, via, envelope_id),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def mark_peer_outbox_status(self, envelope_id, status, error=None, now=None):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE peer_outbox SET status = ?, last_error = ?, last_attempt_at = ?
                    WHERE envelope_id = ?
                    """,
                    (status, _sanitize_text(error), now, envelope_id),
                )
        finally:
            conn.close()

    def record_inbound_envelope(self, envelope_id, kind=None, from_node=None, result=None, now=None):
        """Return True when the envelope is new, False when already seen."""
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO peer_inbox_seen (envelope_id, kind, from_node, received_at, result)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (envelope_id, kind, from_node, now, json.dumps(result) if result is not None else None),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def set_inbound_result(self, envelope_id, result):
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE peer_inbox_seen SET result = ? WHERE envelope_id = ?",
                    (json.dumps(result) if result is not None else None, envelope_id),
                )
        finally:
            conn.close()

    def get_inbound_result(self, envelope_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT result FROM peer_inbox_seen WHERE envelope_id = ?", (envelope_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["result"]:
            return None
        try:
            return json.loads(row["result"])
        except ValueError:
            return None

    def list_messages_by_direction(self, direction, status=None, limit=100):
        conn = self._connect()
        try:
            query = "SELECT * FROM messages WHERE direction = ?"
            params = [direction]
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
