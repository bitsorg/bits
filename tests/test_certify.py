# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the certification core (bits_helpers/certify.py)."""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bits_helpers import certify, trust


def _pkg(package, h, sha, arch="slc7_x86-64", **kw):
    e = {"package": package, "version": "1", "revision": "local1",
         "effective_architecture": arch, "hash": h, "tarball_sha256": sha,
         "tarball": "%s.tar.gz" % package}
    e.update(kw)
    return e


def _manifest(build_id, pkgs):
    return {"build_id": build_id, "packages": pkgs}


class TestMerge(unittest.TestCase):

    def test_dedup_by_hash_and_sorted(self):
        m1 = _manifest("b1", [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")])
        m2 = _manifest("b2", [_pkg("A", "h1", "sha256:aa"), _pkg("C", "h3", "sha256:cc")])
        common = certify.merge_common_manifest([m1, m2])
        hashes = [p["hash"] for p in common["packages"]]
        self.assertEqual(hashes, ["h1", "h2", "h3"])          # deduped + sorted
        self.assertEqual(common["sources"], ["b1", "b2"])
        self.assertEqual(common["kind"], certify.COMMON_MANIFEST_KIND)

    def test_conflicting_sha_fails_closed(self):
        m1 = _manifest("b1", [_pkg("A", "h1", "sha256:aa")])
        m2 = _manifest("b2", [_pkg("A", "h1", "sha256:DIFFERENT")])
        with self.assertRaises(certify.CertifyConflict):
            certify.merge_common_manifest([m1, m2])

    def test_same_sha_prefix_insensitive_no_conflict(self):
        # "sha256:aa" and bare "aa" describe the same bytes -> not a conflict.
        m1 = _manifest("b1", [_pkg("A", "h1", "sha256:aa")])
        m2 = _manifest("b2", [_pkg("A", "h1", "aa")])
        common = certify.merge_common_manifest([m1, m2])
        self.assertEqual(len(common["packages"]), 1)

    def test_entries_without_hash_or_sha_skipped(self):
        m = _manifest("b1", [
            _pkg("Good", "h1", "sha256:aa"),
            {"package": "NoHash", "tarball_sha256": "sha256:bb"},
            {"package": "NoSha", "hash": "h9"},
        ])
        common = certify.merge_common_manifest([m])
        self.assertEqual([p["package"] for p in common["packages"]], ["Good"])


class TestValidateAgainstStore(unittest.TestCase):

    def _common(self):
        return certify.merge_common_manifest(
            [_manifest("b1", [_pkg("A", "h1", "sha256:aa"),
                              _pkg("B", "h2", "sha256:bb")])])

    def test_all_present_and_matching_is_clean(self):
        store = {("slc7_x86-64", "h1"): "sha256:aa", ("slc7_x86-64", "h2"): "bb"}
        problems = certify.validate_against_store(
            self._common(), lambda a, h, t=None: store.get((a, h)))
        self.assertEqual(problems, [])

    def test_missing_object_reported(self):
        store = {("slc7_x86-64", "h1"): "sha256:aa"}   # h2 absent
        problems = certify.validate_against_store(
            self._common(), lambda a, h, t=None: store.get((a, h)))
        self.assertEqual(len(problems), 1)
        self.assertIn("missing from store", problems[0])

    def test_sha_mismatch_reported(self):
        store = {("slc7_x86-64", "h1"): "sha256:aa", ("slc7_x86-64", "h2"): "sha256:WRONG"}
        problems = certify.validate_against_store(
            self._common(), lambda a, h, t=None: store.get((a, h)))
        self.assertEqual(len(problems), 1)
        self.assertIn("sha256 mismatch", problems[0])


class TestAcceptsGroup(unittest.TestCase):
    """The pure group-policy predicate used by trusted_index."""

    def test_none_policy_trusts_everything(self):
        for g in (None, "", "lcg", "ship"):
            self.assertTrue(trust.accepts_group(g, None))

    def test_common_and_untagged_always_trusted(self):
        self.assertTrue(trust.accepts_group("common", ["lcg"]))
        self.assertTrue(trust.accepts_group(None, ["lcg"]))
        self.assertTrue(trust.accepts_group("", ["lcg"]))

    def test_own_group_trusted_foreign_rejected(self):
        self.assertTrue(trust.accepts_group("lcg", ["lcg"]))
        self.assertFalse(trust.accepts_group("ship", ["lcg"]))

    def test_empty_policy_still_trusts_base(self):
        self.assertTrue(trust.accepts_group("common", []))
        self.assertFalse(trust.accepts_group("lcg", []))


class TestCertifyEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Real Ed25519 keypair: private for signing, public in a trust dir.
        priv = Ed25519PrivateKey.generate()
        self.key_pem = os.path.join(self.tmp, "signing.pem")
        with open(self.key_pem, "wb") as fh:
            fh.write(priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
        self.trust_dir = os.path.join(self.tmp, "keys")
        os.makedirs(self.trust_dir)
        with open(os.path.join(self.trust_dir, "pub.pem"), "wb") as fh:
            fh.write(priv.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
        self._old_env = os.environ.get("BITS_TRUST_KEYS")
        os.environ["BITS_TRUST_KEYS"] = self.trust_dir

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("BITS_TRUST_KEYS", None)
        else:
            os.environ["BITS_TRUST_KEYS"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_certify_produces_manifest_trusted_index_accepts(self):
        m = _manifest("b1", [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")])
        store = {("slc7_x86-64", "h1"): "sha256:aa", ("slc7_x86-64", "h2"): "sha256:bb"}
        out = os.path.join(self.tmp, "out", "common.json")
        out_path, sig_path = certify.certify(
            [m], self.key_pem, out, probe=lambda a, h, t=None: store.get((a, h)))
        self.assertTrue(os.path.isfile(out_path) and os.path.isfile(sig_path))
        # The client-side trust gate must accept it and expose the reuse index.
        kid, index = trust.trusted_index(out_path)
        self.assertIsNotNone(kid)
        self.assertEqual(index, {"h1": "sha256:aa", "h2": "sha256:bb"})

    def test_certified_by_recorded_in_signed_manifest(self):
        m = _manifest("b1", [_pkg("A", "h1", "sha256:aa")])
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([m], self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa",
                        approval_check=lambda common: ["alice", "bob"])
        doc = json.load(open(out))
        self.assertEqual(doc["certified_by"], ["alice", "bob"])
        self.assertIn("certified_at", doc)
        kid, _ = trust.trusted_index(out)          # signature still valid
        self.assertIsNotNone(kid)

    def _identity_args(self, admins_text, out, **over):
        admins = os.path.join(self.tmp, "ADMINS")
        with open(admins, "w") as fh:
            fh.write(admins_text)
        base = dict(manifests=[_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
                    out=out, key=self.key_pem, certifyStore="", noStoreCheck=True,
                    workDir=self.tmp, architecture="slc7_x86-64", group=None,
                    requireApproval=True, admins=admins, validDays=None,
                    sourceCommit=None, changedGroups=None,
                    certifierToken="pat", apiUrl="https://gl/api/v4")
        base.update(over)
        return SimpleNamespace(**base)

    def test_identity_token_records_authenticated_certifier(self):
        from bits_helpers import forge
        out = os.path.join(self.tmp, "out", "common.json")
        args = self._identity_args("@alice\n", out)            # alice = overall admin

        class _P:
            def error(self, m):
                raise RuntimeError(m)

        with patch.object(forge, "gitlab_identify", return_value="alice"):
            certify.doCertify(args, _P())
        self.assertEqual(json.load(open(out))["certified_by"], ["alice"])

    def test_identity_token_rejects_unauthorised_user(self):
        from bits_helpers import forge
        out = os.path.join(self.tmp, "out", "common.json")
        args = self._identity_args("lcg @bob\n", out)          # only lcg admin bob

        class _P(Exception):
            pass

        class _Parser:
            def error(self, m):
                raise _P(m)

        with patch.object(forge, "gitlab_identify", return_value="alice"):
            with self.assertRaises(_P):
                certify.doCertify(args, _Parser())
        self.assertFalse(os.path.exists(out))

    def test_certifier_username_recorded_without_api(self):
        # A pre-authenticated identity (e.g. GITLAB_USER_LOGIN) is trusted
        # directly; no forge call is made. alice is an overall admin.
        admins = os.path.join(self.tmp, "ADMINS")
        with open(admins, "w") as fh:
            fh.write("@alice\n")
        out = os.path.join(self.tmp, "out", "common.json")
        args = SimpleNamespace(
            manifests=[_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
            out=out, key=self.key_pem, certifyStore="", noStoreCheck=True,
            workDir=self.tmp, architecture="slc7_x86-64", group=None,
            requireApproval=True, admins=admins, validDays=None, sourceCommit=None,
            changedGroups=None, certifier="alice", certifierToken=None, apiUrl=None)

        class _P:
            def error(self, m):
                raise RuntimeError(m)

        certify.doCertify(args, _P())
        self.assertEqual(json.load(open(out))["certified_by"], ["alice"])

    def test_certifier_username_rejected_when_not_admin(self):
        admins = os.path.join(self.tmp, "ADMINS")
        with open(admins, "w") as fh:
            fh.write("lcg @bob\n")            # only lcg admin bob; no overall
        out = os.path.join(self.tmp, "out", "common.json")
        args = SimpleNamespace(
            manifests=[_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
            out=out, key=self.key_pem, certifyStore="", noStoreCheck=True,
            workDir=self.tmp, architecture="slc7_x86-64", group=None,
            requireApproval=True, admins=admins, validDays=None, sourceCommit=None,
            changedGroups=None, certifier="alice", certifierToken=None, apiUrl=None)

        class _Err(Exception):
            pass

        class _P:
            def error(self, m):
                raise _Err(m)

        with self.assertRaises(_Err):
            certify.doCertify(args, _P())
        self.assertFalse(os.path.exists(out))

    def test_approval_check_failure_aborts_before_signing(self):
        def _deny(common):
            raise certify.CertifyError("not approved")
        out = os.path.join(self.tmp, "out", "common.json")
        with self.assertRaises(certify.CertifyError):
            certify.certify([_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
                            self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa",
                            approval_check=_deny)
        self.assertFalse(os.path.exists(out))

    def test_certify_refuses_when_store_validation_fails(self):
        m = _manifest("b1", [_pkg("A", "h1", "sha256:aa")])
        out = os.path.join(self.tmp, "out", "common.json")
        with self.assertRaises(certify.CertifyError):
            certify.certify([m], self.key_pem, out, probe=lambda a, h, t=None: None)
        self.assertFalse(os.path.exists(out))   # nothing signed on failure

    def test_tampering_breaks_signature(self):
        m = _manifest("b1", [_pkg("A", "h1", "sha256:aa")])
        store = {("slc7_x86-64", "h1"): "sha256:aa"}
        out = os.path.join(self.tmp, "out", "common.json")
        out_path, _ = certify.certify([m], self.key_pem, out,
                                      probe=lambda a, h, t=None: store.get((a, h)))
        data = json.load(open(out_path))
        data["packages"][0]["tarball_sha256"] = "sha256:evil"
        with open(out_path, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        kid, index = trust.trusted_index(out_path)   # fail-closed
        self.assertIsNone(kid)
        self.assertEqual(index, {})

    def test_group_filter_scopes_reuse_index(self):
        # A merged manifest spanning groups: base (untagged), lcg, ship.
        pkgs = [_pkg("Base", "hbase", "sha256:00"),                 # untagged -> base
                _pkg("Lcg", "hlcg", "sha256:aa", group="lcg"),
                _pkg("Ship", "hship", "sha256:bb", group="ship")]
        store = {("slc7_x86-64", "hbase"): "sha256:00",
                 ("slc7_x86-64", "hlcg"): "sha256:aa",
                 ("slc7_x86-64", "hship"): "sha256:bb"}
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([_manifest("b1", pkgs)], self.key_pem, out,
                        probe=lambda a, h, t=None: store.get((a, h)))
        # No policy -> everything trusted.
        _, all_idx = trust.trusted_index(out)
        self.assertEqual(set(all_idx), {"hbase", "hlcg", "hship"})
        # lcg policy -> base + lcg, ship dropped.
        _, lcg_idx = trust.trusted_index(out, accept_groups=["lcg"])
        self.assertEqual(set(lcg_idx), {"hbase", "hlcg"})

    def test_build_trusted_reuse_index_honours_trust_groups(self):
        # End-to-end through the consumer gate: --trust-groups lcg parsed and
        # applied to the verified signed manifest.
        from bits_helpers import build
        pkgs = [_pkg("Base", "hbase", "sha256:00"),
                _pkg("Lcg", "hlcg", "sha256:aa", group="lcg"),
                _pkg("Ship", "hship", "sha256:bb", group="ship")]
        store = {("slc7_x86-64", "hbase"): "sha256:00",
                 ("slc7_x86-64", "hlcg"): "sha256:aa",
                 ("slc7_x86-64", "hship"): "sha256:bb"}
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([_manifest("b1", pkgs)], self.key_pem, out,
                        probe=lambda a, h, t=None: store.get((a, h)))
        args = SimpleNamespace(trustManifest=out, trustGroups="lcg",
                               requireSignedReuse=True)
        idx = build.trusted_reuse_index(args, self.tmp)
        self.assertEqual(set(idx), {"hbase", "hlcg"})   # ship filtered out

    def test_freshness_fields_and_expiry_enforced(self):
        import datetime
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
                        self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa",
                        valid_days=30, source_commit="cafe1234")
        doc = json.load(open(out))
        self.assertEqual(doc["source_commit"], "cafe1234")
        self.assertIn("expires", doc)
        # Fresh now -> trusted.
        kid, index = trust.trusted_index(out)
        self.assertIsNotNone(kid)
        self.assertEqual(set(index), {"h1"})
        # A moment past expiry -> fail closed, even though the signature is valid.
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=31)
        kid2, index2 = trust.trusted_index(out, now=future)
        self.assertIsNone(kid2)
        self.assertEqual(index2, {})

    def test_no_expiry_by_default_never_expires(self):
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
                        self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa")
        self.assertNotIn("expires", json.load(open(out)))
        import datetime
        far = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)
        kid, index = trust.trusted_index(out, now=far)   # still trusted
        self.assertIsNotNone(kid)

    def test_certify_group_stamps_untagged_entries(self):
        out = os.path.join(self.tmp, "out", "common.json")
        certify.certify([_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
                        self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa",
                        default_group="lcg")
        entry = json.load(open(out))["packages"][0]
        self.assertEqual(entry["group"], "lcg")
        # Consumer in a different group won't trust it (not base).
        _, ship_idx = trust.trusted_index(out, accept_groups=["ship"])
        self.assertEqual(ship_idx, {})

    def test_doCertify_cli_defaults_to_workdir_manifests(self):
        # Lay two per-build BOMs under WORKDIR/MANIFESTS/<build_id>/ and certify
        # the whole directory with the CLI entrypoint (offline merge).
        man_root = os.path.join(self.tmp, "sw", "MANIFESTS")
        for bid, pkgs in (("release-b1", [_pkg("A", "h1", "sha256:aa")]),
                          ("release-b2", [_pkg("B", "h2", "sha256:bb")])):
            d = os.path.join(man_root, bid)
            os.makedirs(d)
            with open(os.path.join(d, "host-20260101T000000Z.json"), "w") as fh:
                json.dump(_manifest(bid, pkgs), fh)
        out = os.path.join(self.tmp, "out", "common.json")

        class _Parser:
            def error(self, msg):
                raise AssertionError("parser.error: %s" % msg)

        args = SimpleNamespace(manifests=[], out=out, key=self.key_pem,
                               certifyStore="", noStoreCheck=True,
                               workDir=os.path.join(self.tmp, "sw"),
                               architecture="slc7_x86-64")
        certify.doCertify(args, _Parser())
        kid, index = trust.trusted_index(out)
        self.assertIsNotNone(kid)
        self.assertEqual(index, {"h1": "sha256:aa", "h2": "sha256:bb"})


class TestIsExpired(unittest.TestCase):
    """trust.is_expired: backward-compatible, fail-closed on garbage."""

    def _now(self, y):
        import datetime
        return datetime.datetime(y, 6, 1, tzinfo=datetime.timezone.utc)

    def test_absent_expires_never_expires(self):
        self.assertFalse(trust.is_expired({}, now=self._now(2999)))
        self.assertFalse(trust.is_expired({"packages": []}, now=self._now(2999)))

    def test_future_expiry_not_expired(self):
        self.assertFalse(trust.is_expired({"expires": "2100-01-01T00:00:00Z"},
                                          now=self._now(2026)))

    def test_past_expiry_expired(self):
        self.assertTrue(trust.is_expired({"expires": "2020-01-01T00:00:00Z"},
                                         now=self._now(2026)))

    def test_unparseable_expiry_is_fail_closed(self):
        self.assertTrue(trust.is_expired({"expires": "not-a-date"}))


class TestCertifyApprovalGate(unittest.TestCase):
    """doCertify --require-approval refuses to sign without group-admin approval."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.admins = os.path.join(self.tmp, "ADMINS")
        with open(self.admins, "w") as fh:
            fh.write("@alice\n")
        self.out = os.path.join(self.tmp, "common.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self):
        return SimpleNamespace(
            manifests=[_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
            out=self.out, key="unused.pem", certifyStore="", noStoreCheck=True,
            workDir=self.tmp, architecture="slc7_x86-64", group=None,
            requireApproval=True, admins=self.admins)

    class _Parser:
        class _Err(Exception):
            pass

        def error(self, msg):
            raise self._Err(msg)

    def test_refuses_without_admin_approval(self):
        from bits_helpers import forge
        with patch.object(forge, "forge_from_env",
                          return_value=forge.StaticForge(["eve"], "proj MR !1")):
            with self.assertRaises(self._Parser._Err):
                certify.doCertify(self._args(), self._Parser())
        self.assertFalse(os.path.exists(self.out))   # never signed

    def test_refuses_when_no_forge_context(self):
        from bits_helpers import forge
        with patch.object(forge, "forge_from_env", return_value=None):
            with self.assertRaises(self._Parser._Err):
                certify.doCertify(self._args(), self._Parser())

    def test_proceeds_past_gate_when_admin_approved(self):
        # Approval passes; certify then fails on the (bogus) key — proving the
        # gate was cleared and signing was attempted.
        from bits_helpers import forge
        with patch.object(forge, "forge_from_env",
                          return_value=forge.StaticForge(["alice"], "proj MR !1")):
            with self.assertRaises(Exception) as ctx:
                certify.doCertify(self._args(), self._Parser())
        self.assertNotIsInstance(ctx.exception, self._Parser._Err)  # not the gate

    def test_negative_valid_days_rejected_before_signing(self):
        args = SimpleNamespace(
            manifests=[_manifest("b1", [_pkg("A", "h1", "sha256:aa")])],
            out=self.out, key="unused.pem", certifyStore="", noStoreCheck=True,
            workDir=self.tmp, architecture="slc7_x86-64", group=None,
            requireApproval=False, admins=None, validDays=-1, sourceCommit=None)
        with self.assertRaises(self._Parser._Err):
            certify.doCertify(args, self._Parser())
        self.assertFalse(os.path.exists(self.out))


class TestGroupFromPath(unittest.TestCase):
    """certify infers a per-entry group from the manifests/<group>/ directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, group, name, manifest):
        d = os.path.join(self.tmp, "manifests", group)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            json.dump(manifest, fh)

    def test_group_inferred_and_applied(self):
        self._write("lcg", "b.json", _manifest("b-lcg", [_pkg("L", "hl", "sha256:l")]))
        self._write("common", "b.json", _manifest("b-c", [_pkg("C", "hc", "sha256:c")]))
        mans = certify.load_build_manifests(os.path.join(self.tmp, "manifests"))
        self.assertEqual({m.get("_source_group") for m in mans}, {"lcg", "common"})
        common = certify.merge_common_manifest(mans)
        tags = {p["hash"]: p.get("group") for p in common["packages"]}
        self.assertEqual(tags, {"hl": "lcg", "hc": "common"})

    def test_explicit_entry_group_beats_directory(self):
        self._write("lcg", "b.json",
                    _manifest("b", [_pkg("X", "hx", "sha256:x", group="special")]))
        common = certify.merge_common_manifest(
            certify.load_build_manifests(os.path.join(self.tmp, "manifests")))
        self.assertEqual(common["packages"][0]["group"], "special")


class TestProbeBinding(unittest.TestCase):
    """validate_against_store must ask the probe for the manifest's named tarball."""

    def test_probe_receives_tarball_name(self):
        seen = []

        def probe(a, h, t=None):
            seen.append(t)
            return "sha256:aa"

        common = certify.merge_common_manifest(
            [_manifest("b1", [_pkg("A", "h1", "sha256:aa")])])
        self.assertEqual(certify.validate_against_store(common, probe), [])
        self.assertEqual(seen, ["A.tar.gz"])


class TestKeyPolicySemantics(unittest.TestCase):
    """trust.key_authorized: listed keys restricted, unlisted governed by default."""

    def test_no_policy_is_unrestricted(self):
        self.assertTrue(trust.key_authorized("anykey", "ship", None))

    def test_listed_key_restricted_star_is_all(self):
        p = {"k1": {"lcg"}, "k2": {"*"}}
        self.assertTrue(trust.key_authorized("k1", "lcg", p))
        self.assertFalse(trust.key_authorized("k1", "ship", p))
        self.assertTrue(trust.key_authorized("k2", "ship", p))

    def test_unlisted_key_unrestricted_without_default(self):
        self.assertTrue(trust.key_authorized("unknown", "ship", {"k1": {"lcg"}}))

    def test_default_empty_makes_strict(self):
        p = {"k1": {"lcg"}, "default": set()}
        self.assertFalse(trust.key_authorized("unknown", "ship", p))
        self.assertTrue(trust.key_authorized("k1", "lcg", p))

    def test_empty_list_for_key_denies_all(self):
        self.assertFalse(trust.key_authorized("k1", "lcg", {"k1": set()}))


class TestKeyGroupBinding(unittest.TestCase):
    """A signing key certifies only the groups its key-policy authorises."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        priv = Ed25519PrivateKey.generate()
        self.key_pem = os.path.join(self.tmp, "signing.pem")
        with open(self.key_pem, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        self.kid = trust.key_id(priv.public_key())
        self.trust_dir = os.path.join(self.tmp, "keys")
        os.makedirs(self.trust_dir)
        with open(os.path.join(self.trust_dir, "pub.pem"), "wb") as fh:
            fh.write(priv.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
        self._policy_path = os.path.join(self.trust_dir, "key-policy.json")
        self._set_policy(["lcg"])                     # authorised for lcg only
        self._old = os.environ.get("BITS_TRUST_KEYS")
        os.environ["BITS_TRUST_KEYS"] = self.trust_dir

    def _set_policy(self, groups):
        with open(self._policy_path, "w") as fh:
            json.dump({self.kid: groups}, fh)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BITS_TRUST_KEYS", None)
        else:
            os.environ["BITS_TRUST_KEYS"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sign_raw(self, entries):
        """Sign a hand-built common manifest, bypassing certify's producer check."""
        out = os.path.join(self.tmp, "common.json")
        with open(out, "w") as fh:
            json.dump({"schema_version": 1, "kind": "common-manifest",
                       "packages": entries}, fh)
        trust.sign_manifest(out, self.key_pem)
        return out

    def test_producer_refuses_unauthorised_group(self):
        out = os.path.join(self.tmp, "o.json")
        with self.assertRaises(certify.CertifyError):
            certify.certify([_manifest("b", [_pkg("A", "h1", "sha256:aa", group="ship")])],
                            self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa")

    def test_producer_allows_authorised_group(self):
        out = os.path.join(self.tmp, "o.json")
        certify.certify([_manifest("b", [_pkg("A", "h1", "sha256:aa", group="lcg")])],
                        self.key_pem, out, probe=lambda a, h, t=None: "sha256:aa")
        _, index = trust.trusted_index(out)
        self.assertEqual(set(index), {"h1"})

    def test_consumer_drops_entries_key_not_authorised_for(self):
        out = self._sign_raw([
            {"hash": "h1", "tarball_sha256": "sha256:aa", "group": "lcg"},
            {"hash": "h2", "tarball_sha256": "sha256:bb", "group": "ship"},
            {"hash": "h3", "tarball_sha256": "sha256:cc"},          # untagged -> common
        ])
        _, index = trust.trusted_index(out)   # even with no group filter
        self.assertEqual(set(index), {"h1"})  # ship + common dropped by key policy

    def test_overall_star_key_certifies_any_group(self):
        self._set_policy(["*"])
        out = self._sign_raw([
            {"hash": "h1", "tarball_sha256": "sha256:aa", "group": "lcg"},
            {"hash": "h2", "tarball_sha256": "sha256:bb", "group": "ship"},
        ])
        _, index = trust.trusted_index(out)
        self.assertEqual(set(index), {"h1", "h2"})


if __name__ == "__main__":
    unittest.main()
