import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import require_sandbox
require_sandbox()

import hub  # noqa: E402


class ChannelRoutingTest(unittest.TestCase):
    def _bindings(self, data_dir, project="origin-bot"):
        return {
            "cc_connect": {
                project: {
                    "data_dir": str(data_dir),
                    "session_key": "",
                },
            },
            "cdp": {},
        }

    def _write_session_metadata(
        self,
        data_dir,
        session_key,
        chat_name,
        project="origin-bot",
    ):
        sessions_dir = Path(data_dir) / "sessions"
        sessions_dir.mkdir(parents=True)
        payload = {
            "user_meta": {
                session_key: {
                    "user_name": "User",
                    "chat_name": chat_name,
                },
            },
        }
        (sessions_dir / ("%s_test.json" % project)).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def test_named_feishu_group_is_detected_from_session_metadata(self):
        session_key = "feishu:oc_group:ou_user"
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_session_metadata(tmpdir, session_key, "Agent Builders")

            name = hub.origin_group_chat_name(
                self._bindings(tmpdir),
                "origin-bot",
                session_key,
            )

        self.assertEqual(name, "Agent Builders")

    def test_feishu_private_chat_id_is_not_treated_as_group_name(self):
        session_key = "feishu:oc_private:ou_user"
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_session_metadata(tmpdir, session_key, "oc_private")

            name = hub.origin_group_chat_name(
                self._bindings(tmpdir),
                "origin-bot",
                session_key,
            )

        self.assertIsNone(name)

    def test_unknown_platform_fails_open_without_guessing_chat_type(self):
        session_key = "telegram:chat:user"
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_session_metadata(tmpdir, session_key, "Agent Builders")

            name = hub.origin_group_chat_name(
                self._bindings(tmpdir),
                "origin-bot",
                session_key,
            )

        self.assertIsNone(name)

    def test_send_from_named_group_requires_explicit_private_acknowledgement(self):
        session_key = "feishu:oc_group:ou_user"
        with tempfile.TemporaryDirectory() as tmpdir:
            bindings = self._bindings(tmpdir)
            self._write_session_metadata(tmpdir, session_key, "Agent Builders")
            args = hub.parse_args(["send", "target-bot", "hello"])
            output = io.StringIO()
            with mock.patch.object(
                hub,
                "ensure_registry_and_bindings",
                return_value=({"version": 2, "agents": {}}, bindings),
            ):
                with mock.patch.dict(
                    os.environ,
                    {
                        "CC_PROJECT": "origin-bot",
                        "CC_SESSION_KEY": session_key,
                    },
                ):
                    with contextlib.redirect_stdout(output):
                        result = hub.cmd_send(args)

        self.assertEqual(result, 1)
        self.assertIn("origin is named group chat 'Agent Builders'", output.getvalue())
        self.assertIn("cc-connect relay send", output.getvalue())
        self.assertIn("--private", output.getvalue())

    def test_private_flag_acknowledges_intent_and_reaches_normal_resolution(self):
        session_key = "feishu:oc_group:ou_user"
        with tempfile.TemporaryDirectory() as tmpdir:
            bindings = self._bindings(tmpdir)
            self._write_session_metadata(tmpdir, session_key, "Agent Builders")
            args = hub.parse_args(
                ["send", "missing-target", "hello", "--private"],
            )
            output = io.StringIO()
            with mock.patch.object(
                hub,
                "ensure_registry_and_bindings",
                return_value=({"version": 2, "agents": {}}, bindings),
            ):
                with mock.patch.dict(
                    os.environ,
                    {
                        "CC_PROJECT": "origin-bot",
                        "CC_SESSION_KEY": session_key,
                    },
                ):
                    with contextlib.redirect_stdout(output):
                        result = hub.cmd_send(args)

        self.assertEqual(result, 1)
        self.assertIn("Unknown agent", output.getvalue())
        self.assertNotIn("origin is named group chat", output.getvalue())


if __name__ == "__main__":
    unittest.main()
