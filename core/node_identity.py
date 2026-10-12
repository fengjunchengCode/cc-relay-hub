"""Node identity (node.json), cluster secret and invite strings."""

import base64
import json
import os
import secrets
import socket
import time
import uuid

from core import crypto

NODE_FILE = "node.json"
SECRET_FILE = "cluster.secret"
DEFAULT_NODE_PORT = 9130
DEFAULT_BEACON_PORT = 9131
DEFAULT_RENDEZVOUS_URL = "https://ntfy.sh"
INVITE_PREFIX = "ccrelay://join/"


def _default_name():
    name = socket.gethostname().split(".")[0].strip().lower()
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name) or "node"


def node_path(hub_dir):
    return os.path.join(str(hub_dir), NODE_FILE)


def secret_path(hub_dir, node=None):
    custom = (node or {}).get("cluster_secret_file")
    if custom:
        return custom if os.path.isabs(custom) else os.path.join(str(hub_dir), custom)
    return os.path.join(str(hub_dir), SECRET_FILE)


def load_node(hub_dir):
    path = node_path(hub_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_node(hub_dir, node):
    os.makedirs(str(hub_dir), exist_ok=True)
    tmp = node_path(hub_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(node, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, node_path(hub_dir))
    return node


def ensure_node(hub_dir, name=None, port=None, beacon_port=None, rendezvous_url=None):
    node = load_node(hub_dir) or {}
    changed = False
    defaults = {
        "node_id": "n_" + uuid.uuid4().hex,
        "name": name or _default_name(),
        "listen_host": "0.0.0.0",
        "listen_port": port or DEFAULT_NODE_PORT,
        "beacon_port": beacon_port or DEFAULT_BEACON_PORT,
        "rendezvous_url": rendezvous_url or DEFAULT_RENDEZVOUS_URL,
        "created_at": time.time(),
    }
    for key, value in defaults.items():
        if key not in node:
            node[key] = value
            changed = True
    for key, value in (("name", name), ("listen_port", port), ("beacon_port", beacon_port), ("rendezvous_url", rendezvous_url)):
        if value and node.get(key) != value:
            node[key] = value
            changed = True
    if changed:
        save_node(hub_dir, node)
    return node


def load_secret(hub_dir, node=None):
    path = secret_path(hub_dir, node)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().strip()
    return text or None


def save_secret(hub_dir, secret_hex, node=None):
    path = secret_path(hub_dir, node)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(secret_hex.strip() + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return secret_hex


def ensure_secret(hub_dir, node=None):
    secret = load_secret(hub_dir, node)
    if secret:
        return secret
    return save_secret(hub_dir, secrets.token_hex(32), node)


class ClusterKeys(object):
    def __init__(self, secret_hex):
        self.secret_hex = secret_hex
        secret = bytes.fromhex(secret_hex)
        self.enc_key = crypto.derive_key(secret, "cc-relay-hub/enc")
        self.auth_key = crypto.derive_key(secret, "cc-relay-hub/auth")
        self.topic = "ccr" + crypto.derive_key(secret, "cc-relay-hub/rendezvous").hex()[:24]
        self.fingerprint = crypto.derive_key(secret, "cc-relay-hub/fp").hex()[:12]

    def node_topic(self, node_id):
        return "%s-%s" % (self.topic, node_id.replace("n_", "")[:12])


def encode_invite(secret_hex, rendezvous_url):
    payload = json.dumps({"s": secret_hex, "r": rendezvous_url}, separators=(",", ":")).encode("utf-8")
    return INVITE_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_invite(text):
    text = (text or "").strip()
    if text.startswith(INVITE_PREFIX):
        text = text[len(INVITE_PREFIX):]
    if not text:
        raise ValueError("empty invite")
    pad = (-len(text)) % 4
    try:
        payload = json.loads(base64.urlsafe_b64decode(text + "=" * pad).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid invite: %s" % exc)
    secret = payload.get("s") or ""
    if len(secret) != 64 or any(ch not in "0123456789abcdef" for ch in secret):
        raise ValueError("invalid invite secret")
    return {"secret": secret, "rendezvous_url": payload.get("r") or DEFAULT_RENDEZVOUS_URL}
