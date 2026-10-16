import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import require_sandbox
require_sandbox()

from core import crypto  # noqa: E402


class CryptoTest(unittest.TestCase):
    def setUp(self):
        self.key = crypto.derive_key(b"s" * 32, "enc")

    def test_roundtrip_unicode(self):
        text = "héllo 世界 " * 500
        self.assertEqual(crypto.decrypt(self.key, crypto.encrypt(self.key, text)).decode("utf-8"), text)

    def test_empty_payload(self):
        self.assertEqual(crypto.decrypt(self.key, crypto.encrypt(self.key, b"")), b"")

    def test_tamper_detected(self):
        blob = bytearray(crypto.encrypt(self.key, b"payload"))
        blob[crypto.NONCE_LEN] ^= 0x01
        with self.assertRaises(ValueError):
            crypto.decrypt(self.key, bytes(blob))

    def test_wrong_key_rejected(self):
        blob = crypto.encrypt(self.key, b"payload")
        with self.assertRaises(ValueError):
            crypto.decrypt(crypto.derive_key(b"t" * 32, "enc"), blob)

    def test_nonce_differs_per_message(self):
        self.assertNotEqual(crypto.encrypt(self.key, b"x"), crypto.encrypt(self.key, b"x"))

    def test_sign_verify(self):
        sig = crypto.sign(self.key, "n1", "1.0", "nonce", "POST", "/peer/v1/inbox", "digest")
        self.assertTrue(crypto.verify(self.key, sig, "n1", "1.0", "nonce", "POST", "/peer/v1/inbox", "digest"))
        self.assertFalse(crypto.verify(self.key, sig, "n1", "1.0", "nonce", "POST", "/peer/v1/hello", "digest"))
        self.assertFalse(crypto.verify(self.key, None, "n1"))


if __name__ == "__main__":
    unittest.main()
