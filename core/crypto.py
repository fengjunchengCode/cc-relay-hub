"""Stdlib-only authenticated encryption and request signing for cross-node traffic.

Construction (standard primitives, standard composition):
- key derivation: HMAC-SHA256(cluster_secret, label)
- confidentiality: HMAC-SHA256 in counter mode as a PRF keystream (nonce || counter)
- integrity: encrypt-then-MAC with HMAC-SHA256 over nonce || ciphertext

Blob layout: nonce(16) || ciphertext(n) || tag(32)

Swap `encrypt`/`decrypt` for AES-GCM later without touching callers.
"""

import hashlib
import hmac
import os
import struct

NONCE_LEN = 16
TAG_LEN = 32
_BLOCK = 32


def derive_key(secret, label):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(label, str):
        label = label.encode("utf-8")
    return hmac.new(secret, label, hashlib.sha256).digest()


def _subkeys(key):
    return derive_key(key, b"enc"), derive_key(key, b"mac")


def _keystream(enc_key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(key, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    enc_key, mac_key = _subkeys(key)
    nonce = os.urandom(NONCE_LEN)
    stream = _keystream(enc_key, nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + ciphertext + tag


def decrypt(key, blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < NONCE_LEN + TAG_LEN:
        raise ValueError("ciphertext too short")
    enc_key, mac_key = _subkeys(key)
    nonce = bytes(blob[:NONCE_LEN])
    tag = bytes(blob[-TAG_LEN:])
    ciphertext = bytes(blob[NONCE_LEN:-TAG_LEN])
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("authentication failed")
    stream = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


def sign(key, *parts):
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        if isinstance(part, str):
            part = part.encode("utf-8")
        mac.update(part)
        mac.update(b"\0")
    return mac.hexdigest()


def verify(key, signature, *parts):
    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign(key, *parts), signature)


def body_digest(body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body or b"").hexdigest()
