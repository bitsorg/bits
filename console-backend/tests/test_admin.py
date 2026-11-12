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
import re
import stat
import struct
import tempfile
import time
import unittest
from urllib.parse import urlencode

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from console_backend.admin import app as admin
from console_backend.admin import events, passkeys

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
        self.asked, self.signer = [], "loaded"

        def helper(request):
            self.asked.append(request)
            op, right = request.get("op"), request.get("passphrase") == "right-pass-9"
            if op in ("check-signer-passphrase", "unlock-signer") and not right:
                return {"ok": False, "error": "wrong passphrase, or not an encrypted Ed25519 key"}
            if op == "unlock-signer":
                self.signer = "loaded"
                return {"ok": True, "key_id16": "43fd71705f29a075"}
            if op in ("secrets-list", "secret-set", "secret-remove"):
                return ({"ok": True, "slots": ["gitlab-ops", "gitlab-read"], "pushed": {}} if right else
                        {"ok": False, "error": "wrong passphrase, or secrets/service-secrets.enc is damaged"})
            return {"ok": True, "signer": self.signer}
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = passkeys.Store(os.path.join(self.dir.name, "passkeys.json"))
        self.helper, self.reset = helper, os.path.join(self.dir.name, "reset")
        self.chain = events.Chain(os.path.join(self.dir.name, "events.jsonl"), now_ms=lambda: int(self.t[0] * 1000))
        self.client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store,
                                                  reset_file=self.reset, chain=self.chain),
                                 base_url="http://localhost:8800")

    def login(self, code, origin=ORIGIN):
        return self.client.post("/login", content="name=Predrag&code=" + code, follow_redirects=False,
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

    def test_the_name_comes_with_the_code(self):
        code = self.state.code
        r = self.client.post("/login", content="code=" + code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("Give your name", r.text)
        self.assertEqual(self.state.code, code)  # not used up
        self.assertEqual(self.login(code).status_code, 303)
        self.assertIn("Signed in as Predrag (setup code)", self.client.get("/").text)

    def test_keys_have_owners(self):
        self.store.add({"id": "b2xk", "public_key": "", "sign_count": 0, "label": "before owners",
                        "added": "2026-11-05 23:18 UTC"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.login(self.state.code)
            self.enrol("Touch ID")
        old, new = self.store.load()
        self.assertEqual((old["owner"], new["owner"]), ("Predrag", "Predrag"))
        self.assertIn("Predrag: 1 key(s) without an owner are now theirs", err.getvalue())
        self.assertIn("Predrag: key %s (Touch ID, device-bound) enrolled" % new["id"][:8], err.getvalue())
        self.assertIn("<td>Predrag</td>", self.client.get("/").text)
        self.state.renew_code()
        other = TestClient(self.client.app, base_url="http://localhost:8800")
        other.post("/login", content="name=Someone&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual([k["owner"] for k in self.store.load()], ["Predrag", "Predrag"])  # not taken

    def test_names_are_cleaned(self):
        code = self.state.code
        for blank in ("%20%20", "%0A%1B"):
            r = self.client.post("/login", content="name=%s&code=%s" % (blank, code), headers={
                "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
            self.assertIn("Give your name", r.text)
        self.assertEqual(self.state.code, code)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.client.post("/login", content="name=%3Cscript%3Ex%0A%1B%5B31m&code=" + code, headers={
                "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("Signed in as &lt;script&gt;x[31m", self.client.get("/").text)
        self.assertIn("bits-admin: <script>x[31m: signed in", err.getvalue())
        self.assertNotIn("\x1b", err.getvalue())

    def key_signin(self, client, cred_id, **fake):
        options = client.post("/signin/options", json={}, headers={"Origin": ORIGIN}).json()
        body = {"ticket": options.get("ticket", ""), "credential": assertion(options, cred_id, **fake)}
        return options, client.post("/signin", json=body, headers={"Origin": ORIGIN})

    def test_sign_in_with_a_key(self):
        self.login(self.state.code)
        self.enrol("YubiKey 5C")
        [key] = self.store.load()
        fresh = TestClient(self.client.app, base_url="http://localhost:8800")
        self.assertIn("Sign in with your key", fresh.get("/").text)
        options, r = self.key_signin(fresh, key["id"], counter=7)
        self.assertEqual([c["id"] for c in options["allowCredentials"]], [key["id"]])
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual(r.json(), {"ok": True})
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        self.assertIn("Signed in as Predrag (key YubiKey 5C)", fresh.get("/").text)
        self.assertEqual(self.store.get(key["id"])["sign_count"], 7)
        replay = fresh.post("/signin", headers={"Origin": ORIGIN}, json={
            "ticket": options["ticket"], "credential": assertion(options, key["id"], counter=8)})
        self.assertIn("expired", replay.json()["error"])

    def test_a_bad_sign_in_opens_no_session(self):
        self.login(self.state.code)
        self.enrol("YubiKey 5C")
        self.enrol("ownerless")
        key, ownerless = self.store.load()
        self.store.update(ownerless["id"], owner="")
        for fake, said in (({"flags": UP, "counter": 2}, "did not verify"),
                           ({"origin": "http://localhost:3000", "counter": 2}, "did not verify"),
                           ({"counter": 0}, "did not verify")):
            fresh = TestClient(self.client.app, base_url="http://localhost:8800")
            self.store.update(key["id"], sign_count=1)
            options, r = self.key_signin(fresh, key["id"], **fake)
            self.assertIn(said, r.json()["error"], fake)
            self.assertNotIn("set-cookie", r.headers)
            self.assertNotIn("Signed in as", fresh.get("/").text)
        retry = fresh.post("/signin", headers={"Origin": ORIGIN}, json={
            "ticket": options["ticket"], "credential": assertion(options, key["id"], counter=9)})
        self.assertIn("expired", retry.json()["error"])  # a failed answer used the ticket up
        self.assertEqual(self.store.get(key["id"])["sign_count"], 1)
        self.assertNotIn(ownerless["id"], [c["id"] for c in options["allowCredentials"]])
        _, r = self.key_signin(fresh, ownerless["id"], counter=3)
        self.assertIn("not enrolled here", r.json()["error"])
        r = fresh.post("/signin", json={"ticket": "made-up", "credential": {"id": key["id"]}},
                       headers={"Origin": ORIGIN})
        self.assertIn("expired", r.json()["error"])
        self.assertEqual(fresh.post("/signin", json={"credential": "x"}, headers={"Origin": ORIGIN}).json(),
                         {"error": "bad request"})
        self.assertEqual(fresh.post("/signin/options", json={}).status_code, 403)  # no Origin

    def test_security_keys_only_signs_in_no_synced_passkey(self):
        self.login(self.state.code)
        self.enrol("Touch ID", flags=UP | UV | AT | BE | BS)
        [key] = self.store.load()
        strict = TestClient(admin.create_app(self.state, lambda r: {"ok": True}, [ORIGIN], self.store,
                                             security_keys_only=True), base_url="http://localhost:8800")
        self.assertNotIn("Sign in with your key", strict.get("/").text)
        r = strict.post("/signin/options", json={}, headers={"Origin": ORIGIN})
        self.assertIn("no key can sign in", r.json()["error"])
        self.enrol("YubiKey")  # with a security key there, the synced one still cannot sign in
        options, r = self.key_signin(strict, key["id"], flags=UP | UV, counter=2)
        self.assertIn("accepts security keys only", r.json()["error"])

    def test_abandoned_challenges_are_dropped(self):
        for n in range(50):
            self.state.put_challenge("ticket%d" % n, "signin", b"c")
        self.t[0] += admin.CHALLENGE_TTL - 10
        self.state.put_challenge("session", "box", b"c")
        self.t[0] += 11
        self.state.put_challenge("last", "signin", b"c")
        self.assertEqual(sorted(self.state.challenges), [("last", "signin"), ("session", "box")])

    def test_sign_ins_in_progress_are_capped(self):
        self.login(self.state.code)
        self.enrol()
        for _ in range(admin.SIGNINS):
            self.assertIn("ticket", self.client.post("/signin/options", json={}, headers={"Origin": ORIGIN}).json())
        r = self.client.post("/signin/options", json={}, headers={"Origin": ORIGIN})
        self.assertEqual(r.status_code, 429)
        self.t[0] += admin.CHALLENGE_TTL + 1
        self.assertIn("ticket", self.client.post("/signin/options", json={}, headers={"Origin": ORIGIN}).json())

    def code_login(self, client):
        self.state.renew_code()
        code = self.state.code
        r = client.post("/login", content="name=Someone&code=" + code, follow_redirects=False, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        return r, code

    def test_the_code_is_off_once_an_admin_has_a_key(self):
        self.login(self.state.code)
        self.enrol()
        fresh = TestClient(self.client.app, base_url="http://localhost:8800")
        page = fresh.get("/").text
        self.assertIn("sudo make admin-reset", page)
        self.assertNotIn("action=/login", page)
        r, code = self.code_login(fresh)
        self.assertIn("The setup code is off", r.text)
        self.assertEqual(self.state.code, code)  # not used up

    def test_a_reset_opens_the_code_for_a_while(self):
        self.login(self.state.code)
        self.enrol()
        open(self.reset, "w").close()
        app = admin.create_app(self.state, self.helper, [ORIGIN], self.store, reset_file=self.reset,
                               reset_owner=os.getuid())
        self.assertFalse(os.path.exists(self.reset))  # used once
        client = TestClient(app, base_url="http://localhost:8800")
        self.assertIn("action=/login", client.get("/").text)
        r, _ = self.code_login(client)
        self.assertEqual(r.status_code, 303)
        self.t[0] += admin.CODE_TTL + 1
        r, _ = self.code_login(TestClient(app, base_url="http://localhost:8800"))
        self.assertIn("The setup code is off", r.text)
        os.mkdir(self.reset)  # cannot be removed: no reset, not one at every start
        app = admin.create_app(self.state, self.helper, [ORIGIN], self.store, reset_file=self.reset,
                               reset_owner=os.getuid())
        r, _ = self.code_login(TestClient(app, base_url="http://localhost:8800"))
        self.assertIn("The setup code is off", r.text)
        os.rmdir(self.reset)

    def test_only_a_fresh_reset_file_of_root_counts(self):
        self.login(self.state.code)
        self.enrol()
        for owner, age in ((os.getuid() + 1, 0), (os.getuid(), admin.RESET_MAX_AGE + 60)):
            open(self.reset, "w").close()
            os.utime(self.reset, (time.time() - age, time.time() - age))
            app = admin.create_app(self.state, self.helper, [ORIGIN], self.store, reset_file=self.reset,
                                   reset_owner=owner)
            self.assertFalse(os.path.exists(self.reset))  # removed all the same
            r, _ = self.code_login(TestClient(app, base_url="http://localhost:8800"))
            self.assertIn("The setup code is off", r.text, (owner, age))

    def test_the_code_stays_while_no_key_can_sign_in(self):
        self.login(self.state.code)
        self.enrol("Touch ID", flags=UP | UV | AT | BE | BS)
        strict = TestClient(admin.create_app(self.state, self.helper, [ORIGIN], self.store, security_keys_only=True,
                                             reset_file=self.reset), base_url="http://localhost:8800")
        r, _ = self.code_login(strict)
        self.assertEqual(r.status_code, 303)
        self.enrol("YubiKey")  # an owned security key: the code is off, strict or not
        r, _ = self.code_login(TestClient(strict.app, base_url="http://localhost:8800"))
        self.assertIn("The setup code is off", r.text)

    def invite(self, name="Alice"):
        r = self.client.post("/invites", content="name=" + name, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        codes = [line for line in r.text.split("<pre>")[1:]]
        return r, codes[0].split("</pre>")[0] if codes else None

    def use_invite(self, client, code):
        return client.post("/invite", content="code=" + code, follow_redirects=False, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})

    def test_an_invite_adds_one_admin(self):
        self.login(self.state.code)
        self.enrol("YubiKey")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r, code = self.invite("Alice")
        self.assertIn("Give Alice this invite", r.text)
        self.assertIn("Predrag: invited Alice", err.getvalue())
        self.assertNotIn(code, err.getvalue())  # never logged
        self.assertIn("Alice, invited by Predrag", self.client.get("/").text)
        alice = TestClient(self.client.app, base_url="http://localhost:8800")
        self.assertIn("Invited?", alice.get("/").text)
        self.assertEqual(self.use_invite(alice, code.lower()).status_code, 303)
        page = alice.get("/").text
        self.assertIn("Welcome, Alice", page)
        self.assertNotIn("Host helper", page)
        for path in ("/passkeys/box-options", "/unlock/options", "/invites", "/invites/cancel", "/passkeys/delete"):
            self.assertEqual(alice.post(path, json={}, headers={"Origin": ORIGIN}).status_code, 401, path)
        options = alice.post("/passkeys/options", json={"label": "Alice's key"}, headers={"Origin": ORIGIN}).json()
        r = alice.post("/passkeys", json={"credential": registration(options)}, headers={"Origin": ORIGIN})
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual([k["owner"] for k in self.store.load()], ["Predrag", "Alice"])
        self.assertIn("Sign in with your key", alice.get("/").text)  # the invite's session is over
        again = alice.post("/passkeys/options", json={}, headers={"Origin": ORIGIN})
        self.assertEqual(again.status_code, 401)  # one key per invite
        self.assertNotIn("Alice, invited", self.client.get("/").text)
        self.assertIn("Wrong, used or expired invite", self.use_invite(
            TestClient(self.client.app, base_url="http://localhost:8800"), code).text)

    def test_an_admin_is_removed_with_their_keys(self):
        self.login(self.state.code)
        self.enrol("YubiKey")
        _, code = self.invite("Alice")
        alice = TestClient(self.client.app, base_url="http://localhost:8800")
        self.use_invite(alice, code)
        options = alice.post("/passkeys/options", json={}, headers={"Origin": ORIGIN}).json()
        alice.post("/passkeys", json={"credential": registration(options)}, headers={"Origin": ORIGIN})
        [mine, hers] = self.store.load()
        self.key_signin(alice, hers["id"], counter=2)
        self.assertIn("Signed in as Alice", alice.get("/").text)
        remove = lambda client, n: client.post("/admins/remove", content="name=" + n, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("cannot remove yourself", remove(alice, "Alice").text)
        self.assertIn("No such admin", remove(self.client, "Nobody").text)
        invite = lambda n: alice.post("/invites", content="name=" + n, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        invite("Mallory")                  # Alice's invites: one open, one used
        r = invite("Eve")
        eve = TestClient(self.client.app, base_url="http://localhost:8800")
        self.use_invite(eve, r.text.split("<pre>")[1].split("</pre>")[0])
        self.assertEqual(remove(eve, "Predrag").status_code, 401)  # an invite's session cannot remove
        page = self.client.get("/").text
        self.assertIn("Alice (1 key) <form method=post action=/admins/remove", page)
        self.assertNotIn("Predrag (1 key) <form", page)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(remove(self.client, "Alice").status_code, 200)  # followed to the page
        self.assertIn("Predrag: removed admin Alice (1 key)", err.getvalue())
        self.assertEqual([k["owner"] for k in self.store.load()], ["Predrag"])
        self.assertNotIn("Signed in as Alice", alice.get("/").text)  # her session ended too
        self.assertIn("Signed in as Predrag", self.client.get("/").text)
        self.assertNotIn("Welcome, Eve", eve.get("/").text)  # her invites went with her
        self.assertNotIn("Mallory, invited by", self.client.get("/").text)
        self.assertEqual(alice.post("/signin/options", json={}, headers={"Origin": ORIGIN}).json()[
            "allowCredentials"], [{"id": mine["id"], "type": "public-key"}])

    def test_invites_expire_and_are_checked(self):
        self.login(self.state.code)
        self.enrol("YubiKey")
        self.assertIn("not an admin's", self.invite("Predrag")[0].text)
        self.assertIn("not an admin's", self.invite("%20")[0].text)
        _, code = self.invite("Alice")
        self.assertIn("invited already", self.invite("Alice")[0].text)
        _, bob = self.invite("Bob")
        bobs = TestClient(self.client.app, base_url="http://localhost:8800")
        self.use_invite(bobs, bob)
        self.t[0] += admin.INVITE_TTL + 1
        self.assertIn("Wrong, used or expired invite", self.use_invite(
            TestClient(self.client.app, base_url="http://localhost:8800"), code).text)
        self.assertNotIn("Alice, invited", self.client.get("/").text)
        self.assertNotIn("Welcome, Bob", bobs.get("/").text)  # an invite's session lasts an hour

    def test_an_invite_is_cancelled_and_wrong_ones_count(self):
        self.login(self.state.code)
        self.enrol("YubiKey")
        _, code = self.invite("Alice")
        cancel = lambda n: self.client.post("/invites/cancel", content="name=" + n, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        cancel("Alice")
        other = TestClient(self.client.app, base_url="http://localhost:8800")
        self.assertIn("Wrong, used or expired", self.use_invite(other, code).text)
        _, code = self.invite("Carol")
        carol = TestClient(self.client.app, base_url="http://localhost:8800")
        self.use_invite(carol, code)  # used, no key yet: the name stays taken
        self.assertIn("invited already", self.invite("Carol")[0].text)
        cancel("Carol")  # ends that session too, e.g. if the invite leaked
        self.assertNotIn("Welcome, Carol", carol.get("/").text)
        self.assertIn("this invite", self.invite("Carol")[0].text)
        for _ in range(admin.FAILURES):
            self.use_invite(other, "AAAA-AAAA-AAAA-AAAA")
        _, code = self.invite("Bob")
        self.assertIn("Too many wrong codes", self.use_invite(other, code).text)

    def test_what_admins_do_is_chained(self):
        key_id = self.ready_to_unlock()
        self.unlock(key_id, counter=2)
        _, code = self.invite("Alice")
        self.client.post("/invites/cancel", content="name=Alice", headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        rows = self.chain.rows()
        self.assertEqual([(r["kind"], r["actor_id"]) for r in rows], [
            ("admin.signed_in", "Predrag"), ("admin.key_added", "Predrag"),
            ("admin.passphrase_stored", "Predrag"), ("admin.signer_unlocked", "Predrag"),
            ("admin.invited", "Predrag"), ("admin.invite_cancelled", "Predrag")])
        self.assertEqual(json.loads(rows[3]["payload"]), {"key": key_id, "key_id16": "43fd71705f29a075"})
        with open(self.chain.path) as f:
            text = f.read()
        for secret in ("right-pass-9", code, BOX["ct"]):
            self.assertNotIn(secret, text)
        self.assertEqual(self.chain.verify(), (6, None))
        self.assertIn("admin.signer_unlocked", self.client.get("/").text)
        self.assertEqual(stat.S_IMODE(os.stat(self.chain.path).st_mode), 0o600)

    def test_a_changed_event_breaks_the_chain(self):
        for n in range(3):
            self.chain.append("admin.signed_in", "Predrag", "Predrag", {"how": "key"})
        with open(self.chain.path) as f:
            lines = f.readlines()
        lines[1] = lines[1].replace('"Predrag"', '"Mallory"', 1)
        with open(self.chain.path, "w") as f:
            f.writelines(lines)
        self.assertEqual(events.Chain(self.chain.path).verify(), (2, 2))
        with open(self.chain.path, "w") as f:
            f.writelines([lines[0], lines[2]])  # one removed
        self.assertEqual(events.Chain(self.chain.path).verify(), (2, 2))
        with open(self.chain.path, "a") as f:
            f.write("not json\n")
        self.assertEqual(events.Chain(self.chain.path).verify(), (2, 2))

    def test_the_chain_continues_after_a_restart_and_a_torn_write(self):
        for n in range(2):
            self.chain.append("admin.signed_in", "Predrag", "Predrag")
        with open(self.chain.path, "a") as f:
            f.write('{"seq":3,"ts_')  # a crash mid-write
        again = events.Chain(self.chain.path)
        self.assertEqual(again.verify(), (2, None))  # the torn tail is not an event
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(again.append("admin.signed_in", "Predrag", "Predrag")["seq"], 3)
        self.assertIn("cutting off a torn last line", err.getvalue())
        self.assertEqual(events.Chain(self.chain.path).verify(), (3, None))

    def test_a_failed_sync_does_not_fork_the_chain(self):
        self.chain.append("admin.signed_in", "Predrag", "Predrag")
        real = os.fsync
        os.fsync = lambda fd: (_ for _ in ()).throw(OSError("disk"))
        try:
            with self.assertRaises(OSError):
                self.chain.append("admin.signed_in", "Predrag", "Predrag")  # written, not synced
        finally:
            os.fsync = real
        self.assertEqual(self.chain.append("admin.signed_in", "Predrag", "Predrag")["seq"], 3)
        self.assertEqual(self.chain.verify(), (3, None))

    def test_a_failed_record_does_not_stop_the_action(self):
        self.chain.path = os.path.join(self.dir.name, "missing", "events.jsonl")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.login(self.state.code).status_code, 303)
        self.assertIn("cannot record admin.signed_in", err.getvalue())

    def test_the_copy_in_audit_log_follows(self):
        held, posts = [], []

        def remote(method, path, body=None):  # audit-log's rules, in short
            if method == "GET":
                return 200, {"seq": len(held), "hash": held[-1]["hash"] if held else events.GENESIS}
            posts.append(len(body["events"]))
            for r in body["events"]:
                if r["seq"] != len(held) + 1 or r["prev_hash"] != (held[-1]["hash"] if held else events.GENESIS):
                    return 409, {"error": "does not link"}
                held.append(r)
            return 200, {"head": {"seq": len(held)}}
        for n in range(5):
            self.chain.append("admin.signed_in", "Predrag", "Predrag")
        outside = events.Outside(self.chain, "http://audit-log:8443", "t", call=remote, batch=2)
        self.assertEqual(outside.sync()["state"], "in step")
        self.assertEqual((held, posts), (self.chain.rows(), [2, 2, 1]))
        self.assertEqual({k: v for k, v in outside.sync().items() if k != "checked"},
                         {"state": "in step", "seq": 5, "error": ""})
        self.assertEqual(posts, [2, 2, 1])  # nothing new, nothing sent
        held[4] = dict(held[4], hash="f" * 64)  # its copy is not ours
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.chain.append("admin.signed_in", "Predrag", "Predrag")
            self.assertEqual(outside.sync()["state"], "diverged")
        self.assertIn("audit-log copy: diverged: its event 5 is not this one", err.getvalue())
        self.assertEqual(len(held), 5)  # never written to then
        held.extend([held[0]] * 3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(outside.sync()["error"], "it holds 8 events, here 6")

    def test_the_copy_waits_when_audit_log_is_away(self):
        def away(method, path, body=None):
            raise OSError("connection refused")
        self.chain.append("admin.signed_in", "Predrag", "Predrag")
        outside = events.Outside(self.chain, "http://audit-log:8443", "t", call=away)
        with contextlib.redirect_stderr(io.StringIO()):
            got = outside.sync()
        self.assertEqual((got["state"], got["seq"], got["error"]), ("unreachable", None, "connection refused"))
        refusing = lambda method, path, body=None: (200, {"seq": 0, "hash": events.GENESIS}) \
            if method == "GET" else (409, {"error": "expected seq 1"})
        outside.call = refusing
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(outside.sync()["state"], "diverged")

    def test_hostile_answers_do_not_stop_or_spin_it(self):
        for n in range(3):
            self.chain.append("admin.signed_in", "Predrag", "Predrag")
        cases = [
            (lambda m, p, b=None: (200, {"seq": -1, "hash": "x"}), "diverged"),
            (lambda m, p, b=None: (200, {"seq": "2", "hash": "x"}), "diverged"),
            (lambda m, p, b=None: (200, {"seq": 0, "hash": events.GENESIS}) if m == "GET"
             else (200, {"head": {"seq": 0}}), "diverged"),                     # does not advance
            (lambda m, p, b=None: (500, ["not", "an", "object"]), "unreachable"),
            (lambda m, p, b=None: (200, {"seq": 0, "hash": events.GENESIS}) if m == "GET"
             else (409, {"error": "no\nbits-admin: forged line"}), "diverged"),
        ]
        for call, state in cases:
            outside = events.Outside(self.chain, "http://audit-log:8443", "t", call=call)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(outside.sync()["state"], state)
            self.assertNotIn("\nbits-admin: forged", err.getvalue())
        boom = events.Outside(self.chain, "http://audit-log:8443", "t",
                              call=lambda m, p, b=None: (_ for _ in ()).throw(RuntimeError("odd")))
        boom.wake.wait = lambda timeout: (_ for _ in ()).throw(SystemExit)  # one pass only
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            boom.run()
        self.assertEqual((boom.status["state"], boom.status["error"]), ("unreachable", "RuntimeError: odd"))

    def test_the_copy_is_set_up_from_the_environment(self):
        token = os.path.join(self.dir.name, "token")
        env = {"BITS_ADMIN_AUDIT_URL": "", "BITS_ADMIN_AUDIT_TOKEN_FILE": token}
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            self.assertIsNone(events.outside_from_env(self.chain))
            os.environ["BITS_ADMIN_AUDIT_URL"] = "http://audit-log:8443/"
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(events.outside_from_env(self.chain))
            self.assertIn("make audit-token", err.getvalue())
            with open(token, "w") as f:
                f.write("t0k\n")
            outside = events.outside_from_env(self.chain)
            self.assertEqual((outside.base, outside.token), ("http://audit-log:8443/v1/chains/bits-admin", "t0k"))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_the_page_shows_the_copy(self):
        outside = events.Outside(self.chain, "http://audit-log:8443", "t",
                                 call=lambda m, p, b=None: (200, {"seq": 0, "hash": events.GENESIS}))
        client = TestClient(admin.create_app(self.state, self.helper, [ORIGIN], self.store, reset_file=self.reset,
                                             chain=self.chain, outside=outside), base_url="http://localhost:8800")
        client.post("/login", content="name=Predrag&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertTrue(outside.wake.is_set())  # the sign-in woke it
        self.assertIn("Its copy in audit-log: not checked yet.", client.get("/").text)
        outside.status = {"state": "diverged", "seq": 3, "error": "x", "checked": None}
        self.assertIn("<b>DIVERGED</b> at event 3: x", client.get("/").text)
        self.state.renew_code()
        self.login(self.state.code)
        self.assertIn("Its copy in audit-log: off.", self.client.get("/").text)

    def test_the_hosts_page(self):
        answer = {"ok": True, "host": "bits<1>", "networks": {"audit-net": True, "services-net": False},
                  "services": {
                      "bits-console-backend": {"state": "healthy", "networks": ["services-net"], "profiles": [],
                                               "ports": [{"host_ip": "", "published": "8443", "target": 8443}]},
                      "victoriametrics": {"state": "running", "networks": ["monitoring-net"], "profiles": [],
                                          "ports": [{"host_ip": "127.0.0.1", "published": "8428", "target": 8428}]},
                      "security-proxy": {"state": "unhealthy", "networks": ["audit-net"], "profiles": []},
                      "audit-log": {"state": "healthy", "networks": ["audit-net"], "profiles": []},
                      "bits-admin": {"state": "not running", "network_mode": "", "profiles": ["admin"],
                                     "networks": ["admin-audit-net"]}},
                  "links": [{"service": "bits-console-backend", "key": "GITLAB_API_URL", "host": "gitlab.cern.ch",
                             "port": 443, "scheme": "https"},
                            {"service": "bits-admin", "key": "BITS_ADMIN_AUDIT_URL", "host": "audit-log",
                             "port": 8443, "scheme": "http"},
                            {"service": "bits-admin", "key": "BITS_ADMIN_ORIGINS", "host": "bits-admin.localhost",
                             "port": 8800, "scheme": "http"}],
                  "scrape": [{"job": "prepub", "host": "cvmfs-bits-01.cern.ch", "port": 8080, "labels": {}},
                             {"job": "cvmfs-prepub", "host": "host.docker.internal", "port": 8080, "labels": {}}]}
        asked = []
        def helper(request):
            asked.append(request["op"])
            return answer if request["op"] == "hosts" else {"ok": True, "signer": "no key"}
        client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store, reset_file=self.reset,
                                             chain=self.chain), base_url="http://localhost:8800")
        r = client.get("/hosts", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"], asked), (303, "/", []))  # signed in only
        client.post("/login", content="name=Predrag&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        answer["up"] = {"targets": [
            {"job": "prepub", "instance": "cvmfs-bits-01.cern.ch:8080", "up": 0, "last_up": 1762000000.0},
            {"job": "cvmfs-prepub", "instance": "relabelled", "up": 1, "last_up": None}]}
        page = client.get("/hosts").text
        self.assertIn("<span class=bad-s>down</span> (last answered 2025-11-01 12:26 UTC)", page)
        self.assertIn('<details class="card bad"><summary>cvmfs-bits-01.cern.ch</summary>', page)
        self.assertIn("monitoring job cvmfs-prepub &middot; <span class=ok-s>up</span>", page)  # by its job
        self.assertIn("not monitored", page)  # gitlab.cern.ch: nobody scrapes it
        answer["scrape"].append({"job": "prepub", "host": "bits-build-01.cern.ch", "port": 8080, "labels": {}})
        page = client.get("/hosts").text  # prepub has two hosts now: one target is not the other's
        self.assertIn('<details class="card "><summary>bits-build-01.cern.ch</summary><p class=tag>reached from '
                      'bits&lt;1&gt;; no data from monitoring', page)
        answer["up"]["targets"][0]["last_up"] = float("nan")
        self.assertIn("down</span> (not in the past day)", client.get("/hosts").text)
        answer["up"] = {"targets": []}
        self.assertIn("monitoring has no targets now", client.get("/hosts").text)
        answer["up"] = {"error": "VictoriaMetrics did not answer (OSError)"}
        self.assertIn("Health of other hosts: VictoriaMetrics did not answer", client.get("/hosts").text)
        del answer["up"]
        page = client.get("/hosts").text
        self.assertIn('<meta http-equiv=refresh content="60;url=/hosts">', page)
        self.assertIn("bits&lt;1&gt;", page)
        self.assertNotIn("bits<1>", page)
        self.assertIn("8443&rarr;8443 published", page)
        self.assertIn("8428&rarr;8428 localhost", page)
        self.assertIn("audit-net (internal)", page)
        self.assertIn("<b>no key</b>: unlock on the main page", page)
        self.assertIn('class="card wide bad" open', page)  # the unhealthy signer turns its host red
        self.assertIn("<td>8443/tcp</td><td>clients of bits-console-backend</td>", page)
        self.assertIn("<td>22/tcp</td>", page)
        self.assertIn("gitlab.cern.ch:443", page)
        self.assertIn("<summary>cvmfs-bits-01.cern.ch</summary>", page)
        self.assertIn("monitoring job cvmfs-prepub", page)
        self.assertIn("<td>bits-admin</td><td>&rarr; audit-log:8443</td>", page)
        self.assertIn("(profile admin)", page)
        self.assertNotIn("bits-admin.localhost", page)  # the tunnel's laptop end is no host to reach
        self.assertNotIn("<summary>audit-log</summary>", page)  # a service here, not another host
        self.assertIn("<b>Also on this host, outside Docker</b>", page)
        evil = "<x onload=1>"
        answer = {"ok": True, "host": evil, "networks": {evil: True},
                  "services": {evil: {"state": evil, "networks": [evil], "profiles": [evil],
                                      "ports": [{"host_ip": evil, "published": evil, "target": evil,
                                                 "protocol": evil}, {"published": None}, "odd"]},
                               "plain": {"state": None, "ports": [{"host_ip": "10.0.0.5", "published": 8080,
                                                                   "target": 80}]}},
                  "links": [{"service": evil, "key": evil, "host": evil, "port": evil}, {"host": None},
                            {"service": "plain", "key": "K", "host": "ext.example", "port": None},
                            {"service": "plain", "key": "K", "host": "host.docker.internal", "port": 9000}],
                  "scrape": [{"job": evil, "host": "other.example", "port": evil}, {"host": "localhost", "port": 1},
                             "odd"]}
        page = client.get("/hosts").text
        self.assertNotIn("<x", page)
        self.assertIn("8080/tcp (on 10.0.0.5)", page)   # bound to one address: still to open
        self.assertIn("<td>9000</td>", page)            # host.docker.internal is this host
        answer = {"ok": False, "error": "docker compose config failed; see the helper's journal"}
        self.assertIn("could not say: docker compose config failed", client.get("/hosts").text)

    def test_the_hosts_check(self):
        answer = {"ok": True, "host": "bits", "services": {}, "networks": {},
                  "links": [{"service": "b", "key": "GITLAB_API_URL", "host": "gitlab.cern.ch", "port": 443}],
                  "scrape": []}
        results = [{"target": "gitlab.cern.ch:443", "probe": "TCP connect", "ok": False, "said": "refused", "ms": 3},
                   {"target": "127.0.0.1:8443", "probe": "GET /healthz", "ok": True, "said": "<b>HTTP 200", "ms": 9}]
        asked = []
        def helper(request):
            asked.append(request["op"])
            if request["op"] == "check":
                return {"ok": True, "results": results, "at": 1762000000}
            return answer if request["op"] == "hosts" else {"ok": True}
        client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store, reset_file=self.reset,
                                             chain=self.chain), base_url="http://localhost:8800")
        self.assertEqual(client.post("/hosts/check", headers={"Origin": ORIGIN}).status_code, 401)
        self.assertNotIn("check", asked)
        client.post("/login", content="name=Predrag&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("Check the connections", client.get("/hosts").text)
        self.assertNotIn("<h2>Check,", client.get("/hosts").text)
        page = client.post("/hosts/check", headers={"Origin": ORIGIN}).text
        self.assertIn("<h2>Check, 12:26:40 UTC</h2>", page)  # when the probes ran
        self.assertIn("<td>127.0.0.1:8443</td><td>GET /healthz</td><td class=ok-s>&lt;b&gt;HTTP 200</td>", page)
        self.assertIn("check: <span class=bad-s>refused</span>", page)  # on the connection too
        self.assertEqual(client.post("/hosts/check").status_code, 403)  # no Origin

    def test_the_settings_page(self):
        evil = "<x onload=1>"
        answer = {"ok": True, "facts": {"gitlab_url": "https://gitlab.example.org"},
                  "settings": [
                      {"key": "VM_RETENTION", "value": "24", "origin": "set", "default": "13", "set": True,
                       "services": ["victoriametrics"], "section": "monitoring", "doc": "Retention in months."},
                      {"key": "BITS_GITHUB_TOKEN", "secret": True, "set": False, "origin": "unset", "value": None},
                      {"key": "BITS_OIDC_ISSUER", "value": "https://other.example", "origin": "overridden",
                       "derived": "https://gitlab.example.org", "services": ["bits-console-backend"]},
                      {"key": "BITS_CATALOG_TTL", "origin": "default", "default": "300", "value": None},
                      {"key": "HOOK_URL", "origin": "withheld", "value": None, "set": True},
                      {"key": evil, "value": evil, "origin": evil, "doc": evil, "section": evil, "services": [evil]}],
                  "profile": {"facts": {"gitlab_url": "https://gitlab.example.org"},
                              "overrides": {"BITS_OIDC_ISSUER": "https://other.example"}, "env": {"VM_RETENTION": "24"}},
                  "profile_file": {"diffs": ["differs: BITS_OIDC_ISSUER"], "notes": []}, "withheld": ["HOOK_URL"]}
        helper = lambda request: answer if request["op"] == "settings" else {"ok": True}
        client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store, reset_file=self.reset,
                                             chain=self.chain), base_url="http://localhost:8800")
        for path in ("/settings", "/settings/site-profile.yaml"):
            self.assertEqual(client.get(path, follow_redirects=False).status_code, 303)
        client.post("/login", content="name=Predrag&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("<a href=/settings>Settings</a>", client.get("/").text)
        page = client.get("/settings").text
        self.assertNotIn("<x", page)
        self.assertIn('<td class=k>VM_RETENTION</td><td><input name="set:VM_RETENTION" value="24" '
                      'placeholder="default 13" size=38></td><td class="o-set">set in .env</td>', page)
        self.assertNotIn('name="set:BITS_GITHUB_TOKEN"', page)  # secrets and withheld: not here
        self.assertNotIn('name="set:HOOK_URL"', page)
        self.assertIn('<td class=k>HOOK_URL</td><td><span class=sec>withheld</span>', page)
        self.assertIn("<span class=sec>secret: missing</span>", page)
        self.assertIn("the facts imply <code>https://gitlab.example.org</code>; empty follows them", page)
        self.assertIn('placeholder="default 300"', page)
        self.assertIn("<li>differs: BITS_OIDC_ISSUER</li>", page)
        answer["settings"][0]["doc"] = "word " * 60
        self.assertIn("<details><summary>word word", client.get("/settings").text)
        r = client.get("/settings/site-profile.yaml")
        self.assertEqual(r.headers["content-disposition"], "attachment; filename=site-profile.yaml")
        import yaml
        self.assertEqual(yaml.safe_load(r.text), answer["profile"])
        self.assertTrue(r.text.startswith("# site-profile.yaml of this bits-services site"))
        self.assertIn("set them by hand:\n#   HOOK_URL\n", r.text)

    def settings_client(self):
        answer = {"ok": True, "base": "h0", "facts": {"gitlab_url": "https://gitlab.example.org"}, "settings": [
            {"key": "VM_RETENTION", "value": "24", "origin": "set", "default": "13"},
            {"key": "BITS_CATALOG_TTL", "value": None, "origin": "default", "default": "300"},
            {"key": "OLD_KEY", "value": "x", "origin": "set"},
            {"key": "BITS_OIDC_ISSUER", "value": "https://other.example", "origin": "overridden",
             "derived": "https://gitlab.example.org"},
            {"key": "BITS_GITHUB_TOKEN", "secret": True, "set": True, "origin": "set", "value": None}]}
        self.asked, self.base = [], "h0"

        def helper(request):
            self.asked.append(request)
            if request["op"] == "settings":
                return answer
            if request["op"] == "settings-plan":
                return {"ok": True, "base": self.base, "diff": [{"key": "VM_RETENTION", "old": "24", "new": "30"}]}
            if request["op"] == "settings-apply":
                return {"ok": True, "written": True, "backup": ".env.bak-1", "keys": ["VM_RETENTION"],
                        "profile": "site-profile.yaml" if "profile" in request["changes"] else None}
            return {"ok": True}
        client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store, reset_file=self.reset,
                                             chain=self.chain), base_url="http://localhost:8800")
        self.client = client
        self.login(self.state.code)
        return client

    def test_settings_are_planned_from_the_form(self):
        client = self.settings_client()
        page = client.post("/settings/plan", content="base=h9&set:VM_RETENTION=30", headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text
        self.assertIn("changed since you opened this page", page)
        self.assertEqual([a for a in self.asked if a["op"] == "settings-plan"], [])
        self.assertIn('name=base value="h0"', client.get("/settings").text)
        self.base = "h2"  # .env changed between the page's question and the plan's
        page = client.post("/settings/plan", content="base=h0&set:VM_RETENTION=30", headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text
        self.assertIn("changed since you opened this page", page)
        self.base = "h0"
        form = ("base=h0&fact:gitlab_url=https://gl.example.net&set:VM_RETENTION=30&set:BITS_CATALOG_TTL="
                "&set:OLD_KEY=&set:BITS_OIDC_ISSUER=&set:BITS_GITHUB_TOKEN=leak")
        page = client.post("/settings/plan", content=form, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text
        planned = [a for a in self.asked if a["op"] == "settings-plan"][-1]["changes"]
        self.assertEqual(planned, {"facts": {"gitlab_url": "https://gl.example.net"},
                                   "set": {"VM_RETENTION": "30"}, "unset": ["OLD_KEY"],
                                   "follow": ["BITS_OIDC_ISSUER"]})  # a secret is not taken from the form
        self.assertIn("<td class=k>VM_RETENTION</td><td><code>24</code></td><td><code>30</code></td>", page)
        self.assertRegex(page, "<button id=apply data-plan=\"[0-9a-f]{16}\">Apply with your key</button>")
        self.assertIn("comments and the order of its lines are not kept", page)

    def test_settings_apply_needs_your_own_key(self):
        client = self.settings_client()
        self.enrol("mine")
        [mine] = self.store.load()
        post = lambda path, body: client.post(path, json=body, headers={"Origin": ORIGIN}).json()
        plan = lambda form: re.search("data-plan=\"([0-9a-f]+)\"", client.post("/settings/plan", content=form, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text).group(1)
        self.assertIn("expired", post("/settings/apply", {"credential": {"id": mine["id"]}})["error"])  # no plan
        first, latest = plan("base=h0&set:VM_RETENTION=31"), plan("base=h0&set:VM_RETENTION=30")  # two tabs
        options = post("/settings/apply/options", {})
        self.assertEqual([c["id"] for c in options["allowCredentials"]], [mine["id"]])
        self.assertIn("not your latest plan", post("/settings/apply", {
            "credential": assertion(options, mine["id"], counter=4), "plan": first})["error"])
        # someone else's key's answer does not confirm my plan, nor use it up
        options = post("/settings/apply/options", {})
        self.store.update(mine["id"], owner="Alice")
        self.assertIn("your own keys", post("/settings/apply", {
            "credential": assertion(options, mine["id"], counter=5), "plan": latest})["error"])
        self.store.update(mine["id"], owner=mine["owner"])
        options = post("/settings/apply/options", {})
        done = post("/settings/apply", {"credential": assertion(options, mine["id"], counter=5), "plan": latest})
        self.assertEqual(done, {"ok": True, "keys": ["VM_RETENTION"], "backup": ".env.bak-1", "profile": None,
                                "profile_error": None})
        applied = [a for a in self.asked if a["op"] == "settings-apply"]
        self.assertEqual(applied, [{"op": "settings-apply", "base": "h0", "changes": {
            "facts": {}, "set": {"VM_RETENTION": "30"}, "unset": [], "follow": []}}])
        self.assertEqual(self.chain.rows()[-1]["kind"], "admin.settings_applied")
        options = post("/settings/apply/options", {})
        self.assertIn("expired", post("/settings/apply", {
            "credential": assertion(options, mine["id"], counter=6), "plan": latest})["error"])
        # someone else's key does not confirm my plan
        self.store.update(mine["id"], owner="Alice")
        plan("base=h0&set:VM_RETENTION=30")
        self.assertIn("no key to confirm with", post("/settings/apply/options", {})["error"])

    def test_a_site_profile_is_planned_and_applied(self):
        client = self.settings_client()
        self.enrol("mine")
        [mine] = self.store.load()
        form = lambda text: client.post("/settings/plan", content="base=h0&" + urlencode({"profile": text}), headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text
        self.assertIn("Nothing to load", form(" \r\n"))
        page = client.get("/settings").text
        self.assertLess(page.index("id=profile-file"), page.index("<h2>Facts"))  # near the top
        self.assertLess(page.index("<button>Plan</button>", page.index("</details>")), page.index("<h2>Facts"))
        self.assertEqual(page.count("<script src=/admin.js>"), 1)
        page = form("# ours\r\nenv:\r\n  VM_RETENTION: '30'\r\n")
        planned = [a for a in self.asked if a["op"] == "settings-plan"]
        self.assertEqual([p["changes"] for p in planned], [{"profile": "# ours\nenv:\n  VM_RETENTION: '30'\n"}])
        self.assertIn("the settings it does not name go", page)
        plan_id = re.search("data-plan=\"([0-9a-f]+)\"", page).group(1)
        post = lambda path, body: client.post(path, json=body, headers={"Origin": ORIGIN}).json()
        options = post("/settings/apply/options", {})
        done = post("/settings/apply", {"credential": assertion(options, mine["id"], counter=5), "plan": plan_id})
        self.assertEqual(done["profile"], "site-profile.yaml")
        row = self.chain.rows()[-1]
        self.assertEqual((row["kind"], json.loads(row["payload"])["profile_sha256"]), ("admin.settings_applied",
                         hashlib.sha256(b"# ours\nenv:\n  VM_RETENTION: '30'\n").hexdigest()))

    def store_client(self, config=None, console_key="C" * 43):
        self.asked, self.base, self.plan_base = [], "s0", None
        config = config if config is not None else {
            "issuer": "https://bits-store.testbed", "audience": "testbed/store", "keys": {},
            "volumes": {"v": {"endpoint": "http://minio:9000", "bucket": "b", "region": "us-east-1",
                              "access_key_env": "BITS_STORE_V_AK", "secret_key_env": "BITS_STORE_V_SK"}},
            "bindings": [{"prefix": "/store", "volume": "v", "key_prefix": "TARS"}]}

        def helper(request):
            self.asked.append(request)
            if request["op"] == "store":
                return {"ok": True, "config": config or None, "path": "config/bits-store/config.json", "base": self.base,
                        "credentials": ["BITS_STORE_V_AK"], "console_key": console_key}
            if request["op"] == "store-plan":
                if request["config"].get("issuer") == "refused":
                    return {"ok": False, "error": "bits-store refuses it: issuer"}
                return {"ok": True, "base": self.plan_base or self.base, "diff": ["--- config.json", "+  \"console-1\": {"],
                        "credentials": {n: "will set" for n in request["credentials"]},
                        "restart": bool(request["credentials"])}
            if request["op"] == "store-apply":
                return {"ok": True, "written": True, "backup": "config.json.bak-1",
                        "credentials": sorted(request["credentials"]), "restart": bool(request["credentials"])}
            return {"ok": True}
        self.client = TestClient(admin.create_app(self.state, helper, [ORIGIN], self.store, reset_file=self.reset,
                                                  chain=self.chain), base_url="http://localhost:8800")
        self.login(self.state.code)
        return self.client

    def test_the_store_page(self):
        page = self.store_client().get("/store").text
        self.assertIn('name=base value="s0"', page)
        self.assertIn("&quot;bucket&quot;: &quot;b&quot;", page)                 # the config, as JSON
        self.assertIn("<td class=k>BITS_STORE_V_AK</td><td>set</td>", page)
        self.assertIn("<td class=k>BITS_STORE_V_SK</td><td><b>missing</b></td>", page)
        self.assertIn("name=trust_console value=1 checked", page)
        self.assertIn("did not answer", self.store_client(console_key=None).get("/store").text)
        trusted = {"x": "C" * 43, "signer": "user"}
        page = self.store_client(config={"keys": {"console-1": trusted}, "volumes": {}, "bindings": []}).get("/store").text
        self.assertIn("The store trusts the console", page)
        self.assertIn("this is a template", self.store_client(config=False).get("/store").text)

    def test_the_store_is_planned_and_applied_with_your_key(self):
        client = self.store_client()
        self.enrol("mine")
        [mine] = self.store.load()
        config = json.loads(re.search(r"<textarea[^>]*>(.*?)</textarea>", client.get("/store").text, re.S)
                            .group(1).replace("&quot;", '"'))
        form = lambda **f: client.post("/store/plan", content=urlencode(f), headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).text
        self.assertIn("changed since you opened", form(base="s9", config=json.dumps(config)))
        self.plan_base = "s2"                  # config.json changed between the page's question and the plan's
        self.assertIn("changed since you opened", form(base="s0", config=json.dumps(config)))
        self.plan_base = None
        form(base="s0", config=json.dumps(config))                      # trust_console not ticked
        self.assertEqual([a for a in self.asked if a["op"] == "store-plan"][-1]["config"]["keys"], {})
        self.assertIn("not JSON", form(base="s0", config="{"))
        self.assertIn("bits-store refuses it: issuer", form(base="s0", config=json.dumps(dict(config, issuer="refused"))))
        page = form(**{"base": "s0", "config": json.dumps(config), "trust_console": "1",
                       "key:BITS_STORE_V_SK": "secret-value", "key:BITS_STORE_V_AK": "",
                       "new_key_name": "BITS_STORE_W_AK", "new_key_value": "w"})
        planned = [a for a in self.asked if a["op"] == "store-plan"][-1]
        self.assertEqual(planned["config"]["keys"], {"console-1": {"x": "C" * 43, "signer": "user"}})
        self.assertEqual(planned["credentials"], {"BITS_STORE_V_SK": "secret-value", "BITS_STORE_W_AK": "w"})
        self.assertNotIn("secret-value", page)
        self.assertIn("make store-up", page)
        plan_id = re.search('data-plan="([0-9a-f]+)"', page).group(1)
        self.assertIn('data-path=/store', page)
        post = lambda path, body: client.post(path, json=body, headers={"Origin": ORIGIN}).json()
        options = post("/settings/apply/options", {})                  # a store plan is not a settings one
        self.assertIn("expired", post("/settings/apply", {
            "credential": assertion(options, mine["id"], counter=3), "plan": plan_id})["error"])
        options = post("/store/apply/options", {})
        self.assertIn("not your latest plan", post("/store/apply", {
            "credential": assertion(options, mine["id"], counter=4), "plan": "0" * 16})["error"])
        options = post("/store/apply/options", {})
        done = post("/store/apply", {"credential": assertion(options, mine["id"], counter=5), "plan": plan_id})
        self.assertEqual(done, {"ok": True, "written": True, "backup": "config.json.bak-1",
                                "credentials": ["BITS_STORE_V_SK", "BITS_STORE_W_AK"], "restart": True})
        applied = [a for a in self.asked if a["op"] == "store-apply"]
        self.assertEqual((len(applied), applied[0]["base"], applied[0]["credentials"]["BITS_STORE_V_SK"]),
                         (1, "s0", "secret-value"))
        row = self.chain.rows()[-1]
        self.assertEqual(row["kind"], "admin.store_applied")
        self.assertNotIn("secret-value", json.dumps(row))
        options = post("/store/apply/options", {})                     # used up
        self.assertIn("expired", post("/store/apply", {
            "credential": assertion(options, mine["id"], counter=6), "plan": plan_id})["error"])

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
        self.assertEqual(self.client.post("/login", content="name=Predrag&code=" + self.state.code).status_code, 403)
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
        strict.post("/login", content="name=Predrag&code=" + self.state.code, headers={
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
        other.post("/login", content="name=Predrag&code=" + self.state.code, headers={
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
        client.post("/login", content="name=Predrag&code=" + self.state.code, headers={
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

    def ready_to_unlock(self):
        self.login(self.state.code)
        self.enrol()
        [key] = self.store.load()
        self.store_box(key["id"])
        self.signer = "no key"
        return key["id"]

    def unlock(self, key_id, passphrase="right-pass-9", **fake):
        options = self.client.post("/unlock/options", json={"id": key_id}, headers={"Origin": ORIGIN}).json()
        if "error" in options:
            return options, None
        box = self.client.post("/unlock/box", json={"credential": assertion(options, key_id, **fake)},
                               headers={"Origin": ORIGIN}).json()
        if "error" in box:
            return box, None
        return box, self.client.post("/unlock", json={"passphrase": passphrase}, headers={"Origin": ORIGIN}).json()

    def test_unlock_with_a_touch(self):
        key_id = self.ready_to_unlock()
        page = self.client.get("/").text
        self.assertIn("Unlock the signer", page)
        box, done = self.unlock(key_id, counter=2)
        stored = self.store.get(key_id)["box"]
        self.assertEqual(box, {"iv": stored["iv"], "ct": stored["ct"]})
        self.assertEqual(done, {"ok": True, "key_id16": "43fd71705f29a075"})
        self.assertEqual(self.asked[-1], {"op": "unlock-signer", "passphrase": "right-pass-9"})
        self.assertEqual(self.store.get(key_id)["sign_count"], 2)
        self.assertNotIn("Unlock the signer", self.client.get("/").text)  # loaded now

    def test_unlock_needs_a_fresh_verified_touch(self):
        key_id = self.ready_to_unlock()
        unlocks = lambda: [a for a in self.asked if a.get("op") == "unlock-signer"]
        r = self.client.post("/unlock", json={"passphrase": "right-pass-9"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # no touch at all
        for fake in ({"flags": UP, "counter": 2}, {"origin": "http://localhost:3000", "counter": 2},
                     {"counter": 0}):
            box, _ = self.unlock(key_id, **fake)
            self.assertIn("did not verify", box["error"], fake)               # no box handed out
        box, done = self.unlock(key_id, passphrase="wrong-pass-7", counter=3)
        self.assertIn("not unlocked: wrong passphrase", done["error"])
        r = self.client.post("/unlock", json={"passphrase": "right-pass-9"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # the grant was used
        options = self.client.post("/unlock/options", json={"id": key_id}, headers={"Origin": ORIGIN}).json()
        answer = {"credential": assertion(options, key_id, counter=4)}
        self.assertIn("ct", self.client.post("/unlock/box", json=answer, headers={"Origin": ORIGIN}).json())
        replay = self.client.post("/unlock/box", json=answer, headers={"Origin": ORIGIN}).json()
        self.assertIn("expired", replay["error"])                             # one box per touch
        r = self.client.post("/unlock", json={"passphrase": ""}, headers={"Origin": ORIGIN})
        self.assertIn("bad request", r.json()["error"])                       # keeps the grant...
        self.t[0] += admin.GRANT_TTL + 1
        r = self.client.post("/unlock", json={"passphrase": "right-pass-9"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # ...for a minute at most
        self.assertEqual([u["passphrase"] for u in unlocks()], ["wrong-pass-7"])

    def secrets_step(self, key_id, action, counter, **fields):
        post = lambda path, body: self.client.post(path, json=body, headers={"Origin": ORIGIN}).json()
        options = post("/secrets/options", {"id": key_id})
        if "error" in options:
            return options
        box = post("/secrets/box", {"credential": assertion(options, key_id, counter=counter)})
        if "error" in box:
            return box
        return post("/secrets", dict({"passphrase": "right-pass-9", "action": action}, **fields))

    def secrets_box(self, key_id, counter):
        options = self.client.post("/secrets/options", json={"id": key_id}, headers={"Origin": ORIGIN}).json()
        return self.client.post("/secrets/box", json={"credential": assertion(options, key_id, counter=counter)},
                                headers={"Origin": ORIGIN}).json()

    def test_service_secrets_with_a_touch(self):
        key_id = self.ready_to_unlock()
        self.assertIn("<a href=/secrets>Secrets</a>", self.client.get("/").text)
        self.assertIn("<select id=secrets-key>", self.client.get("/secrets").text)
        done = self.secrets_step(key_id, "set", 2, slot="gitlab-ops", value="glpat-o")
        self.assertEqual(done, {"ok": True, "slots": ["gitlab-ops", "gitlab-read"], "push_error": None})
        self.assertEqual(self.asked[-1], {"op": "secret-set", "passphrase": "right-pass-9", "slot": "gitlab-ops",
                                          "value": "glpat-o"})
        row = self.chain.rows()[-1]
        self.assertEqual((row["kind"], json.loads(row["payload"])["slot"]), ("admin.secret_set", "gitlab-ops"))
        self.assertNotIn("glpat-o", json.dumps(self.chain.rows()))            # names only
        self.assertEqual(self.secrets_step(key_id, "list", 3)["slots"], ["gitlab-ops", "gitlab-read"])
        self.assertEqual(self.asked[-1], {"op": "secrets-list", "passphrase": "right-pass-9"})
        r = self.client.post("/secrets", json={"passphrase": "right-pass-9", "action": "list"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # one step per touch
        self.assertIn("bad request", self.secrets_step(key_id, "drop", 4)["error"])
        r = self.client.post("/secrets", json={"passphrase": "right-pass-9", "action": "list"}, headers={"Origin": ORIGIN})
        self.assertTrue(r.json()["ok"])                                        # a bad request kept the grant
        for fields, said in (({"slot": "gitlab-read\n", "value": "x"}, "a slot is a name"),
                             ({"slot": "gitlab-read", "value": "a\nb"}, "one line")):
            r = self.client.post("/secrets", json=dict(fields, passphrase="right-pass-9", action="set"),
                                 headers={"Origin": ORIGIN})
            self.assertIn(said, r.json()["error"])                             # before the grant is used
        unlock_options = self.client.post("/unlock/options", json={"id": key_id}, headers={"Origin": ORIGIN}).json()
        self.assertIn("ct", self.client.post("/unlock/box", json={"credential": assertion(
            unlock_options, key_id, counter=5)}, headers={"Origin": ORIGIN}).json())       # an unlock grant now
        r = self.client.post("/secrets", json={"passphrase": "right-pass-9", "action": "list"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # an unlock grant is not one
        self.assertEqual([a["op"] for a in self.asked if a.get("op") == "secrets-list"], ["secrets-list"] * 2)
        self.client.post("/unlock", json={"passphrase": "right-pass-9"}, headers={"Origin": ORIGIN})
        self.assertIn("ct", self.secrets_box(key_id, 6))
        r = self.client.post("/unlock", json={"passphrase": "right-pass-9"}, headers={"Origin": ORIGIN})
        self.assertIn("touch the key first", r.json()["error"])               # nor the reverse
        self.client.post("/secrets", json={"passphrase": "right-pass-9", "action": "list"}, headers={"Origin": ORIGIN})
        self.assertIn("wrong passphrase", self.secrets_step(key_id, "remove", 7, slot="gitlab-ops",
                                                            passphrase="wrong-pass-7")["error"])
        self.assertEqual([r["kind"] for r in self.chain.rows() if r["kind"].startswith("admin.secret")],
                         ["admin.secret_set"])                                 # nothing removed
        owner = self.store.get(key_id)["owner"]
        self.store.update(key_id, owner="Alice")                               # someone else's key
        self.assertIn("your own keys", self.secrets_step(key_id, "list", 8)["error"])
        self.assertIn("None of your keys holds the passphrase", self.client.get("/secrets").text)
        self.store.update(key_id, owner=owner)

    def test_unlock_only_keys_with_a_stored_passphrase(self):
        self.login(self.state.code)
        self.enrol()
        [key] = self.store.load()
        self.signer = "no key"
        page = self.client.get("/").text
        self.assertIn("No key can unlock yet", page)
        self.assertNotIn("<form id=unlock>", page)
        self.assertIn("<code>make unlock</code> asks for", page)
        options, _ = self.unlock(key["id"])
        self.assertIn("no passphrase is stored", options["error"])
        for path in ("/unlock/options", "/unlock/box", "/unlock", "/secrets/options", "/secrets/box", "/secrets"):
            self.assertEqual(TestClient(self.client.app, base_url="http://localhost:8800").post(
                path, json={}, headers={"Origin": ORIGIN}).status_code, 401)

    def test_security_keys_only_refuses_a_synced_unlock(self):
        self.login(self.state.code)
        self.enrol("Touch ID", flags=UP | UV | AT | BE | BS)
        [key] = self.store.load()
        self.store_box(key["id"], flags=UP | UV | BE | BS)
        strict = TestClient(admin.create_app(self.state, lambda r: {"ok": True, "signer": "no key"}, [ORIGIN],
                                             self.store, security_keys_only=True), base_url="http://localhost:8800")
        self.state.renew_code()
        strict.post("/login", content="name=Predrag&code=" + self.state.code, headers={
            "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
        page = strict.get("/").text
        self.assertNotIn("<form id=unlock>", page)
        self.assertIn("Only security keys may unlock here", page)
        r = strict.post("/unlock/options", json={"id": key["id"]}, headers={"Origin": ORIGIN})
        self.assertIn("accepts security keys only", r.json()["error"])

    def test_storing_needs_a_session(self):
        for path in ("/passkeys/box-options", "/passkeys/box"):
            self.assertEqual(self.client.post(path, json={}, headers={"Origin": ORIGIN}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)

    def test_helper_unreachable(self):
        self.assertEqual(admin.ask_helper({"op": "status"}, "/nonexistent/h.sock")["ok"], False)


if __name__ == "__main__":
    unittest.main()
