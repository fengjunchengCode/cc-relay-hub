import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InstallAssetsTest(unittest.TestCase):
    def test_skill_file_exists(self):
        skill_path = ROOT / "skills" / "cc-relay.md"
        self.assertTrue(skill_path.exists())

    def test_wrapper_exists_and_is_executable(self):
        wrapper_path = ROOT / "bin" / "cc-relay-hub"
        self.assertTrue(wrapper_path.exists())
        mode = wrapper_path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_wrapper_invokes_hub_cli(self):
        wrapper_path = ROOT / "bin" / "cc-relay-hub"
        result = subprocess.run(
            [str(wrapper_path), "list"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Found", result.stdout)


if __name__ == "__main__":
    unittest.main()
