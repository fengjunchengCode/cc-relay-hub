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
                "content": "pong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["notify"]["status"], "sent")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][0],
            [
                "cc-connect",
                "send",
                "-p",
                "codex-bot",
                "-s",
                "feishu:origin:u1",
                "--stdin",
            ],
        )
        self.assertIn("pong", calls[0][1])

        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")
        self.assertEqual(message["reply_body"], "pong")
        self.assertIsNotNone(message["notified_at"])
        self.assertFalse(self.store.has_active_lock("feishu:target:u1"))

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
            runner=runner,
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
