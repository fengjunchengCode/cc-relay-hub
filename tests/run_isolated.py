"""Run all unit/integration tests without discovering the user's live registry.

Usage: python3 tests/run_isolated.py [test_module ...]
Every default DB/hook subprocess points at a TemporaryDirectory. Individual
fixtures can use their own temporary paths. No real relay send is part of this
runner; external providers in tests are fakes and localhost test webhooks.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='relay-test-') as folder:
        root = Path(folder) / 'cc-relay-hub'
        root.mkdir()
        os.environ['CC_RELAY_HUB_DIR'] = str(root)
        (root/'registry.json').write_text(json.dumps({'version':2,'agents':{},'groups':{}}))
        (root/'bindings.json').write_text(json.dumps({'cc_connect':{},'cdp':{}}))
        if len(sys.argv) > 1:
            suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
        else:
            suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
