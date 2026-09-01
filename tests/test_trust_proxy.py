# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for signing via the security-proxy (trust.sign_*_via_proxy).

A tiny in-process HTTP server emulates the proxy's /sign route, signing with a
known Ed25519 key exactly as the real proxy does (full-length keyid + base64
sig). Because Ed25519 is deterministic, the proxy envelope must equal a local
sign_bytes envelope byte-for-byte.
"""

import base64
import hashlib
import http.server
import json
import os
import tempfile
import threading
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bits_helpers import trust


class _SignHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pub_raw(self):
        return trust._pub_bytes(self.server.priv.public_key())

    def _full_keyid(self):  # real proxy returns the FULL sha256 hex, not [:16]
        return hashlib.sha256(self._pub_raw()).hexdigest()

    def _authed(self):  # emulate the proxy's gate-token check -> 401 on mismatch
        if self.headers.get("Authorization") != "Bearer " + self.server.token:
            self._reply(401, {"error": "Invalid token"})
            return False
        return True

    def do_POST(self):
        if not self._authed():
            return
        n = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(n)
        if getattr(self.server, "malformed", False):
            return self._reply(200, {"unexpected": "shape"})
        sig = self.server.priv.sign(data)
        self._reply(200, {"keyid": self._full_keyid(),
                          "sig": base64.b64encode(sig).decode("ascii")})

    def do_GET(self):
        if not self._authed():
            return
        self._reply(200, {"keyid": self._full_keyid(),
                          "publicKey": base64.b64encode(self._pub_raw()).decode("ascii")})


class TestSignViaProxy(unittest.TestCase):
    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _SignHandler)
        self.srv.priv = self.priv
        self.srv.token = "gate-token"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/sign/bits" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_envelope_matches_local_byte_for_byte(self):
        # The whole point: a proxy signature is indistinguishable from a local one.
        data = b"common-manifest-bytes-\x00\x01\x02"
        local = trust.sign_bytes(data, self.priv)
        via = trust.sign_bytes_via_proxy(data, self.url, "gate-token")
        self.assertEqual(via, local)
        # And it verifies against the key as a trust anchor.
        trusted = {trust.key_id(self.priv.public_key()): self.priv.public_key()}
        self.assertEqual(trust.verify_bytes(data, via, trusted),
                         trust.key_id(self.priv.public_key()))

    def test_bad_token_raises(self):
        with self.assertRaises(RuntimeError):
            trust.sign_bytes_via_proxy(b"x", self.url, "wrong-token")

    def test_empty_token_rejected(self):
        with self.assertRaises((ValueError, RuntimeError)):
            trust.sign_bytes_via_proxy(b"x", self.url, "")

    def test_malformed_response_raises(self):
        self.srv.malformed = True
        with self.assertRaises(RuntimeError):
            trust.sign_bytes_via_proxy(b"x", self.url, "gate-token")

    def test_proxy_pubkey_keyid(self):
        kid, pub = trust.proxy_pubkey(self.url, "gate-token")
        self.assertEqual(kid, trust.key_id(self.priv.public_key()))
        self.assertEqual(trust._pub_bytes(pub),
                         trust._pub_bytes(self.priv.public_key()))

    def test_sign_manifest_via_proxy_writes_verifiable_sig(self):
        d = tempfile.mkdtemp()
        try:
            mp = os.path.join(d, "common.json")
            payload = b'{"packages":[],"build_id":"b1"}'
            with open(mp, "wb") as fh:
                fh.write(payload)
            sp = trust.sign_manifest_via_proxy(mp, self.url, "gate-token")
            with open(sp) as fh:
                env = json.load(fh)
            trusted = {trust.key_id(self.priv.public_key()): self.priv.public_key()}
            self.assertEqual(trust.verify_bytes(payload, env, trusted),
                             trust.key_id(self.priv.public_key()))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
