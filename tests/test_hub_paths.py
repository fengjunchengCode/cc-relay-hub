import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402


class HubPathTest(unittest.TestCase):
    def test_hub_dir_uses_cc_relay_hub_name(self):
        self.assertEqual(hub.HUB_DIR.name, "cc-relay-hub")

    def test_hub_command_prefers_cmd_wrapper_on_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir) / "cc-relay-hub"
            bin_dir = hub_dir / "bin"
            bin_dir.mkdir(parents=True)
            posix_bin = bin_dir / "cc-relay-hub"
            cmd_bin = bin_dir / "cc-relay-hub.cmd"
            posix_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            cmd_bin.write_text("@echo off\n", encoding="utf-8")

            with mock.patch.object(hub, "HUB_DIR", hub_dir), \
                    mock.patch.object(hub, "HUB_BIN", posix_bin), \
                    mock.patch.object(hub.os, "name", "nt"):
                self.assertEqual(hub._hub_command(), str(cmd_bin))


if __name__ == "__main__":
    unittest.main()
