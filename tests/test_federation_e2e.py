"""Two in-process node daemons + a local mini relay: discovery, relay delivery,
direct path, reply return, offline queueing, auth rejection."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hub  # noqa: E402
import relay_server  # noqa: E402
from core import node_identity  # noqa: E402
from core.rendezvous import NtfyRendezvous  # noqa: E402
from noded import NodeDaemon  # noqa: E402

import secrets as _secrets


def wait_until(predicate, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class FederationE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.relay = relay_server.serve("127.0.0.1", 0)
        cls.relay_port = cls.relay.server_address[1]
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = _secrets.token_hex(32)
        self.dir_a = Path(self.tmp.name) / "a"
        self.dir_b = Path(self.tmp.name) / "b"
        for hub_dir, name in ((self.dir_a, "mbp"), (self.dir_b, "office")):
            hub_dir.mkdir()
            node_identity.ensure_node(hub_dir, name=name, port=9130, rendezvous_url="http://127.0.0.1:%d" % self.relay_port)
            node_identity.save_secret(hub_dir, self.secret)
        self.delivered = []
        self.notified = []
        self.daemons = []
        self.a = self._daemon(self.dir_a, snapshot=[{"name": "claude-bot", "type": "claudecode", "provider": "cc_connect", "groups": ["team"], "has_session": True, "health": "idle"}])
        self.b = self._daemon(self.dir_b, snapshot=[{"name": "codex-bot", "type": "codex", "provider": "cc_connect", "groups": ["team"], "has_session": True, "health": "idle"}])

    def _daemon(self, hub_dir, snapshot, deliver_status="delivered"):
        def deliver(payload, origin_node, envelope_id):
            self.delivered.append((hub_dir.name, payload, origin_node))
            return {"status": deliver_status, "request_id": payload["request_id"], "delivered_at": time.time(), "error": None if deliver_status == "delivered" else "busy: session x"}

        def notify(message, reply_text, label=None):
            self.notified.append((hub_dir.name, message["request_id"], reply_text, label))
            return {"status": "sent"}

        daemon = NodeDaemon(
            str(hub_dir),
            deliver_fn=deliver,
            notify_fn=notify,
            snapshot_fn=lambda: snapshot,
            rendezvous=NtfyRendezvous("http://127.0.0.1:%d" % self.relay_port),
            enable_beacon=False,
            listen_host="127.0.0.1",
            listen_port=0,
            log=lambda msg: None,
        )
        daemon.start()
        self.daemons.append(daemon)
        return daemon

    def tearDown(self):
        for daemon in self.daemons:
            daemon.stop()
        self.tmp.cleanup()

    def _discover(self):
        self.a.publish_card()
        self.b.publish_card()
        self.assertTrue(wait_until(lambda: self.a.store.get_peer(self.b.node_id) and self.b.store.get_peer(self.a.node_id)))

    def test_discovery_via_relay_and_remote_agents(self):
        self._discover()
        remote = self.a.store.list_remote_agents()
        self.assertEqual([r["name"] for r in remote], ["codex-bot"])
        self.assertEqual(remote[0]["node_name"], "office")
        self.assertEqual(remote[0]["groups"], ["team"])

    def test_request_and_reply_via_relay(self):
        self._discover()
        code, result = self.a.api_send({
            "agent": "codex-bot", "node_id": self.b.node_id, "body": "reply with pong", "expect_reply": True,
            "origin_project": "claude-bot", "origin_session": "feishu:x:y", "sender": "claude-bot", "wait_secs": 15,
        })
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "delivered", result)
        self.assertEqual(result["via"], "relay")
        self.assertEqual(len(self.delivered), 1)
        name, payload, origin_node = self.delivered[0]
        self.assertEqual(name, "b")
        self.assertEqual(origin_node, self.a.node_id)
        self.assertEqual(payload["origin_label"], "claude-bot@mbp")
        request_id = result["request_id"]
        self.assertEqual(self.a.store.get_message(request_id)["status"], "delivered")

        # B's hook path forwards the agent reply back to A.
        message = {"request_id": request_id, "origin_node": self.a.node_id, "target": "codex-bot",
                   "origin_project": "claude-bot", "origin_session_key": "feishu:x:y"}
        with mock.patch.object(hub, "HUB_DIR", self.dir_b), mock.patch.object(hub, "NODE_JSON_PATH", self.dir_b / "node.json"):
            outcome = hub.forward_reply_to_origin_node(self.b.store, message, "pong")
        self.assertEqual(outcome["status"], "queued")
        self.b.kick_event.set()
        self.assertTrue(wait_until(lambda: self.notified))
        self.assertEqual(self.notified[0][1:], (request_id, "pong", None))
        self.assertEqual(self.a.store.get_message(request_id)["status"], "replied")
        self.assertEqual(self.a.store.get_message(request_id)["reply_body"], "pong")
        self.assertTrue(wait_until(lambda: self.b.store.get_peer_outbox(outcome["envelope_id"])["status"] == "delivered"))

    def test_direct_path_after_probe(self):
        self._discover()
        self.a.store.add_peer_addr(self.b.node_id, "127.0.0.1:%d" % self.b.port)
        best = self.a.probe_peer(self.a.store.get_peer(self.b.node_id))
        self.assertIsNotNone(best)
        self.assertEqual(self.a.choose_path(self.a.store.get_peer(self.b.node_id))[0], "direct")
        code, result = self.a.api_send({"agent": "codex-bot", "node_id": self.b.node_id, "body": "hi", "expect_reply": False, "wait_secs": 10})
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["via"], "lan")
        self.assertEqual(self.delivered[-1][1]["expect_reply"], False)

    def test_busy_target_reports_failure(self):
        self.b.stop()
        self.daemons.remove(self.b)
        self.b = self._daemon(self.dir_b, snapshot=[], deliver_status="busy")
        self._discover()
        code, result = self.a.api_send({"agent": "codex-bot", "node_id": self.b.node_id, "body": "hi", "wait_secs": 10})
        self.assertEqual(result["status"], "failed")
        self.assertIn("busy", result["error"])

    def test_offline_peer_queues_then_delivers(self):
        self._discover()
        self.b.stop()
        self.daemons.remove(self.b)
        code, result = self.a.api_send({"agent": "codex-bot", "node_id": self.b.node_id, "body": "later", "wait_secs": 1})
        self.assertEqual(result["status"], "queued")
        request_id = result["request_id"]
        self.b = self._daemon(self.dir_b, snapshot=[])
        self.assertTrue(wait_until(lambda: self.a.store.get_message(request_id)["status"] == "delivered", timeout=20))
        self.assertEqual(self.delivered[-1][1]["body"], "later")

    def test_duplicate_envelope_not_delivered_twice(self):
        self._discover()
        self.a.store.add_peer_addr(self.b.node_id, "127.0.0.1:%d" % self.b.port)
        self.a.probe_peer(self.a.store.get_peer(self.b.node_id))
        code, result = self.a.api_send({"agent": "codex-bot", "node_id": self.b.node_id, "body": "once", "wait_secs": 10})
        item = self.a.store.list_peer_outbox(limit=1)[0]
        responses = self.a.direct_post("127.0.0.1:%d" % self.b.port, "/peer/v1/inbox", item["envelope"], 10)
        self.assertEqual(responses[0]["kind"], "receipt")
        self.assertEqual(len([d for d in self.delivered if d[1]["body"] == "once"]), 1)

    def test_unauthenticated_peer_requests_rejected(self):
        for path in ("/peer/v1/probe", "/peer/v1/inbox"):
            request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.b.port, path), data=b"x" if path.endswith("inbox") else None)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 401)
        # wrong cluster secret -> 401 as well
        other = NodeDaemon(str(Path(self.tmp.name) / "c"), snapshot_fn=lambda: [], rendezvous=NtfyRendezvous("http://127.0.0.1:%d" % self.relay_port), enable_beacon=False, listen_port=0, log=lambda m: None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            other.direct_probe("127.0.0.1:%d" % self.b.port)
        self.assertEqual(ctx.exception.code, 401)

    def test_api_is_loopback_only_and_health(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/api/v1/health" % self.a.port, timeout=5) as response:
            health = json.loads(response.read())
        self.assertEqual(health["name"], "mbp")
        self.assertIn("peers", health)


if __name__ == "__main__":
    unittest.main()
