import os
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


if __name__ == "__main__":
    unittest.main()
