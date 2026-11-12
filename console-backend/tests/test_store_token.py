# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Store tokens: the signer reproduces bits-store's test vector byte for byte (so
bits-store's verifier, tested on those vectors, accepts what it signs) and refuses
what that verifier would refuse."""

import base64
import hashlib
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from console_backend.store_token import StoreTokenSigner, check_pattern

# bits-store testdata/tokens/vectors.json: its issuer, audience and "now", key
# "test-1" (seed: SHA-256 of a fixed string) and case get-star-and-doublestar.
ISS, AUD, NOW = "https://bits.example.org", "example-site/store", 1800000000
SEED = hashlib.sha256(b"bits-store test key 1").digest()
VECTOR = (
    "eyJhbGciOiJFZERTQSIsImtpZCI6InRlc3QtMSIsInR5cCI6ImJpdHMtc3RvcmUrand0In0."
    "eyJpc3MiOiJodHRwczovL2JpdHMuZXhhbXBsZS5vcmciLCJhdWQiOiJleGFtcGxlLXNpdGUvc3RvcmUi"
    "LCJzdWIiOiJhbGljZSIsImdycCI6ImxoY2IiLCJpYXQiOjE3OTk5OTk0MDAsImV4cCI6MTgwMDAwMzYw"
    "MCwianRpIjoidC0xIiwiY2FwIjpbeyJtIjoiR0VUIiwicCI6Ii9zdG9yZS9saGNiL3g4Nl82NC1lbDEw"
    "Ki8qKiJ9LHsibSI6IkdFVCIsInAiOiIvc291cmNlcy9hcmNoaXZlcy8qIn0seyJtIjoiUFVUIiwicCI6"
    "Ii9zdG9yZS9saGNiL3g4Nl82NC1lbDEwLWdjYzE0LW9wdC8qKiJ9XX0."
    "4G8V15yQXNG0L3Skm6CTWACTMa_Tz87eZPPtsnLGZOlS3EiW44bmyuElwhDjzpFFld0Hzo6YHJ0m6KT2Ep2oAQ")
CAPS = [("GET", "/store/lhcb/x86_64-el10*/**"), ("GET", "/sources/archives/*"),
        ("PUT", "/store/lhcb/x86_64-el10-gcc14-opt/**")]


def signer(**kw):
    return StoreTokenSigner("test-1", Ed25519PrivateKey.from_private_bytes(SEED), ISS, AUD, **kw)


class StoreTokenTest(unittest.TestCase):

    def test_reproduces_the_vector(self):
        tok = signer().sign("alice", "lhcb", CAPS, 4200, now=NOW - 600, jti="t-1")
        self.assertEqual(tok, VECTOR)

    def test_from_seed(self):
        seed = base64.urlsafe_b64encode(SEED).rstrip(b"=").decode()
        s = StoreTokenSigner.from_seed("test-1", seed, ISS, AUD)
        self.assertEqual(s.sign("alice", "lhcb", CAPS, 4200, now=NOW - 600, jti="t-1"), VECTOR)
        s = StoreTokenSigner.from_seed("test-1", seed + "\n", ISS, AUD)  # as read from a file
        self.assertEqual(s.sign("alice", "lhcb", CAPS, 4200, now=NOW - 600, jti="t-1"), VECTOR)
        for bad in (seed[:20], seed[:10] + "$$$" + seed[13:], seed[:42] + "+", b"x" * 43, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                StoreTokenSigner.from_seed("test-1", bad, ISS, AUD)

    def test_a_signer_elsewhere(self):
        key = Ed25519PrivateKey.from_private_bytes(SEED)       # the security proxy's, say
        s = StoreTokenSigner("test-1", key.sign, ISS, AUD)
        self.assertEqual(s.sign("alice", "lhcb", CAPS, 4200, now=NOW - 600, jti="t-1"), VECTOR)
        for bad in (lambda b: b"x" * 63, lambda b: "x" * 64):
            with self.assertRaises(ValueError):
                StoreTokenSigner("test-1", bad, ISS, AUD).sign("alice", "lhcb", CAPS, 60)
        with self.assertRaises(ValueError):
            StoreTokenSigner("test-1", "not a key", ISS, AUD)

    def test_claims_and_header(self):
        tok = signer().sign("alice", "lhcb", CAPS[:1], 3600)
        pub = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
        c = jwt.decode(tok, pub, algorithms=["EdDSA"], audience=AUD, issuer=ISS)
        self.assertEqual((c["exp"] - c["iat"], c["cap"]), (3600, [{"m": "GET", "p": CAPS[0][1]}]))
        self.assertEqual(jwt.get_unverified_header(tok),
                         {"alg": "EdDSA", "kid": "test-1", "typ": "bits-store+jwt"})
        # each token its own jti, for the deny-list
        self.assertNotEqual(c["jti"], jwt.decode(signer().sign("alice", "lhcb", CAPS[:1], 3600),
                                                 options={"verify_signature": False})["jti"])

    def test_build_signer(self):
        b = signer(signer="build")
        self.assertTrue(b.sign("build:123", "lhcb", CAPS, 600))
        for s, sub in ((b, "alice"), (signer(), "build:123")):
            with self.assertRaises(ValueError):
                s.sign(sub, "lhcb", CAPS, 600)

    def test_patterns(self):
        for p in ("/a/~+!@=,:._-", "/store/lhcb/x86_64-el10*/**", "/a/*b*c*d*", "/**", "/a/*"):
            check_pattern(p)
        for p in ("", "/", "a/b", "/a//b", "/a/", "/a/../b", "/./a", "/**/a", "/a/***",
                  "/a/b**", "/a/*a*b*c*d*", "/a%2Fb", "/a b", "/ä", "/" + "x" * 256, None):
            with self.assertRaises(ValueError, msg=repr(p)):
                check_pattern(p)

    def test_refusals(self):
        s = signer()
        for args in (("", "lhcb", CAPS, 60), ("alice", "", CAPS, 60), ("alice", "lhcb", [], 60),
                     ("alice", "lhcb", [("POST", "/a")], 60), ("alice", "lhcb", [("GET", "a")], 60),
                     ("alice", "lhcb", CAPS, 0), ("alice", "lhcb", CAPS, 7 * 86400 + 1),
                     ("alice", "lhcb", CAPS, 60.5),
                     ("alice", "lhcb", [("GET", "/a/" + "b" * 200 + "/%d" % i) for i in range(20)], 60)):
            with self.assertRaises(ValueError, msg=repr(args)[:80]):
                s.sign(*args)
        for kw in (dict(jti=5), dict(now=-100), dict(now=2**60), dict(now=float("inf"))):
            with self.assertRaises(ValueError, msg=repr(kw)):
                s.sign("alice", "lhcb", CAPS, 60, **kw)
        for caps in ([None], 5, [("GET",)]):
            with self.assertRaises(ValueError, msg=repr(caps)):
                s.sign("alice", "lhcb", caps, 60)
        self.assertTrue(s.sign("alice", "lhcb", CAPS, 7 * 86400))
        # a generator of capabilities is read once, not emptied by the checks
        tok = s.sign("alice", "lhcb", (c for c in CAPS), 60)
        self.assertEqual(len(jwt.decode(tok, options={"verify_signature": False})["cap"]), 3)
        with self.assertRaises(ValueError):
            s.sign("alice", "lhcb", (c for c in []), 60)
        for life in (30 * 86400, 59, 3600.0):
            with self.assertRaises(ValueError, msg=repr(life)):
                signer(max_lifetime=life)
        for bad in (dict(kid=""), dict(issuer=""), dict(signer="admin")):
            with self.assertRaises(ValueError):
                StoreTokenSigner(bad.get("kid", "k"), Ed25519PrivateKey.generate(),
                                 bad.get("issuer", ISS), AUD, signer=bad.get("signer", "user"))


if __name__ == "__main__":
    unittest.main()
