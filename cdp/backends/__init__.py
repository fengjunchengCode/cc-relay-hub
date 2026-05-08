"""Backend registry — maps backend names to classes."""
from __future__ import annotations

from cdp.backends.antigravity import AntigravityBackend

BACKENDS = {
    "antigravity": AntigravityBackend,
}


def get_backend(name):
    """Get a backend class by name. Raises ValueError if not found."""
    name = name.lower().strip()
    if name in BACKENDS:
        return BACKENDS[name]
    if name == "cursor":
        from cdp.backends.cursor import CursorBackend
        BACKENDS["cursor"] = CursorBackend
        return CursorBackend
    raise ValueError("Unknown backend: %s. Available: %s" % (name, list(BACKENDS.keys())))


def list_backends():
    """Return list of (name, description) tuples."""
    return [
        ("antigravity", "Google Antigravity IDE (Electron, CDP)"),
        ("cursor", "Cursor IDE (Electron, CDP) — stub"),
    ]
