import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402
from core.state import StateStore  # noqa: E402


class CompletedProcessStub(object):
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


class HookHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_handle_hook_event_marks_reply_and_notifies_origin(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.acquire_session_lock("feishu:target:u1", "req-1", 30)

        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                    }
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["notify"]["status"], "queued")
        self.assertEqual(calls, [])

        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")
        self.assertEqual(message["reply_body"], "pong")
        self.assertIsNone(message["notified_at"])
        self.assertFalse(self.store.has_active_lock("feishu:target:u1"))
        queued = self.store.list_notifications(status="pending")
        self.assertEqual(len(queued), 1)
        self.assertIn("pong", queued[0]["content"])

    def test_handle_hook_event_without_relay_marker_does_not_match_pending_lock(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.acquire_session_lock("feishu:target:u1", "req-1", 30)

        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "direct user reply",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={"cc_connect": {}},
            runner=runner,
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(calls, [])
        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "delivered")
        self.assertTrue(self.store.has_active_lock("feishu:target:u1"))

    def test_handle_hook_event_rejects_empty_relay_reply_without_forwarding(self):
        self.store.insert_message(
            request_id="req-empty",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-empty", 11.0)
        self.store.acquire_session_lock("feishu:target:u1", "req-empty", 30)

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-empty]\n \t",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={"cc_connect": {}},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "empty_relay_reply")
        message = self.store.get_message("req-empty")
        self.assertEqual(message["status"], "delivered")
        self.assertTrue(self.store.has_active_lock("feishu:target:u1"))
        self.assertEqual(self.store.list_notifications(status="open"), [])

    def test_handle_hook_event_forwards_followup_reply_on_same_request_id(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.mark_replied("req-1", "on it, rendering now", 12.0)

        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\nrender finished: 10275 frames",
                "timestamp": "2026-05-06T11:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                    }
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "followup")
        self.assertEqual(result["notify"]["status"], "queued")
        self.assertEqual(calls, [])
        queued = self.store.list_notifications(status="pending")
        self.assertEqual(len(queued), 1)
        self.assertIn("render finished", queued[0]["content"])
        self.assertIn("follow-up", queued[0]["content"])

        message = self.store.get_message("req-1")
        self.assertEqual(message["reply_body"], "render finished: 10275 frames")

    def test_handle_hook_event_forwards_late_reply_after_timeout(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.mark_timeout("req-1", 300.0)

        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\nlate but done",
                "timestamp": "2026-05-06T11:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                    }
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "followup")
        self.assertEqual(calls, [])
        self.assertEqual(len(self.store.list_notifications(status="pending")), 1)
        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")

    def test_handle_hook_event_suppresses_duplicate_of_stored_reply(self):
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.mark_replied("req-1", "pong", 12.0)

        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T11:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={"cc_connect": {}},
            runner=runner,
        )

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(calls, [])

    def test_handle_hook_event_without_match_does_not_notify(self):
        calls = []

        def runner(command, input_text):
            calls.append((command, input_text))
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:missing:u1",
                "content": "pong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={"cc_connect": {}},
            runner=runner,
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
