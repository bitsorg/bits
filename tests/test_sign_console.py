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
        # The backend no longer returns approve_url; the CLI builds it itself.
        return {"request_id": "r1", "digest": self.digest, "groups": ["lcg"]}

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

    def test_builds_approve_url_from_console(self):
        captured = {}
        results = iter([{"status": "signed",
                         "envelope": {"alg": "ed25519", "key_id": "k", "sig": "s"},
                         "signed_by": "a", "groups": ["lcg"]}])
        with patch.object(sign_console, "_post", lambda url, data, ctype="": self._req()), \
             patch.object(sign_console, "_get", lambda url: next(results)), \
             patch.object(sign_console, "_print_qr", lambda u: captured.__setitem__("u", u)), \
             patch.object(sign_console.trust, "load_trusted_keys", lambda: {}), \
             patch.object(sign_console.trust, "verify_bytes", lambda b, e, t: "kid"), \
             patch.object(sign_console.time, "sleep", lambda s: None):
            sign_console.sign_via_console("https://bits.cern.ch/", self.mp, timeout=10)
        self.assertEqual(captured["u"], "https://bits.cern.ch/approve?approve=r1")

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



class TestPreapproveViaConsole(unittest.TestCase):
    BOMS = [{"build_id": "rel-0123456789ab", "effective_architecture": "a", "packages": []}]

    @staticmethod
    def _call(post, res):
        # POSTs go to *post*; GETs (polls) take the next (code, body) — a bare dict is a 200.
        def call(url, obj=None):
            if obj is not None:
                return post(url, obj)
            r = next(res)
            return r if isinstance(r, tuple) else (200, r)
        return call

    def _run(self, post, results):
        shown = {}
        res = iter(results)
        with patch.object(sign_console, "_json_call", self._call(post, res)), \
             patch.object(sign_console, "_print_qr", lambda u: shown.__setitem__("url", u)), \
             patch.object(sign_console.time, "sleep", lambda s: None):
            out = sign_console.preapprove_via_console("https://bits.cern.ch/", "rel-0123456789ab",
                                                      ["lcg"], self.BOMS, timeout=10)
        return out, shown

    def test_waits_for_approval_and_builds_link(self):
        sent = {}
        def post(url, obj):
            sent.update(url=url, obj=obj)
            return 200, {"request_id": "r1", "code": "K7Q2MX", "groups": ["lcg"], "packages": 3}
        out, shown = self._run(post, [{"status": "pending"},
                                      {"status": "approved", "approved_by": "alice"}])
        self.assertEqual(out, "alice")
        self.assertEqual(shown["url"], "https://bits.cern.ch/approve?preapprove=r1")
        self.assertEqual(sent["url"], "https://bits.cern.ch/preapprove/cli/request")
        self.assertEqual((sent["obj"]["build_id"], sent["obj"]["groups"], sent["obj"]["boms"]),
                         ("rel-0123456789ab", ["lcg"], self.BOMS))

    def test_already_preapproved_continues(self):
        out, shown = self._run(lambda u, o: (409, {"detail": "already"}), [])
        self.assertEqual((out, shown), ("", {}))

    def test_refused_and_errors_exit(self):
        ok = lambda u, o: (200, {"request_id": "r1", "code": "C"})
        with self.assertRaises(SystemExit):
            self._run(ok, [{"status": "refused"}])
        with self.assertRaises(SystemExit):
            self._run(lambda u, o: (400, {"detail": "bad"}), [])
        with self.assertRaises(SystemExit):                  # timeout
            with patch.object(sign_console.time, "monotonic", side_effect=[0, 0, 100]):
                self._run(ok, [{"status": "pending"}] * 5)
        with self.assertRaises(SystemExit):                  # expired request
            self._run(ok, [(404, {})])

    def test_transient_errors_keep_polling(self):
        ok = lambda u, o: (200, {"request_id": "r1", "code": "C"})
        out, _ = self._run(ok, [(502, {}), (0, {}), {"status": "approving"},
                                {"status": "approved", "approved_by": "bob"}])
        self.assertEqual(out, "bob")

    def test_refuses_plain_http_console(self):
        with self.assertRaises(SystemExit):
            sign_console.preapprove_via_console("http://bits.example", "b-0123456789ab",
                                                ["lcg"], self.BOMS)
        sign_console._check_console_url("http://localhost:8080", False)   # local is fine
        sign_console._check_console_url("http://tb:8080", True)           # explicit override

if __name__ == "__main__":
    unittest.main()
