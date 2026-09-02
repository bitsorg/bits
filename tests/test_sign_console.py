# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the cross-device console signing client (bits_helpers.sign_console)."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import sign_console


class TestSignConsole(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mp = os.path.join(self.dir, "m.json")
        with open(self.mp, "wb") as fh:
            fh.write(b'{"packages":[{"package":"A","group":"lcg"}]}')
        self.digest = hashlib.sha256(open(self.mp, "rb").read()).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _req(self):
        return {"request_id": "r1", "approve_url": "http://x/?approve=r1",
                "digest": self.digest, "groups": ["lcg"]}

    def test_flow_verifies_and_writes_sig(self):
        results = iter([
            {"status": "pending"},
            {"status": "signed", "envelope": {"alg": "ed25519", "key_id": "k", "sig": "s"},
             "signed_by": "alice", "groups": ["lcg"]}])
        with patch.object(sign_console, "_post", lambda url, data, ctype="": self._req()), \
             patch.object(sign_console, "_get", lambda url: next(results)), \
             patch.object(sign_console.trust, "load_trusted_keys", lambda: {}), \
             patch.object(sign_console.trust, "verify_bytes", lambda b, e, t: "kid1234"), \
             patch.object(sign_console.time, "sleep", lambda s: None):
            out = sign_console.sign_via_console("http://x", self.mp, timeout=10)
        self.assertEqual(json.load(open(out))["key_id"], "k")

    def test_unverifiable_signature_rejected(self):
        with patch.object(sign_console, "_post", lambda url, data, ctype="": self._req()), \
             patch.object(sign_console, "_get",
                          lambda url: {"status": "signed", "envelope": {}, "signed_by": "a"}), \
             patch.object(sign_console.trust, "load_trusted_keys", lambda: {}), \
             patch.object(sign_console.trust, "verify_bytes", lambda b, e, t: None), \
             patch.object(sign_console.time, "sleep", lambda s: None):
            with self.assertRaises(SystemExit):
                sign_console.sign_via_console("http://x", self.mp, timeout=10)

    def test_digest_mismatch_aborts(self):
        bad = dict(self._req(), digest="deadbeef")
        with patch.object(sign_console, "_post", lambda url, data, ctype="": bad):
            with self.assertRaises(SystemExit):
                sign_console.sign_via_console("http://x", self.mp, timeout=10)


if __name__ == "__main__":
    unittest.main()
