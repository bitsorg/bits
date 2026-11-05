# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin: setup code, session, Host and Origin guards, helper status, enrolment,
the stored passphrase."""

import base64
import contextlib
import hashlib
import io
import json
import os
import stat
import struct
import tempfile
import unittest

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from console_backend.admin import app as admin
from console_backend.admin import passkeys

ORIGIN = "http://localhost:8800"
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
PRIVATE = {}  # credential id (base64url) -> the fake authenticator's private key


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def registration(options, flags=UP | UV | AT, origin=ORIGIN, prf=True, cred_id=None, rp=None):
    """What a browser sends after navigator.credentials.create, attestation "none"."""
    cred_id = cred_id or os.urandom(16)
    private = PRIVATE[b64u(cred_id)] = ec.generate_private_key(ec.SECP256R1())
    nums = private.public_key().public_numbers()
    cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
    auth = (hashlib.sha256((rp or options["rp"]["id"]).encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
            + bytes(16) + struct.pack(">H", len(cred_id)) + cred_id + cose)
    client = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin})
    return {"id": b64u(cred_id), "rawId": b64u(cred_id), "type": "public-key",
            "clientExtensionResults": {"prf": {"enabled": prf}} if prf is not None else {},
            "response": {"clientDataJSON": b64u(client.encode()),
                         "attestationObject": b64u(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth}))}}


def assertion(options, cred_id, flags=UP | UV, origin=ORIGIN, counter=1):
    """What a browser sends after navigator.credentials.get."""
    auth = hashlib.sha256(options["rpId"].encode()).digest() + bytes([flags]) + struct.pack(">I", counter)
    client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
    signature = PRIVATE[cred_id].sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    return {"id": cred_id, "rawId": cred_id, "type": "public-key", "clientExtensionResults": {},
            "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth),
                         "signature": b64u(signature), "userHandle": None}}


BOX = {"iv": b64u(bytes(12)), "ct": b64u(bytes(len("right-pass-9") + 16))}


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.t, self.said = [1000.0], []
        self.state = admin.State(now=lambda: self.t[0], write=self.said.append)
        self.asked = []
        def helper(request):
            self.asked.append(request)
            if request.get("op") == "check-signer-passphrase" and request["passphrase"] != "right-pass-9":
                return {"ok": False, "error": "wrong passphrase, or not an encrypted Ed25519 key"}
            return {"ok": True, "signer": "loaded"}
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = passkeys.Store(os.path.join(self.dir.name, "passkeys.json"))
        self.client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store),
                                 base_url="http://localhost:8800")

    def login(self, code, origin=ORIGIN):
        return self.client.post("/login", content="code=" + code, follow_redirects=False,
                                headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})

    def test_the_code_signs_in_once(self):
        self.assertEqual(self.said, [self.state.code])
        self.assertNotIn("helper", self.client.get("/").text.lower())
        code = self.state.code
        r = self.login(code.lower().replace("-", " "))  # as typed, any case or spacing
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        for part in ("HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(part.lower(), cookie.lower())
        page = self.client.get("/").text
        self.assertIn("&quot;signer&quot;: &quot;loaded&quot;", page)
        self.assertEqual(self.asked, [{"op": "status"}])
        self.assertEqual(self.said, [code, self.state.code])  # used: a new one is written
        self.assertNotEqual(self.login(code).status_code, 303)

    def test_wrong_codes_lock_out(self):
        for _ in range(admin.FAILURES):
            self.assertNotEqual(self.login("AAAA-AAAA-AAAA-AAAA").status_code, 303)
        self.assertIn("Too many wrong codes", self.login(self.state.code).text)
        self.t[0] += admin.WINDOW + 1
        self.assertEqual(self.login(self.state.code).status_code, 303)
        for _ in range(admin.FAILURES):
            self.login("AAAA-AAAA-AAAA-AAAA")
        self.state.renew_code()  # a new code clears the lockout (restart)
        self.assertEqual(self.login(self.state.code).status_code, 303)

    def test_the_code_expires(self):
        old = self.state.code
        self.t[0] += admin.CODE_TTL + 1
        self.assertNotEqual(self.login(old).status_code, 303)
        self.assertNotEqual(self.state.code, old)
        self.assertEqual(len(self.said), 2)  # the new one is written
        self.assertEqual(self.login(self.state.code).status_code, 303)

    def test_sessions_expire_and_log_out(self):
        self.login(self.state.code)
        self.t[0] += admin.SESSION_TTL + 1
        self.assertIn("Each code signs in once", self.client.get("/").text)
        self.login(self.state.code)
        self.client.post("/logout", headers={"Origin": ORIGIN})
        self.assertIn("Each code signs in once", self.client.get("/").text)

    def test_other_sites_are_refused(self):
        self.assertEqual(self.login(self.state.code, origin="http://evil.example").status_code, 403)
        self.assertEqual(self.login(self.state.code, origin="http://localhost:3000").status_code, 403)
        self.assertEqual(self.client.put("/login", headers={"Origin": "http://localhost:3000"}).status_code, 403)
        self.assertEqual(self.client.post("/login", content="code=" + self.state.code).status_code, 403)
        rebound = TestClient(admin.create_app(self.state, lambda r: {}, [ORIGIN]), base_url="http://evil.example:8800")
        self.assertEqual(rebound.get("/").status_code, 421)

    def test_headers_and_health(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.json(), {"status": "ok"})
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        # no-referrer would make browsers send Origin: null on the sign-in form
        self.assertEqual(r.headers["referrer-policy"], "same-origin")
        self.assertEqual(self.login(self.state.code, origin="null").status_code, 403)
        self.assertNotIn(self.state.code, self.client.get("/").text)

    def test_bind(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "admin.sock")
            open(path, "w").close()
            with self.assertRaises(SystemExit):  # not a socket: left alone
                admin.bind(path)
            os.unlink(path)
            admin.bind(path).close()  # a stale socket is replaced
            sock = admin.bind(path)
            try:
                self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660)
            finally:
                sock.close()

    def test_write_code(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "setup-code")
            admin.write_code("ABCD-EFGH-JKLM-NPQR", path)
            with open(path) as f:
                self.assertEqual(f.read(), "ABCD-EFGH-JKLM-NPQR\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
            self.assertEqual(os.listdir(d), ["setup-code"])

    def enrol(self, label="YubiKey 5C", **fake):
        r = self.client.post("/passkeys/options", json={"label": label}, headers={"Origin": ORIGIN})
        self.assertEqual(r.status_code, 200, r.text)
        options = r.json()
        return options, self.client.post("/passkeys", json={"credential": registration(options, **fake)},
                                         headers={"Origin": ORIGIN})

    def test_a_security_key_is_enrolled(self):
        self.login(self.state.code)
        options, r = self.enrol("YubiKey <5C>")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(options["rp"]["id"], "localhost")
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "discouraged")
        [key] = self.store.load()
        self.assertEqual(key["label"], "YubiKey <5C>")
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        page = self.client.get("/").text
        self.assertIn("YubiKey &lt;5C&gt;", page)
        self.assertIn("<script src=/admin.js>", page)
        again = self.client.post("/passkeys", json={"credential": registration(options)},
                                 headers={"Origin": ORIGIN})  # the challenge is used up
        self.assertIn("expired", again.json()["error"])
        options, _ = self.enrol("second")
        self.assertEqual([c["id"] for c in options["excludeCredentials"]], [key["id"]])

    def test_keys_that_cannot_guard_the_passphrase_are_refused(self):
        self.login(self.state.code)
        for fake, said in (({"prf": False}, "no PRF"), ({"prf": None}, "no PRF"),
                           ({"flags": UP | AT}, "did not verify"),
                           ({"origin": "http://localhost:3000"}, "did not verify"),
                           ({"rp": "example.org"}, "did not verify")):
            _, r = self.enrol(**fake)
            self.assertEqual(r.status_code, 400, fake)
            self.assertIn(said, r.json()["error"], fake)
        self.assertEqual(self.store.load(), [])

    def test_synced_passkeys_unless_security_keys_only(self):
        self.login(self.state.code)
        _, r = self.enrol("Touch ID", flags=UP | UV | AT | BE | BS)
        self.assertEqual(r.json(), {"ok": True})
        [key] = self.store.load()
        self.assertEqual(key["synced"], True)
        self.assertIn("<td>synced passkey</td>", self.client.get("/").text)
        _, r = self.enrol("not yet backed up", flags=UP | UV | AT | BE)
        self.assertEqual(self.store.load()[1]["synced"], True)
        self.store.remove(self.store.load()[1]["id"])
        self.assertEqual(self.store_box(key["id"], flags=UP | UV | BE | BS).json(), {"ok": True})
        strict = TestClient(admin.create_app(self.state, lambda r: {"ok": True}, [ORIGIN], self.store,
                                             security_keys_only=True), base_url="http://localhost:8800")
        self.state.renew_code()
        strict.post("/login", content="code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("Security keys only", strict.get("/").text)
        r = strict.post("/passkeys/box-options", json={"id": key["id"]}, headers={"Origin": ORIGIN})
        self.assertIn("accepts security keys only", r.json()["error"])  # refused before the touch
        self.store.update(key["id"], synced=False)  # recorded wrongly: the touch says it is synced
        options = strict.post("/passkeys/box-options", json={"id": key["id"]}, headers={"Origin": ORIGIN}).json()
        r = strict.post("/passkeys/box", headers={"Origin": ORIGIN}, json=dict(
            BOX, passphrase="right-pass-9", credential=assertion(options, key["id"], flags=UP | UV | BE, counter=2)))
        self.assertIn("accepts security keys only", r.json()["error"])
        for flags in (UP | UV | AT | BE | BS, UP | UV | AT | BE):
            options = strict.post("/passkeys/options", json={}, headers={"Origin": ORIGIN}).json()
            r = strict.post("/passkeys", json={"credential": registration(options, flags=flags)},
                            headers={"Origin": ORIGIN})
            self.assertIn("accepts security keys only", r.json()["error"])
        options = strict.post("/passkeys/options", json={}, headers={"Origin": ORIGIN}).json()
        r = strict.post("/passkeys", json={"credential": registration(options)}, headers={"Origin": ORIGIN})
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual([k["synced"] for k in self.store.load()], [False, False])

    def test_the_setting_errs_to_strict(self):
        for value, on in ((None, False), ("", False), (" 0 ", False), ("No", False), ("false", False),
                          ("1", True), ("yes", True), ("on", True), ("true ", True), ("enabled", True)):
            self.assertEqual(admin._on(value), on, value)

    def test_the_same_key_twice_is_refused(self):
        self.login(self.state.code)
        self.enrol(cred_id=b"k" * 16)
        _, r = self.enrol(cred_id=b"k" * 16)
        self.assertIn("already enrolled", r.json()["error"])
        self.assertEqual(len(self.store.load()), 1)

    def test_a_challenge_is_for_one_session_and_five_minutes(self):
        self.login(self.state.code)
        options = self.client.post("/passkeys/options", json={}, headers={"Origin": ORIGIN}).json()
        other = TestClient(self.client.app, base_url="http://localhost:8800")
        self.state.renew_code()
        other.post("/login", content="code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        r = other.post("/passkeys", json={"credential": registration(options)}, headers={"Origin": ORIGIN})
        self.assertIn("expired", r.json()["error"])
        self.t[0] += admin.CHALLENGE_TTL + 1
        r = self.client.post("/passkeys", json={"credential": registration(options)}, headers={"Origin": ORIGIN})
        self.assertIn("expired", r.json()["error"])
        self.assertEqual(self.store.load(), [])

    def test_large_bodies_and_storage_failures(self):
        self.login(self.state.code)
        r = self.client.post("/passkeys/options", content=b"{" + b" " * 2000 + b"}",
                             headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.store.path = os.path.join(self.dir.name, "missing", "passkeys.json")
        _, r = self.enrol()
        self.assertEqual(r.status_code, 500)
        self.assertIn("could not save", r.json()["error"])

    def test_the_origins_name_the_rp_id(self):
        app = admin.create_app(self.state, lambda r: {}, ["http://bits-admin.localhost:8800"], self.store)
        client = TestClient(app, base_url="http://bits-admin.localhost:8800")
        self.state.renew_code()
        client.post("/login", content="code=" + self.state.code, headers={
            "Origin": "http://bits-admin.localhost:8800", "Content-Type": "application/x-www-form-urlencoded"})
        options = client.post("/passkeys/options", json={},
                              headers={"Origin": "http://bits-admin.localhost:8800"}).json()
        self.assertEqual(options["rp"]["id"], "bits-admin.localhost")
        r = client.post("/passkeys", json={"credential": registration(
            options, origin="http://bits-admin.localhost:8800")},
            headers={"Origin": "http://bits-admin.localhost:8800"})
        self.assertEqual(r.json(), {"ok": True})
        with self.assertRaises(ValueError):
            admin.create_app(self.state, lambda r: {}, ["http://bits-admin.localhost:8800",
                                                         "http://localhost:8800"], self.store)

    def test_a_key_is_removed(self):
        self.login(self.state.code)
        self.enrol()
        [key] = self.store.load()
        r = self.client.post("/passkeys/delete", content="id=" + key["id"], follow_redirects=False,
                             headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((r.status_code, self.store.load()), (303, []))

    def test_enrolment_needs_a_session(self):
        for path in ("/passkeys/options", "/passkeys", "/passkeys/delete"):
            self.assertEqual(self.client.post(path, json={}, headers={"Origin": ORIGIN}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)  # no Origin
        self.assertNotIn("Add key", self.client.get("/").text)

    def test_the_script_is_served_and_allowed(self):
        r = self.client.get("/admin.js")
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/javascript")
        self.assertIn("navigator.credentials.create", r.text)
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])

    def store_box(self, key_id, passphrase="right-pass-9", box=BOX, **fake):
        r = self.client.post("/passkeys/box-options", json={"id": key_id}, headers={"Origin": ORIGIN})
        if r.status_code != 200:
            return r
        options = r.json()
        self.assertEqual([c["id"] for c in options["allowCredentials"]], [key_id])
        self.assertEqual(options["userVerification"], "required")
        body = dict(box, passphrase=passphrase, credential=assertion(options, key_id, **fake))
        return self.client.post("/passkeys/box", json=body, headers={"Origin": ORIGIN})

    def test_the_passphrase_is_stored_once_checked(self):
        self.login(self.state.code)
        self.enrol()
        [key] = self.store.load()
        r = self.store_box(key["id"], passphrase="wrong-pass-7")
        self.assertIn("not stored: wrong passphrase", r.json()["error"])
        self.assertNotIn("box", self.store.get(key["id"]))
        self.assertEqual(self.store.get(key["id"])["sign_count"], 1)  # the touch counted
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r = self.store_box(key["id"], counter=2)
        self.assertEqual(r.json(), {"ok": True})
        self.assertIn("passphrase stored under key", err.getvalue())
        self.assertNotIn("right-pass-9", err.getvalue())
        stored = self.store.get(key["id"])
        self.assertEqual((stored["box"]["iv"], stored["box"]["ct"], stored["sign_count"]),
                         (BOX["iv"], BOX["ct"], 2))
        self.assertEqual(self.asked[-1], {"op": "check-signer-passphrase", "passphrase": "right-pass-9"})
        with open(self.store.path) as f:
            self.assertNotIn("right-pass-9", f.read())
        self.assertIn(stored["box"]["stored"], self.client.get("/").text)

    def test_a_bad_answer_stores_nothing(self):
        self.login(self.state.code)
        self.enrol()
        self.enrol()
        first, second = [k["id"] for k in self.store.load()]
        checks = lambda: [a for a in self.asked if a.get("op") == "check-signer-passphrase"]
        self.store.update(first, sign_count=5)
        for fake, said in (({"flags": UP, "counter": 6}, "did not verify"),  # no user verification
                           ({"origin": "http://localhost:3000", "counter": 6}, "did not verify"),
                           ({"counter": 3}, "did not verify")):  # not above the stored count
            r = self.store_box(first, **fake)
            self.assertIn(said, r.json()["error"], fake)
        options = self.client.post("/passkeys/box-options", json={"id": first}, headers={"Origin": ORIGIN}).json()
        r = self.client.post("/passkeys/box", headers={"Origin": ORIGIN}, json=dict(
            BOX, passphrase="right-pass-9", credential=assertion(options, second, counter=6)))
        self.assertIn("another key", r.json()["error"])
        for box in ({"iv": "short", "ct": BOX["ct"]}, {"iv": BOX["iv"], "ct": b64u(bytes(4))},
                    {"iv": BOX["iv"], "ct": b64u(bytes(len("right-pass-9") + 17))}):
            self.assertIn("malformed", self.store_box(first, box=box).json()["error"])
        self.assertIn("no passphrase", self.store_box(first, passphrase="").json()["error"])
        self.assertIn("1024 bytes", self.store_box(first, passphrase="é" * 600).json()["error"])
        self.assertIn("bad request", self.store_box(first, box={"iv": BOX["iv"]}).json()["error"])  # no ct
        self.assertIn("no such", self.store_box("bm9uZQ").json()["error"])
        self.assertEqual(checks(), [])  # the helper was never asked
        self.assertFalse(any("box" in k for k in self.store.load()))

    def test_an_answer_is_used_once_and_a_removed_key_gets_nothing(self):
        self.login(self.state.code)
        self.enrol()
        [key] = self.store.load()
        options = self.client.post("/passkeys/box-options", json={"id": key["id"]}, headers={"Origin": ORIGIN}).json()
        body = dict(BOX, passphrase="right-pass-9", credential=assertion(options, key["id"]))
        self.assertEqual(self.client.post("/passkeys/box", json=body, headers={"Origin": ORIGIN}).json(), {"ok": True})
        self.assertIn("expired", self.client.post("/passkeys/box", json=body, headers={"Origin": ORIGIN}).json()["error"])
        options = self.client.post("/passkeys/box-options", json={"id": key["id"]}, headers={"Origin": ORIGIN}).json()
        self.store.remove(key["id"])
        body = dict(BOX, passphrase="right-pass-9", credential=assertion(options, key["id"], counter=2))
        self.assertIn("no such", self.client.post("/passkeys/box", json=body, headers={"Origin": ORIGIN}).json()["error"])

    def test_storing_needs_a_session(self):
        for path in ("/passkeys/box-options", "/passkeys/box"):
            self.assertEqual(self.client.post(path, json={}, headers={"Origin": ORIGIN}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)

    def test_helper_unreachable(self):
        self.assertEqual(admin.ask_helper({"op": "status"}, "/nonexistent/h.sock")["ok"], False)


if __name__ == "__main__":
    unittest.main()
