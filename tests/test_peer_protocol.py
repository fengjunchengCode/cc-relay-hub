import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import require_sandbox
require_sandbox()

from core import crypto, node_identity, peer_protocol  # noqa: E402


class PeerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.key = crypto.derive_key(b"k" * 32, "enc")

    def test_single_chunk_roundtrip(self):
        env = peer_protocol.new_envelope("request", "n_a", "n_b", {"body": "hi"}, from_name="a")
        chunks = peer_protocol.to_chunks(env, self.key)
        self.assertEqual(len(chunks), 1)
        out = peer_protocol.Reassembler(self.key).feed(chunks[0])
        self.assertEqual(out["payload"]["body"], "hi")
        self.assertEqual(out["envelope_id"], env["envelope_id"])

    def test_multi_chunk_out_of_order(self):
        body = "".join(random.choice("abcdefghij0123456789") for _ in range(20000))
        env = peer_protocol.new_envelope("request", "n_a", "n_b", {"body": body})
        chunks = peer_protocol.to_chunks(env, self.key, chunk_chars=1000)
        self.assertGreater(len(chunks), 3)
        shuffled = chunks[:]
        random.shuffle(shuffled)
        reassembler = peer_protocol.Reassembler(self.key)
        results = [reassembler.feed(c) for c in shuffled]
        done = [r for r in results if r is not None]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["payload"]["body"], body)

    def test_duplicate_chunks_ignored(self):
        env = peer_protocol.new_envelope("notice", "n_a", "*", {"x": 1})
        chunks = peer_protocol.to_chunks(env, self.key, chunk_chars=50)
        reassembler = peer_protocol.Reassembler(self.key)
        results = reassembler.feed_text("\n".join(chunks + chunks[:2]))
        self.assertEqual(len(results), 1)

    def test_garbage_lines_ignored(self):
        reassembler = peer_protocol.Reassembler(self.key)
        self.assertIsNone(reassembler.feed("hello world"))
        self.assertIsNone(reassembler.feed("CCR1 x 2/1 abc"))

    def test_wrong_key_raises(self):
        env = peer_protocol.new_envelope("request", "n_a", "n_b", {})
        chunk = peer_protocol.to_chunks(env, self.key)[0]
        with self.assertRaises(ValueError):
            peer_protocol.Reassembler(crypto.derive_key(b"z" * 32, "enc")).feed(chunk)

    def test_stale_envelope_rejected(self):
        env = peer_protocol.new_envelope("request", "n_a", "n_b", {}, ts=1000.0)
        chunk = peer_protocol.to_chunks(env, self.key)[0]
        with self.assertRaises(ValueError):
            peer_protocol.Reassembler(self.key, max_age_secs=60).feed(chunk, now=5000.0)

    def test_invite_roundtrip(self):
        secret = "ab" * 32
        invite = node_identity.encode_invite(secret, "http://relay.local:9140")
        self.assertTrue(invite.startswith("ccrelay://join/"))
        decoded = node_identity.decode_invite(invite)
        self.assertEqual(decoded["secret"], secret)
        self.assertEqual(decoded["rendezvous_url"], "http://relay.local:9140")
        with self.assertRaises(ValueError):
            node_identity.decode_invite("ccrelay://join/notbase64!!")

    def test_cluster_keys_are_distinct(self):
        keys = node_identity.ClusterKeys("cd" * 32)
        self.assertNotEqual(keys.enc_key, keys.auth_key)
        self.assertTrue(keys.topic.startswith("ccr"))
        self.assertEqual(keys.node_topic("n_1234567890abcdef"), keys.topic + "-1234567890ab")


if __name__ == "__main__":
    unittest.main()
