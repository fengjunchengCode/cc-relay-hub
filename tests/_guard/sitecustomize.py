"""Loaded only through the isolated test runner's PYTHONPATH."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from sandbox import install_write_guard
    install_write_guard()
except Exception as exc:
    # Python normally prints sitecustomize errors and continues. Fail closed.
    sys.stderr.write('Test sandbox startup failed: %s\n' % exc)
    sys.stderr.flush()
    os._exit(2)
