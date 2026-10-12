"""Cross-node envelope encoding: JSON -> gzip -> encrypt -> base64url -> chunks.

Chunk line format (one chunk per rendezvous message or all chunks joined by
newlines on a direct HTTP body):

    CCR1 <envelope_id> <seq>/<total> <base64url-data>
"""

import base64
import gzip
import json
import time
import uuid

from core import crypto

PROTOCOL_VERSION = 1
CHUNK_DATA_CHARS = 3000
REASSEMBLY_TTL_SECS = 180.0

KINDS = ("hello", "request", "notice", "reply", "followup", "receipt")


def new_envelope(kind, from_node, to_node, payload, from_name=None, envelope_id=None, ts=None):
    if kind not in KINDS:
        raise ValueError("unknown envelope kind: %s" % kind)
    return {
        "v": PROTOCOL_VERSION,
        "kind": kind,
        "envelope_id": envelope_id or uuid.uuid4().hex,
        "from_node": from_node,
        "from_name": from_name or "",
        "to_node": to_node or "*",
        "ts": float(ts if ts is not None else time.time()),
        "payload": payload or {},
    }


def _b64e(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text):
    text = text.strip()
    pad = (-len(text)) % 4
    return base64.urlsafe_b64decode(text + "=" * pad)


def encode(envelope, enc_key):
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = crypto.encrypt(enc_key, gzip.compress(raw, compresslevel=6))
    return _b64e(blob)


def decode(text, enc_key, max_age_secs=None, now=None):
    raw = gzip.decompress(crypto.decrypt(enc_key, _b64d(text)))
    envelope = json.loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported envelope")
    if envelope.get("kind") not in KINDS:
        raise ValueError("unknown envelope kind")
    if max_age_secs is not None:
        now = time.time() if now is None else now
        if abs(now - float(envelope.get("ts", 0))) > max_age_secs:
            raise ValueError("envelope outside time window")
    return envelope


def to_chunks(envelope, enc_key, chunk_chars=CHUNK_DATA_CHARS):
    data = encode(envelope, enc_key)
    pieces = [data[i:i + chunk_chars] for i in range(0, len(data), chunk_chars)] or [""]
    total = len(pieces)
    return [
        "CCR1 %s %d/%d %s" % (envelope["envelope_id"], index + 1, total, piece)
        for index, piece in enumerate(pieces)
    ]


def parse_chunk(line):
    parts = (line or "").strip().split(" ", 3)
    if len(parts) != 4 or parts[0] != "CCR1":
        return None
    try:
        seq_text, total_text = parts[2].split("/", 1)
        seq, total = int(seq_text), int(total_text)
    except ValueError:
        return None
    if seq < 1 or total < 1 or seq > total:
        return None
    return {"envelope_id": parts[1], "seq": seq, "total": total, "data": parts[3]}


class Reassembler(object):
    """Collects chunks and yields decoded envelopes once complete."""

    def __init__(self, enc_key, ttl_secs=REASSEMBLY_TTL_SECS, max_age_secs=None):
        self.enc_key = enc_key
        self.ttl_secs = ttl_secs
        self.max_age_secs = max_age_secs
        self._partial = {}

    def feed(self, line, now=None):
        now = time.time() if now is None else now
        self._expire(now)
        chunk = parse_chunk(line)
        if not chunk:
            return None
        entry = self._partial.setdefault(
            chunk["envelope_id"], {"total": chunk["total"], "parts": {}, "first_seen": now}
        )
        if entry["total"] != chunk["total"]:
            return None
        entry["parts"][chunk["seq"]] = chunk["data"]
        if len(entry["parts"]) < entry["total"]:
            return None
        data = "".join(entry["parts"][i] for i in range(1, entry["total"] + 1))
        del self._partial[chunk["envelope_id"]]
        return decode(data, self.enc_key, max_age_secs=self.max_age_secs, now=now)

    def feed_text(self, text, now=None):
        envelopes = []
        for line in (text or "").splitlines():
            envelope = self.feed(line, now=now)
            if envelope is not None:
                envelopes.append(envelope)
        return envelopes

    def _expire(self, now):
        stale = [key for key, entry in self._partial.items() if now - entry["first_seen"] > self.ttl_secs]
        for key in stale:
            del self._partial[key]
