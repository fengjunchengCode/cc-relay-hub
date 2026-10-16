import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.state import StateStore


class HookUtf8Test(unittest.TestCase):
    def test_utf8_hook_survives_legacy_windows_stdio(self):
        bodies = [
            "pong",
            "中文调研报告：最新两条视频，已完成。✅𠮷\n|播放|1839|",
            ("中文报告，留存43.66%，完整正文不能乱码。✅\n" * 1000),
        ]
        for body in bodies:
            with self.subTest(chars=len(body)), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                # A closed local port prevents tests touching the live daemon.
                (root / "node.json").write_text(json.dumps({
                    "node_id": "test-local", "listen_port": 1,
                }), encoding="utf-8")
                store = StateStore(str(root / "state.db"))
                store.insert_message(
                    request_id="utf8-test", sender="remote", target="test-agent",
                    session_key="test-session", provider="cc_connect", body="test",
                    status="delivered", created_at=1.0, direction="inbound",
                    origin_node="test-remote", origin_project="remote-agent",
                    origin_session_key="remote-session",
                )
                payload = {
                    "event": "message.sent", "project": "test-agent",
                    "session_key": "test-session",
                    "content": "[cc-relay reply_to=utf8-test]\n" + body,
                }
                env = dict(os.environ, CC_RELAY_HUB_DIR=td,
                           PYTHONIOENCODING="gbk:surrogateescape", PYTHONUTF8="0")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "hub.py"), "_on_hook"],
                    input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    capture_output=True, env=env, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", "backslashreplace"))
                outcome = json.loads(result.stdout)
                self.assertEqual(outcome["notify"]["via"], "peer_outbox")
                self.assertEqual(store.get_message("utf8-test")["reply_body"], body)
                item = store.get_peer_outbox(outcome["notify"]["envelope_id"])
                self.assertEqual(item["envelope"]["payload"]["reply"], body)
                # Hook retries must not enqueue the same reply twice.
                retry = subprocess.run(
                    [sys.executable, str(ROOT / "hub.py"), "_on_hook"],
                    input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    capture_output=True, env=env, timeout=15,
                )
                self.assertEqual(retry.returncode, 0)
                self.assertEqual(json.loads(retry.stdout)["status"], "duplicate")
                self.assertEqual(len(store.list_peer_outbox(limit=10)), 1)


if __name__ == "__main__":
    unittest.main()
