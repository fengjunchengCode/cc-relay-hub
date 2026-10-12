import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hub  # noqa: E402
from core.state import StateStore  # noqa: E402


REGISTRY = {
    "version": 2,
    "agents": {
        "claude-bot": {"type": "claudecode", "provider": "cc_connect", "work_dir": "/w"},
        "codex-bot": {"type": "codex", "provider": "cc_connect", "work_dir": "/w"},
    },
    "groups": {"team": {"members": ["claude-bot", "codex-bot"]}},
}
BINDINGS = {
    "cc_connect": {
        "claude-bot": {"session_key": "feishu:a:b", "webhook_port": 9110},
        "codex-bot": {"session_key": "feishu:c:d", "webhook_port": 9112},
    },
    "cdp": {},
}


class NodeResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hub_dir = Path(self.tmp.name)
        (hub_dir / "node.json").write_text(json.dumps({"node_id": "n_local", "name": "mbp", "listen_port": 9130}))
        self.store = StateStore(str(hub_dir / "state.db"))
        self.store.upsert_peer("n_office", name="office", addrs=["10.0.0.9:9130"])
        self.store.replace_remote_agents("n_office", [
            {"name": "codex-bot", "type": "codex", "provider": "cc_connect", "groups": ["team"], "has_session": True, "health": "idle"},
            {"name": "gemini-bot", "type": "gemini", "provider": "cc_connect", "groups": ["other"], "has_session": True, "health": "idle"},
        ])
        self.patches = [
            mock.patch.object(hub, "HUB_DIR", hub_dir),
            mock.patch.object(hub, "NODE_JSON_PATH", hub_dir / "node.json"),
            mock.patch.object(hub, "STATE_DB_PATH", hub_dir / "state.db"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_parse_address(self):
        self.assertEqual(hub.parse_agent_address("codex-bot@office"), ("codex-bot", "office"))
        self.assertEqual(hub.parse_agent_address("codex-bot"), ("codex-bot", None))

    def test_local_wins_for_bare_name(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot", sender="claude-bot")
        self.assertFalse(agent.get("remote"))
        self.assertEqual(agent["binding"]["webhook_port"], 9112)

    def test_explicit_node_suffix(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@office", sender="claude-bot")
        self.assertTrue(agent["remote"])
        self.assertEqual(agent["node_id"], "n_office")
        self.assertEqual(hub.remote_agent_label(agent), "codex-bot@office")

    def test_node_id_prefix_suffix(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@n_off")
        self.assertEqual(agent["node_name"], "office")

    def test_remote_fallback_when_no_local_match(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "gemini-bot", sender="claude-bot")
        self.assertTrue(agent["remote"])
        self.assertEqual(agent["groups"], ["other"])

    def test_remote_fuzzy_by_type(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "gemini")
        self.assertEqual(agent["name"], "gemini-bot")

    def test_unknown_everywhere(self):
        with self.assertRaises(KeyError):
            hub.resolve_agent(REGISTRY, BINDINGS, "nobody")
        with self.assertRaises(KeyError):
            hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@nowhere")

    def test_local_alias(self):
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@local")
        self.assertFalse(agent.get("remote"))

    def test_group_compatibility_entry(self):
        remote = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@office")
        self.assertTrue(hub.check_group_compatibility_entry(REGISTRY, "claude-bot", remote))
        other = hub.resolve_agent(REGISTRY, BINDINGS, "gemini-bot@office")
        self.assertFalse(hub.check_group_compatibility_entry(REGISTRY, "claude-bot", other))

    def test_remote_listed_as_offline_when_stale(self):
        self.store.upsert_peer("n_office", now=time.time() - 3600)
        agent = hub.resolve_agent(REGISTRY, BINDINGS, "codex-bot@office")
        self.assertFalse(agent["online"])


if __name__ == "__main__":
    unittest.main()
