import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import require_sandbox
require_sandbox()

import hub  # noqa: E402
from core.envelope import RelayEvent  # noqa: E402
from core.state import StateStore  # noqa: E402


class CompletedProcessStub(object):
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


class ProviderStub(object):
    def __init__(self, agent_id, binding, events=None, error=None):
        self.agent_id = agent_id
        self.binding = binding
        self.events = events or []
        self.error = error
        self.cursors = []

    def poll_events(self, cursor=None):
        self.cursors.append(cursor)
        if self.error:
            raise self.error
        return list(self.events)


def make_factory(events_by_agent, errors_by_agent=None):
    created = {}

    def factory(agent_id, binding):
        provider = ProviderStub(
            agent_id,
            binding,
            events=events_by_agent.get(agent_id, []),
            error=(errors_by_agent or {}).get(agent_id),
        )
        created[agent_id] = provider
        return provider

    return factory, created


BINDINGS = {
    "cc_connect": {
        "claude-bot": {"config_path": "/tmp/config-claude.toml"},
        "my-project": {"config_path": "/tmp/config-my.toml"},
    }
}


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.store = StateStore(self.db_path)
        self.calls = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def runner(self, command, input_text):
        self.calls.append((command, input_text))
        return CompletedProcessStub()

    def insert_delivered(self, request_id="req-1", delivered_at=100.0):
        self.store.insert_message(
            request_id=request_id,
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="render the film",
            status="pending",
            created_at=delivered_at - 1.0,
            origin_project="my-project",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered(request_id, delivered_at)

    def test_recovers_unsolicited_reply_from_session_history(self):
        self.insert_delivered()
        event = RelayEvent(
            event_type="message.reply",
            request_id=None,
            agent_id="claude-bot",
            content="[cc-relay reply_to=req-1]\nrender done, 10275 frames",
            timestamp=900.0,
            session_key="feishu:target:u1",
        )
        factory, created = make_factory({"claude-bot": [event]})

        results = hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "recovered")
        self.assertEqual(results[0]["request_id"], "req-1")
        self.assertEqual(results[0]["notify"]["status"], "queued")

        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")
        self.assertEqual(message["reply_body"], "render done, 10275 frames")
        self.assertIsNone(message["notified_at"])

        # Cursor anchored at delivered_at so pre-dispatch turns are ignored.
        self.assertEqual(created["claude-bot"].cursors, ["100.0"])
        self.assertEqual(self.calls, [])
        queued = self.store.list_notifications(status="pending")
        self.assertEqual(len(queued), 1)
        self.assertIn("recovered", queued[0]["content"])
        self.assertIn("render done", queued[0]["content"])

    def test_skips_requests_younger_than_min_age(self):
        self.insert_delivered(delivered_at=990.0)
        event = RelayEvent(
            event_type="message.reply",
            request_id=None,
            agent_id="claude-bot",
            content="[cc-relay reply_to=req-1]\ntoo fresh",
            timestamp=995.0,
            session_key="feishu:target:u1",
        )
        factory, created = make_factory({"claude-bot": [event]})

        results = hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )

        self.assertEqual(results, [])
        self.assertNotIn("claude-bot", created)
        self.assertEqual(self.store.get_message("req-1")["status"], "delivered")

    def test_ignores_history_without_matching_marker(self):
        self.insert_delivered()
        noise = [
            RelayEvent(
                event_type="message.reply",
                request_id=None,
                agent_id="claude-bot",
                content="progress: encoding 40%",
                timestamp=500.0,
                session_key="feishu:target:u1",
            ),
            RelayEvent(
                event_type="message.reply",
                request_id=None,
                agent_id="claude-bot",
                content="[cc-relay reply_to=other-id]\nanswer for someone else",
                timestamp=600.0,
                session_key="feishu:target:u1",
            ),
        ]
        factory, _ = make_factory({"claude-bot": noise})

        results = hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )

        self.assertEqual(results, [])
        self.assertEqual(self.store.get_message("req-1")["status"], "delivered")
        self.assertEqual(self.calls, [])

    def test_poll_failure_degrades_without_stopping_other_messages(self):
        self.insert_delivered(request_id="req-broken")
        self.store.insert_message(
            request_id="req-ok",
            sender="hub",
            target="my-project",
            session_key="feishu:other:u1",
            provider="cc_connect",
            body="status?",
            status="pending",
            created_at=99.0,
            origin_project="claude-bot",
            origin_session_key="feishu:origin2:u1",
        )
        self.store.mark_delivered("req-ok", 100.0)

        good = RelayEvent(
            event_type="message.reply",
            request_id=None,
            agent_id="my-project",
            content="[cc-relay reply_to=req-ok]\nall green",
            timestamp=700.0,
            session_key="feishu:other:u1",
        )
        factory, _ = make_factory(
            {"my-project": [good]},
            errors_by_agent={"claude-bot": ValueError("bad session json")},
        )

        results = hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )

        statuses = {item["request_id"]: item["status"] for item in results}
        self.assertEqual(statuses["req-broken"], "error")
        self.assertEqual(statuses["req-ok"], "recovered")
        self.assertEqual(self.store.get_message("req-broken")["status"], "delivered")
        self.assertEqual(self.store.get_message("req-ok")["status"], "replied")

    def test_hook_after_reconcile_is_suppressed_as_duplicate(self):
        self.insert_delivered()
        event = RelayEvent(
            event_type="message.reply",
            request_id=None,
            agent_id="claude-bot",
            content="[cc-relay reply_to=req-1]\nrender done",
            timestamp=900.0,
            session_key="feishu:target:u1",
        )
        factory, _ = make_factory({"claude-bot": [event]})
        hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )
        notify_calls_after_sweep = len(self.calls)

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\nrender done",
                "timestamp": "2026-05-06T11:00:00+08:00",
            },
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
        )

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(self.calls), notify_calls_after_sweep)

    def test_released_session_lock_after_recovery(self):
        self.insert_delivered()
        self.store.acquire_session_lock("feishu:target:u1", "req-1", 3600)
        event = RelayEvent(
            event_type="message.reply",
            request_id=None,
            agent_id="claude-bot",
            content="[cc-relay reply_to=req-1]\ndone",
            timestamp=900.0,
            session_key="feishu:target:u1",
        )
        factory, _ = make_factory({"claude-bot": [event]})

        hub.reconcile_messages(
            state_path=self.db_path,
            bindings=BINDINGS,
            runner=self.runner,
            min_age_secs=30,
            now=1000.0,
            provider_factory=factory,
        )

        self.assertFalse(self.store.has_active_lock("feishu:target:u1"))


if __name__ == "__main__":
    unittest.main()
