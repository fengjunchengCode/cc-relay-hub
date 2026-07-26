import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import StateStore  # noqa: E402


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "state.db")
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_session_write_lock_blocks_second_pending_request(self):
        acquired = self.store.acquire_session_lock("feishu:s1:u1", "req-1", 30)
        self.assertTrue(acquired)

        blocked = self.store.acquire_session_lock("feishu:s1:u1", "req-2", 30)
        self.assertFalse(blocked)

    def test_session_write_lock_releases_after_explicit_release(self):
        self.assertTrue(self.store.acquire_session_lock("feishu:s1:u1", "req-1", 30))
        self.store.release_session_lock("feishu:s1:u1", "req-1")

        self.assertTrue(self.store.acquire_session_lock("feishu:s1:u1", "req-2", 30))

    def test_stale_session_lock_can_be_reclaimed(self):
        self.assertTrue(self.store.acquire_session_lock("feishu:s1:u1", "req-1", 0))
        time.sleep(0.01)

        self.assertTrue(self.store.acquire_session_lock("feishu:s1:u1", "req-2", 30))

    def test_message_lifecycle_updates_status_fields(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="codex-bot",
            session_key="feishu:s1:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=100.0,
        )
        self.store.mark_delivered("req-1", 101.0)
        self.store.mark_timeout("req-1", 130.0)

        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "timeout")
        self.assertEqual(message["delivered_at"], 101.0)
        self.assertEqual(message["replied_at"], 130.0)

    def test_message_persists_origin_and_notify_fields(self):
        self.store.insert_message(
            request_id="req-2",
            sender="hub",
            target="codex-bot",
            session_key="feishu:s1:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=100.0,
            origin_project="relay-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_notified("req-2", 105.0)

        message = self.store.get_message("req-2")
        self.assertEqual(message["origin_project"], "relay-bot")
        self.assertEqual(message["origin_session_key"], "feishu:origin:u1")
        self.assertEqual(message["notified_at"], 105.0)

    def test_text_fields_replace_invalid_surrogates_before_sqlite_write(self):
        self.store.insert_message(
            request_id="req-3",
            sender="hub",
            target="claude-bot",
            session_key="feishu:s1:u1",
            provider="cc_connect",
            body="bad\udcadbody",
            status="pending",
            created_at=100.0,
        )
        self.store.mark_replied("req-3", "reply\udc80body", 101.0)
        self.store.append_event(
            event_type="message.sent",
            agent_id="claude-bot",
            request_id="req-3",
            session_key="feishu:s1:u1",
            content="event\udcaecontent",
            timestamp=101.0,
        )

        message = self.store.get_message("req-3")
        self.assertEqual(message["body"], "bad?body")
        self.assertEqual(message["reply_body"], "reply?body")
        conn = sqlite3.connect(self.db_path)
        try:
            event = conn.execute("SELECT content FROM events WHERE request_id = ?", ("req-3",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(event[0], "event?content")

    def test_notification_outbox_is_fifo_and_deduplicated(self):
        first = self.store.enqueue_notification(
            dedupe_key="same",
            request_id="req-1",
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
            target="my-project",
            content="first",
            created_at=100.0,
        )
        duplicate = self.store.enqueue_notification(
            dedupe_key="same",
            request_id="req-1",
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
            target="my-project",
            content="first",
            created_at=101.0,
        )
        second = self.store.enqueue_notification(
            dedupe_key="other",
            request_id="req-1",
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
            target="my-project",
            content="follow-up",
            created_at=102.0,
        )

        self.assertEqual(first["id"], duplicate["id"])
        self.assertNotEqual(first["id"], second["id"])
        rows = self.store.list_notifications(status="pending")
        self.assertEqual([row["content"] for row in rows], ["first", "follow-up"])

    def test_notification_outbox_tracks_retry_and_delivery(self):
        item = self.store.enqueue_notification(
            dedupe_key="one",
            request_id="req-1",
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
            target="my-project",
            content="reply",
            created_at=100.0,
        )
        self.store.mark_notification_failed(
            item["id"],
            "session busy",
            attempted_at=101.0,
            next_attempt_at=103.0,
        )

        pending = self.store.get_notification(item["id"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["last_error"], "session busy")
        self.assertEqual(
            self.store.list_notifications(status="pending", due_at=102.0),
            [],
        )

        self.store.mark_notification_delivered(item["id"], 104.0)
        delivered = self.store.get_notification(item["id"])
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["attempts"], 2)
        self.assertEqual(delivered["delivered_at"], 104.0)
        self.assertIsNone(delivered["last_error"])

    def test_notification_claim_prevents_parallel_drains(self):
        item = self.store.enqueue_notification(
            dedupe_key="claim-once",
            request_id="req-1",
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
            target="my-project",
            content="reply",
            created_at=100.0,
        )

        first_claim = self.store.claim_notifications(due_at=100.0)
        second_claim = self.store.claim_notifications(due_at=100.0)

        self.assertEqual([row["id"] for row in first_claim], [item["id"]])
        self.assertEqual(second_claim, [])
        self.store.release_notification_claim(item["id"])
        self.assertEqual(
            [row["id"] for row in self.store.claim_notifications(due_at=100.0)],
            [item["id"]],
        )


if __name__ == "__main__":
    unittest.main()
