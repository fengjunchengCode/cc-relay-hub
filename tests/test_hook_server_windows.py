import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_for_port(port, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


@unittest.skipUnless(_node_available(), "node not available")
class HookServerWindowsCompatTest(unittest.TestCase):
    def test_hook_server_honors_python_bin_when_python3_is_bad(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            bad_python3 = fake_bin / "python3"
            bad_python3.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            bad_python3.chmod(0o755)
            good_python = fake_bin / "python"
            good_python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
                "cat >/dev/null\n"
                "echo '{\"status\":\"fake\"}'\n",
                encoding="utf-8",
            )
            good_python.chmod(0o755)

            port = _free_port()
            env = dict(os.environ)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["PYTHON_BIN"] = str(good_python)
            env["HOOK_PORT"] = str(port)
            env["HOOK_EVENTS_FILE"] = str(tmp / "events.jsonl")
            env["HOME"] = str(tmp)

            server = subprocess.Popen(
                ["node", str(ROOT / "hook-server.mjs")],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(_wait_for_port(port), "hook server did not start")
                payload = json.dumps(
                    {
                        "event": "message.sent",
                        "project": "agent-a",
                        "session_key": "s1",
                        "content": "[cc-relay reply_to=req-1]\nhello",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cc-connect/hooks/reply",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    self.assertEqual(resp.status, 204)
                time.sleep(0.5)
            finally:
                server.send_signal(signal.SIGTERM)
                stdout, stderr = server.communicate(timeout=5)

            self.assertIn("[hub]", stdout)
            self.assertNotIn("hub forward exited with code 127", stderr)

    def test_hook_server_uses_file_url_to_path(self):
        source = (ROOT / "hook-server.mjs").read_text(encoding="utf-8")

        self.assertIn("fileURLToPath(import.meta.url)", source)
        self.assertNotIn("new URL(import.meta.url).pathname", source)


if __name__ == "__main__":
    unittest.main()
