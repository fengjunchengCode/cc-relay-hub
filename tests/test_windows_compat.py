import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsCompatTest(unittest.TestCase):
    def test_hub_import_and_registry_mutation_without_fcntl(self):
        script = textwrap.dedent(
            r"""
            import builtins
            import json
            import sys
            import tempfile
            import types
            from pathlib import Path

            real_import = builtins.__import__

            fake_msvcrt = types.ModuleType("msvcrt")
            fake_msvcrt.LK_LOCK = 1
            fake_msvcrt.LK_UNLCK = 2
            fake_msvcrt.calls = []

            def locking(fd, mode, nbytes):
                fake_msvcrt.calls.append((mode, nbytes))

            fake_msvcrt.locking = locking
            sys.modules["msvcrt"] = fake_msvcrt

            def guarded_import(name, *args, **kwargs):
                if name == "fcntl":
                    raise ModuleNotFoundError("No module named 'fcntl'")
                return real_import(name, *args, **kwargs)

            builtins.__import__ = guarded_import

            sys.path.insert(0, %r)
            import hub

            with tempfile.TemporaryDirectory() as td:
                registry_path = Path(td) / "registry.json"
                registry_path.write_text(
                    json.dumps({"version": 2, "agents": {}, "groups": {}}),
                    encoding="utf-8",
                )
                hub.REGISTRY_PATH = registry_path
                hub.mutate_registry_groups(
                    lambda groups: groups.update({"win": {"members": []}})
                )
                data = json.loads(registry_path.read_text(encoding="utf-8"))
                assert "win" in data["groups"], data

            assert fake_msvcrt.calls, "msvcrt.locking was not called"
            print("ok")
            """
            % str(ROOT)
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("ok", result.stdout)

    def test_shell_wrapper_falls_back_to_python_when_python3_fails(self):
        wrapper_path = ROOT / "bin" / "cc-relay-hub"
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td)
            python3_path = bin_dir / "python3"
            python_path = bin_dir / "python"
            python3_path.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            python_path.write_text(
                "#!/bin/sh\nexec %s \"$@\"\n" % sys.executable,
                encoding="utf-8",
            )
            python3_path.chmod(0o755)
            python_path.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                [str(wrapper_path), "list"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                env=env,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Found", result.stdout)

    def test_windows_cmd_wrapper_exists(self):
        self.assertTrue((ROOT / "bin" / "cc-relay-hub.cmd").exists())


if __name__ == "__main__":
    unittest.main()
