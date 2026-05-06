import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402


class HubPathTest(unittest.TestCase):
    def test_hub_dir_uses_cc_relay_hub_name(self):
        self.assertEqual(hub.HUB_DIR.name, "cc-relay-hub")


if __name__ == "__main__":
    unittest.main()
