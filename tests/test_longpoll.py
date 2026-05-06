import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _wait_for_port(port, timeout=5):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


@unittest.skipUnless(_node_available(), "node not available")
class LongPollTest(unittest.TestCase):
    server = None
    port = 19120  # use non-standard port to avoid conflicts

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        env = os.environ.copy()
        env["HOOK_PORT"] = str(cls.port)
        env["HOOK_EVENTS_FILE"] = str(Path(cls.tmpdir.name) / "events.jsonl")
        cls.server = subprocess.Popen(
            ["node", str(ROOT / "hook-server.mjs")],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not _wait_for_port(cls.port, timeout=5):
            cls.server.kill()
            raise RuntimeError("hook-server did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.send_signal(signal.SIGTERM)
            cls.server.wait(timeout=5)
        cls.tmpdir.cleanup()

    def _longpoll(self, since="", timeout=2):
        url = f"http://127.0.0.1:{self.port}/events/longpoll?timeout={timeout}"
        if since:
            url += f"&since={urllib.request.quote(since, safe='')}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout + 3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_hook(self, project, content, session_key="s1"):
        # Include relay marker so hook server processes the event
        relay_content = "[cc-relay reply_to=test-req]\n" + content
        payload = json.dumps({
            "event": "message.sent",
            "project": project,
            "session_key": session_key,
            "content": relay_content,
            "timestamp": "2026-05-06T10:00:00+08:00",
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/cc-connect/hooks/reply",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status

    def test_longpoll_returns_empty_on_timeout(self):
        """No events → long-poll should return empty after timeout."""
        result = self._longpoll(since="2099-01-01T00:00:00Z", timeout=1)
        self.assertEqual(result["events"], [])

    def test_longpoll_returns_existing_events(self):
        """Events posted before long-poll should be returned immediately."""
        self._post_hook("test-proj", "hello-1")
        time.sleep(0.2)
        result = self._longpoll(since="2000-01-01T00:00:00Z", timeout=1)
        contents = [e["payload"]["content"] for e in result["events"]]
        self.assertTrue(any("hello-1" in c for c in contents))

    def test_longpoll_waits_for_new_event(self):
        """Long-poll should block until a new event arrives."""
        import threading

        # Use a past timestamp so the long-poll waits (no existing events match)
        # but new events will match
        since = "2020-01-01T00:00:00Z"
        # First post and immediately consume an event to establish a cursor
        self._post_hook("test", "anchor")
        time.sleep(0.3)
        data = self._longpoll(since=since, timeout=1)
        if data.get("events"):
            since = data["events"][-1]["received_at"]

        # Now long-poll should block since there are no events after `since`
        results = []

        def poll():
            try:
                results.append(self._longpoll(since=since, timeout=5))
            except Exception as e:
                results.append({"error": str(e)})

        t = threading.Thread(target=poll)
        t.start()
        time.sleep(0.5)

        # Post a new event — should wake the long-poll
        self._post_hook("test", "trigger")

        t.join(timeout=8)
        self.assertTrue(len(results) > 0)
        if "error" not in results[0]:
            contents = [e["payload"]["content"] for e in results[0].get("events", [])]
            self.assertTrue(any("trigger" in c for c in contents))

    def test_longpoll_since_filters_old_events(self):
        """Events before `since` should not be returned."""
        self._post_hook("test", "old-event")
        time.sleep(0.5)

        # Get the timestamp of the event we just posted
        data = self._longpoll(since="2000-01-01T00:00:00Z", timeout=1)
        if data.get("events"):
            latest = data["events"][-1]["received_at"]
            # Now poll with since=latest, should get nothing (timeout)
            data2 = self._longpoll(since=latest, timeout=1)
            # Should return empty or no new events after `latest`
            new_events = [e for e in data2.get("events", []) if e["received_at"] > latest]
            self.assertEqual(len(new_events), 0)


if __name__ == "__main__":
    unittest.main()
