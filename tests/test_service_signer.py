# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""F4: certify's _ServiceSigner — sign via the console-backend service (CI OIDC +
the build's pre-approval), no key in CI. HTTP is mocked; the returned envelope is
verified over our bytes against the trust anchor before it is written."""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import certify


class TestServiceSigner(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.mf = os.path.join(self.d, "m.json")
        with open(self.mf, "w") as fh:
            fh.write('{"x":1}')
        self.sig = self.mf + ".sig"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _resp(self, obj):
        return io.BytesIO(json.dumps(obj).encode())

    def test_make_signer_prefers_service(self):
        s = certify._make_signer("ignored.pem", ("u", "t"), ("https://x", "42", "tok"))
        self.assertEqual(type(s).__name__, "_ServiceSigner")

    def test_sign_verifies_and_writes_envelope(self):
        env = {"alg": "ed25519", "key_id": "k1", "sig": "s"}
        calls = []
        def fake(req, timeout=None):
            calls.append(req.full_url)
            self.assertEqual(req.headers.get("Authorization"), "Bearer tok")
            return self._resp({"envelope": env, "preapproved_by": "alice"})
        with patch.object(certify.urllib.request, "urlopen", fake), \
             patch.object(certify.trust, "load_trusted_keys", lambda: {}), \
             patch.object(certify.trust, "verify_bytes", lambda b, e, t: "k1"):
            s = certify._ServiceSigner("https://bits.cern.ch", "42", "tok")
            out = s.sign_manifest(self.mf, self.sig)
        self.assertEqual(out, self.sig)
        self.assertEqual(json.load(open(self.sig)), env)
        self.assertIn("/sign/preapproved?build_id=42", calls[0])

    def test_sign_rejects_unverifiable_envelope(self):
        with patch.object(certify.urllib.request, "urlopen",
                          lambda req, timeout=None: self._resp({"envelope": {"sig": "bad"}})), \
             patch.object(certify.trust, "load_trusted_keys", lambda: {}), \
             patch.object(certify.trust, "verify_bytes", lambda b, e, t: None):
            s = certify._ServiceSigner("https://x", "1", "t")
            with self.assertRaises(certify.CertifyError):
                s.sign_manifest(self.mf, self.sig)
        self.assertFalse(os.path.exists(self.sig))   # nothing written on failure

    def test_key_id_from_trust_pubkey(self):
        with patch.object(certify.urllib.request, "urlopen",
                          lambda req, timeout=None: self._resp({"key_id": "kid9"})):
            s = certify._ServiceSigner("https://x", "1", "t")
            self.assertEqual(s.key_id(), "kid9")


if __name__ == "__main__":
    unittest.main()
