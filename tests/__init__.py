"""Fail closed if tests are imported outside the isolated runner."""
from .sandbox import require_sandbox
require_sandbox()
