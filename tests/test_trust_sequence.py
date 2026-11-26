# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A manifest with a sequence: a node never goes back to an older one."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bits_helpers import trust


class SequenceTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.priv = Ed25519PrivateKey.generate()
        keys = os.path.join(self.tmp, "keys")
        os.makedirs(keys)
        with open(os.path.join(keys, "pub.pem"), "wb") as fh:
            fh.write(self.priv.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        with open(os.path.join(keys, "key-policy.json"), "w") as fh:
            fh.write('{"default": ["*"]}\n')
        self.state = os.path.join(self.tmp, "state", "trust-sequence.json")
        env = mock.patch.dict(os.environ, {"BITS_TRUST_KEYS": keys,
                                           "BITS_TRUST_SEQUENCE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)

    def manifest(self, name, seq, arch="el9", hashes=("h1",), **extra):
        doc = dict({"kind": "common-manifest", "architecture": arch,
                    "packages": [{"hash": h, "tarball_sha256": "sha256:" + h,
                                  "effective_architecture": arch} for h in hashes]}, **extra)
        if seq is not None:
            doc["sequence"] = seq
        path = os.path.join(self.tmp, name)
        raw = json.dumps(doc).encode()
        with open(path, "wb") as fh:
            fh.write(raw)
        with open(path + ".sig", "w") as fh:
            json.dump(trust.sign_bytes(raw, self.priv), fh)
        return path

    def trusted(self, path):
        return trust.trusted_index(path)[0] is not None

    def test_a_manifest_that_never_expires_is_warned_about_once(self):
        path = self.manifest("forever", 1)
        with self.assertLogs("bits", "WARNING") as logs:
            self.assertTrue(self.trusted(path))
            self.assertTrue(self.trusted(path))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("forever never expires", logs.output[0])
        with mock.patch("bits_helpers.log.warning") as warned:
            self.assertTrue(self.trusted(self.manifest("dated", 2, expires="2999-01-01T00:00:00Z")))
        self.assertFalse(warned.called)

    def test_a_node_never_goes_back(self):
        self.assertTrue(self.trusted(self.manifest("m1", 1)))
        self.assertTrue(self.trusted(self.manifest("m3", 3, hashes=("h1", "h3"))))
        with self.assertLogs("bits", "WARNING") as logs:
            self.assertFalse(self.trusted(self.manifest("m2", 2)))   # put back
        self.assertTrue(any("older than the 3" in line for line in logs.output), logs.output)
        self.assertTrue(self.trusted(os.path.join(self.tmp, "m3")))  # the same one again

    def test_two_manifests_under_one_number(self):
        self.assertTrue(self.trusted(self.manifest("a", 5)))
        with self.assertLogs("bits", "WARNING"):
            self.assertFalse(self.trusted(self.manifest("b", 5, hashes=("h2",))))

    def test_each_architecture_counts_on_its_own(self):
        self.assertTrue(self.trusted(self.manifest("x", 9, arch="el9")))
        self.assertTrue(self.trusted(self.manifest("y", 1, arch="el10")))

    def test_after_numbered_ones_an_unnumbered_one_is_refused(self):
        self.assertTrue(self.trusted(self.manifest("ci", None)))       # before the switch
        self.assertTrue(self.trusted(self.manifest("w", 1)))
        with self.assertLogs("bits", "WARNING"):
            self.assertFalse(self.trusted(self.manifest("ci2", None)))
        self.assertTrue(self.trusted(self.manifest("ci10", None, arch="el10")))

    def test_each_issuer_numbers_on_its_own(self):
        self.assertTrue(self.trusted(self.manifest("p", 40, issuer="bits-orchestrator/prod")))
        self.assertTrue(self.trusted(self.manifest("t", 3, issuer="bits-orchestrator/test")))
        with self.assertLogs("bits", "WARNING"):
            self.assertFalse(self.trusted(self.manifest("p2", 39,
                                                        issuer="bits-orchestrator/prod")))

    def test_another_key_cannot_use_up_an_issuer(self):
        self.assertTrue(self.trusted(self.manifest("p", 40, issuer="bits-orchestrator/prod")))
        other = Ed25519PrivateKey.generate()
        with open(os.path.join(os.environ["BITS_TRUST_KEYS"], "other.pem"), "wb") as fh:
            fh.write(other.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        mine, self.priv = self.priv, other
        self.assertTrue(self.trusted(self.manifest("x", 10**9, issuer="bits-orchestrator/prod")))
        self.priv = mine
        self.assertTrue(self.trusted(self.manifest("p41", 41, issuer="bits-orchestrator/prod")))

    def test_what_a_node_remembered_before_is_carried_over(self):
        self.assertTrue(self.trusted(self.manifest("p", 40, issuer="bits-orchestrator/prod")))
        with open(self.state) as fh:
            state = json.load(fh)
        state = {(k.split(" by ")[0] + " " + k.split()[-1] if " by " in k else k): v
                 for k, v in state.items()}                    # as an older bits wrote it
        with open(self.state, "w") as fh:
            json.dump(state, fh)
        with self.assertLogs("bits", "WARNING"):
            self.assertFalse(self.trusted(self.manifest("p39", 39, issuer="bits-orchestrator/prod")))

    def test_a_damaged_entry_starts_again(self):
        self.assertTrue(self.trusted(self.manifest("a", 2)))
        with open(self.state) as fh:
            state = json.load(fh)
        for k in state:
            if not k.startswith("numbered-by"):
                state[k] = {"sequence": "two"}
        with open(self.state, "w") as fh:
            json.dump(state, fh)
        self.assertTrue(self.trusted(self.manifest("b", 1)))

    def test_manifests_without_a_sequence_are_left_to_their_expiry(self):
        self.assertTrue(self.trusted(self.manifest("old", None)))
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(self.trusted(self.manifest("exp", None, expires="2020-01-01T00:00:00Z")))

    def test_a_malformed_sequence_is_refused(self):
        with self.assertLogs("bits", "WARNING"):
            self.assertFalse(self.trusted(self.manifest("bad", "7")))

    def test_an_unwritable_memory_does_not_stop_the_build(self):
        os.makedirs(os.path.dirname(self.state))
        os.chmod(os.path.dirname(self.state), 0o500)
        self.addCleanup(os.chmod, os.path.dirname(self.state), 0o700)
        if os.access(os.path.dirname(self.state), os.W_OK):
            self.skipTest("running as root")
        with self.assertLogs("bits", "WARNING"):
            self.assertTrue(self.trusted(self.manifest("m", 1)))


if __name__ == "__main__":
    unittest.main()
