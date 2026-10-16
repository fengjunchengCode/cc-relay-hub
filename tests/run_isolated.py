"""Run tests in a fresh interpreter with a temporary HOME and production-file guard.

Usage: python3 -B tests/run_isolated.py [--guard-report /tmp/report.json] [tests.test_name ...]
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Import the standalone helper, not the tests package which deliberately rejects
# an unisolated interpreter. No application modules are imported in the parent.
from sandbox import make_environment, protected_paths, require_sandbox, snapshot, verify_snapshot


def worker(names):
    require_sandbox()
    import unittest
    suite = (unittest.defaultTestLoader.loadTestsFromNames(names) if names else
             unittest.defaultTestLoader.discover(str(ROOT/'tests'), top_level_dir=str(ROOT)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--guard-report', help='Write before/after hashes and timestamps (no file contents)')
    parser.add_argument('tests', nargs='*')
    args = parser.parse_args(argv)
    if args.worker: return worker(args.tests)
    before = snapshot(protected_paths())
    code = 1
    try:
        with tempfile.TemporaryDirectory(prefix='relay-tests-') as folder:
            env = make_environment(folder, ROOT)
            cwd = Path(folder)/'work'; cwd.mkdir()
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', *args.tests],
                                    cwd=str(cwd), env=env)
            code = result.returncode
    finally:
        report = verify_snapshot(before)
        if args.guard_report:
            Path(args.guard_report).write_text(json.dumps(report, indent=2)+'\n')
        if report['ok']:
            print('Production file guard: unchanged (%d paths; mtime + SHA-256).' % len(before), flush=True)
        else:
            print('Production file guard FAILED: '+', '.join(report['changed']), file=sys.stderr, flush=True)
            code = 1
    return code


if __name__ == '__main__': sys.exit(main())
