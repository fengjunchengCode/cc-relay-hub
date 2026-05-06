import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.envelope import RelayEvent  # noqa: E402
from core.match import wait_for_reply_framework  # noqa: E402
from core.state import StateStore  # noqa: E402


class StaticProvider(object):
    def __init__(self, events):
        self._events = events

    def poll_events(self, cursor=None):
        return list(self._events)


class MatchFrameworkTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.tmpdir.name) / "state.db"))
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="codex-bot",
            session_key="feishu:s1:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.acquire_session_lock("feishu:s1:u1", "req-1", 1)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_wait_framework_records_reply_when_single_event_arrives(self):
        provider = StaticProvider([
            RelayEvent(
                event_type="message.reply",
                request_id=None,
                agent_id="codex-bot",
                content="pong",
                timestamp=12.0,
            )
        ])

        reply = wait_for_reply_framework(
            store=self.store,
            provider=provider,
            request_id="req-1",
            session_key="feishu:s1:u1",
            timeout_secs=1,
            poll_interval=0.01,
        )

        self.assertEqual(reply, "pong")
        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")
        self.assertEqual(message["reply_body"], "pong")

    def test_wait_framework_times_out_and_releases_lock(self):
        provider = StaticProvider([])

        reply = wait_for_reply_framework(
            store=self.store,
            provider=provider,
            request_id="req-1",
            session_key="feishu:s1:u1",
            timeout_secs=0.05,
            poll_interval=0.01,
        )

        self.assertIsNone(reply)
        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "timeout")
        self.assertTrue(self.store.acquire_session_lock("feishu:s1:u1", "req-2", 1))


if __name__ == "__main__":
    unittest.main()
