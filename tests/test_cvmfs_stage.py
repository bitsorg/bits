# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the producer-side staging helper.

The catalogs here are REAL SQLite databases with the columns the walk reads, not
mocks of the reader. The bug this code exists to prevent — sending the root
catalog instead of the subtree — is a bug about which row you pick, so a test
that stubs out the picking proves nothing.

Shapes are taken from the testbed run recorded in MEASUREMENTS §21: a root
catalog covering "/", a subtree catalog covering the lease path, and a third
nested catalog that the prepare did NOT re-stage.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers.cvmfs_stage import (  # noqa: E402
    StageError, bucket_root_url, find_subtree_catalog, parse_manifest,
    parse_s3_upstream, read_catalog, repo_alias, staging_prefix, staging_url,
)


def make_catalog(root_prefix, nested=(), revision=1):
    """Build a real catalog database and return its bytes."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = sqlite3.connect(path)
        db.execute("create table properties (key text primary key, value text)")
        db.execute("create table nested_catalogs (path text, sha1 text, size integer)")
        db.execute("insert into properties values ('root_prefix', ?)", (root_prefix,))
        db.execute("insert into properties values ('revision', ?)", (str(revision),))
        for p, sha in nested:
            db.execute("insert into nested_catalogs values (?, ?, 0)", (p, sha))
        db.commit()
        db.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


H_ROOT = "a" * 40
H_SUB = "b" * 40
H_OTHER = "c" * 40
LEASE = "releases/ROOT/v6-36-04"


def tree():
    """The §21 shape: root -> {subtree, an untouched catalog}."""
    return {
        H_ROOT: make_catalog("/", nested=[("/" + LEASE, H_SUB),
                                          ("/golden/smoke", H_OTHER)]),
        H_SUB: make_catalog("/" + LEASE),
        H_OTHER: make_catalog("/golden/smoke"),
    }


def fetcher(objects, unreachable=()):
    def fetch(h):
        if h in unreachable:
            raise IOError("HTTP 404")
        return objects[h]
    return fetch


class TestFindSubtreeCatalog(unittest.TestCase):

    def test_finds_the_subtree_not_the_root(self):
        """The whole point: the answer is not the hash the manifest names."""
        got = find_subtree_catalog(H_ROOT, LEASE, fetcher(tree()))
        self.assertEqual(got, H_SUB)
        self.assertNotEqual(got, H_ROOT, "returned the ROOT catalog")

    def test_unreachable_sibling_does_not_abort_the_walk(self):
        """§21: /golden/smoke 404s from staging and 200s from the repository.

        The unreachable catalog is listed FIRST, so the walk must survive it to
        reach the subtree at all. Ordered the other way this test passes even
        when the walk aborts on the first fetch failure — which it did, and the
        negative control is what caught it.
        """
        objects = {
            H_ROOT: make_catalog("/", nested=[("/golden/smoke", H_OTHER),
                                              ("/" + LEASE, H_SUB)]),
            H_SUB: make_catalog("/" + LEASE),
            H_OTHER: make_catalog("/golden/smoke"),
        }
        got = find_subtree_catalog(H_ROOT, LEASE,
                                   fetcher(objects, unreachable={H_OTHER}))
        self.assertEqual(got, H_SUB)

    def test_lease_path_slashes_are_irrelevant(self):
        for variant in (LEASE, "/" + LEASE, LEASE + "/", "/" + LEASE + "/"):
            self.assertEqual(find_subtree_catalog(H_ROOT, variant, fetcher(tree())),
                             H_SUB, "failed for %r" % variant)

    def test_no_covering_catalog_raises_and_names_what_it_saw(self):
        """Refusing is the feature. Falling back to the root publishes a
        whole revision as a subtree, and nothing downstream would catch it."""
        with self.assertRaises(StageError) as cm:
            find_subtree_catalog(H_ROOT, "some/other/path", fetcher(tree()))
        msg = str(cm.exception)
        self.assertIn("some/other/path", msg)
        self.assertIn("graft a whole revision", msg)
        # It lists what it REACHED, which since pruning is only the branches
        # that could have contained the target — not every catalog in the
        # repository. /golden/smoke is deliberately absent.
        self.assertIn("/", msg)

    def test_deeply_nested_subtree_is_found(self):
        h_mid = "d" * 40
        objects = {
            H_ROOT: make_catalog("/", nested=[("/releases", h_mid)]),
            h_mid: make_catalog("/releases", nested=[("/" + LEASE, H_SUB)]),
            H_SUB: make_catalog("/" + LEASE),
        }
        self.assertEqual(find_subtree_catalog(H_ROOT, LEASE, fetcher(objects)), H_SUB)

    def test_cycle_does_not_hang(self):
        objects = {
            H_ROOT: make_catalog("/", nested=[("/x", H_SUB)]),
            H_SUB: make_catalog("/x", nested=[("/", H_ROOT)]),
        }
        with self.assertRaises(StageError):
            find_subtree_catalog(H_ROOT, "nowhere", fetcher(objects))

    def test_implausible_root_hash_refused_before_any_fetch(self):
        def explode(_h):
            raise AssertionError("must not fetch on a malformed hash")
        for bad in ("", "xyz", "A" * 40, "a" * 39):
            with self.assertRaises(StageError):
                find_subtree_catalog(bad, LEASE, explode)

    def test_empty_or_root_path_is_REFUSED(self):
        """The one input that disabled the guard.

        want = "/" + "".strip("/") is "/", which matches the root catalog on the
        first iteration — so an empty path returned the whole revision, silently.
        An earlier version of this file asserted that as intended behaviour.

        NEGATIVE CONTROL: remove the empty-path check and every case here
        returns H_ROOT instead of raising. Verified.
        """
        objects = {H_ROOT: make_catalog("/")}
        for bad in ("", "/", "///", None):
            with self.assertRaises(StageError, msg="path %r was accepted" % bad):
                find_subtree_catalog(H_ROOT, bad, fetcher(objects))

    def test_pruning_does_not_visit_irrelevant_branches(self):
        """Unpruned, finding a leaf cost one fetch per catalog in the repository
        — 4,201 in the case the review measured, twice per package."""
        fetched = []
        objects = {
            H_ROOT: make_catalog("/", nested=[("/" + LEASE, H_SUB),
                                              ("/golden/smoke", H_OTHER)]),
            H_SUB: make_catalog("/" + LEASE),
            H_OTHER: make_catalog("/golden/smoke"),
        }
        def counting(h):
            fetched.append(h)
            return objects[h]
        self.assertEqual(find_subtree_catalog(H_ROOT, LEASE, counting), H_SUB)
        self.assertNotIn(H_OTHER, fetched,
                         "walked into a branch that cannot contain the target")

    def test_manifest_signature_is_not_parsed_as_fields(self):
        """.cvmfspublished is text, then "--", then a binary RSA signature.
        Decoded with errors="replace", a 0x43 byte after a newline looks like a
        "C" line and silently replaced the root — 3.15% of random signatures.

        NEGATIVE CONTROL: remove the `line == "--"` break and this fails.
        """
        good = "a" * 40
        text = ("C%s\nNtest.cvmfs.io\nS2\n--\n"
                "deadbeef\nC\ufffd\ufffdgarbage\nCnot-a-hash\n" % good)
        self.assertEqual(parse_manifest(text)["root"], good)

    def test_manifest_root_must_be_a_hash(self):
        with self.assertRaises(StageError):
            parse_manifest("Cnot-a-hash\nNtest.cvmfs.io\n")


class TestParseManifest(unittest.TestCase):

    def test_reads_the_real_shape(self):
        # Verbatim from the §21 testbed run.
        text = ("C7cb0af6f3ffabb6b03aee178dfa81775e588a49d\nB57344\n"
                "Rd41d8cd98f00b204e9800998ecf8427e\nD240\nS2\nGno\nAno\n"
                "Ntest.cvmfs.io\n")
        m = parse_manifest(text)
        self.assertEqual(m["root"], "7cb0af6f3ffabb6b03aee178dfa81775e588a49d")
        self.assertEqual(m["repo"], "test.cvmfs.io")
        self.assertEqual(m["revision"], "2")

    def test_missing_root_is_an_error(self):
        with self.assertRaises(StageError):
            parse_manifest("Ntest.cvmfs.io\nS2\n")


class TestReadCatalog(unittest.TestCase):

    def test_rejects_a_non_catalog(self):
        with self.assertRaises(StageError):
            read_catalog(b"this is not a database")


class TestStagingPrefix(unittest.TestCase):

    def test_shape(self):
        self.assertEqual(staging_prefix("build07", "alice", "12345"),
                         "staging/build07/alice/12345")

    def test_folds_characters_prepub_refuses(self):
        """A login with an @ must not fail a publish."""
        got = staging_prefix("build07.cern.ch", "alice@cern.ch", "job/1")
        self.assertEqual(got, "staging/build07.cern.ch/alice-cern.ch/job-1")

    def test_empty_components_get_named_fallbacks(self):
        got = staging_prefix("", None, "")
        self.assertEqual(got, "staging/unknown-host/unknown-user/unknown-job")

    def test_never_ends_in_data(self):
        """prepub refuses a trailing 'data' segment: promotion appends /data/
        itself, so it is the likeliest producer mistake."""
        self.assertFalse(staging_prefix("h", "u", "data").endswith("/data"))

    def test_over_long_prefix_refused(self):
        with self.assertRaises(StageError):
            staging_prefix("h" * 60, "u" * 60, "j" * 60)




class TestPrepareArgv(unittest.TestCase):
    """The argv is the safety surface: -P/-H are what contact the gateway."""

    def argv(self, **kw):
        from bits_helpers.cvmfs_stage import prepare_argv
        args = dict(repo="test.cvmfs.io", lease_path="pkg/1.0",
                    tar_path="/tmp/p.tar", stage_prefix="staging/h/u/7",
                    s3_conf="/etc/cvmfs/s3/test.cvmfs.io.s3.conf",
                    stratum0_url="http://minio:9000/cvmfs/test.cvmfs.io",
                    base_root="a" * 40, manifest_out="/tmp/m")
        args.update(kw)
        return prepare_argv(**args)

    def test_never_passes_the_gateway_flags(self):
        """-P is a session token and -H a gateway key. Either one turns a
        prepare into a publish nobody asked for (MEASUREMENTS §18)."""
        a = self.argv()
        self.assertNotIn("-P", a)
        self.assertNotIn("-H", a)

    def test_carries_the_flags_the_spike_proved(self):
        a = self.argv()
        for flag in ("-u", "-c", "-t", "-b", "-r", "-w", "-o", "-K", "-N",
                     "-U", "-G", "-T", "-B", "-C"):
            self.assertIn(flag, a, "missing %s" % flag)
        self.assertEqual(a[0:2], ["cvmfs_swissknife", "ingest"])

    def test_spooler_string_names_the_staging_prefix(self):
        a = self.argv(stage_prefix="staging/build07/alice/99")
        r = a[a.index("-r") + 1]
        self.assertTrue(r.startswith("S3,"), r)
        self.assertIn("staging/build07/alice/99@", r,
                      "the alias IS the key prefix objects land under")

    def test_lease_path_is_passed_without_leading_slash(self):
        self.assertEqual(self.argv(lease_path="/pkg/1.0")[
            self.argv(lease_path="/pkg/1.0").index("-B") + 1], "pkg/1.0")

    def test_base_root_is_the_hash_it_was_given(self):
        a = self.argv(base_root="b" * 40)
        self.assertEqual(a[a.index("-b") + 1], "b" * 40)


class TestParseS3Upstream(unittest.TestCase):

    def test_reads_the_production_value_verbatim(self):
        alias, conf = parse_s3_upstream(
            "S3,/var/spool/cvmfs/bits.cern.ch/tmp,cvmfs/bits.cern.ch"
            "@/etc/cvmfs/keys/bits.cern.ch.s3.conf")
        self.assertEqual(alias, "cvmfs/bits.cern.ch")
        self.assertEqual(conf, "/etc/cvmfs/keys/bits.cern.ch.s3.conf")

    def test_testbed_single_segment_alias(self):
        alias, conf = parse_s3_upstream(
            "S3,/var/spool/cvmfs/test.cvmfs.io/tmp,test.cvmfs.io"
            "@/etc/cvmfs/s3/test.cvmfs.io.s3.conf")
        self.assertEqual(alias, "test.cvmfs.io")
        self.assertEqual(conf, "/etc/cvmfs/s3/test.cvmfs.io.s3.conf")

    def test_quoted_value(self):
        alias, _ = parse_s3_upstream("'S3,/tmp,cvmfs/bits.cern.ch@/etc/x.conf'")
        self.assertEqual(alias, "cvmfs/bits.cern.ch")

    def test_non_s3_upstream_is_refused(self):
        """A local or gateway upstream has no bucket to read staged objects
        from, so continuing would fetch from nowhere."""
        for bad in ("local,/srv/cvmfs/repo,/srv/cvmfs/repo",
                    "gw,/var/spool/cvmfs/repo/tmp,http://gw:4929/api/v1",
                    "", "S3,/tmp"):
            with self.assertRaises(StageError, msg="accepted %r" % bad):
                parse_s3_upstream(bad)

    def test_missing_at_sign_is_refused(self):
        with self.assertRaises(StageError):
            parse_s3_upstream("S3,/tmp,cvmfs/bits.cern.ch")

    def test_no_laxer_than_cvmfs_itself(self):
        """upload_spooler_definition.cc refuses upstream.size() != 3 and
        compares the "S3" tag literally. Accepting more here would mean a value
        the publisher rejects gets diagnosed by whatever fails next instead of
        by this function."""
        for bad in ("S3,/tmp,extra,cvmfs/bits.cern.ch@/etc/y.conf",
                    "s3,/tmp,cvmfs/bits.cern.ch@/etc/y.conf"):
            with self.assertRaises(StageError, msg="accepted %r" % bad):
                parse_s3_upstream(bad)


class TestRepoAlias(unittest.TestCase):

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf")
        os.write(fd, text.encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_reads_the_alias(self):
        p = self.conf(
            "CVMFS_SPOOL_DIR=/var/spool/cvmfs/bits.cern.ch\n"
            "CVMFS_UPSTREAM_STORAGE=S3,/var/spool/cvmfs/bits.cern.ch/tmp,"
            "cvmfs/bits.cern.ch@/etc/cvmfs/keys/bits.cern.ch.s3.conf\n"
            "CVMFS_USER=cvmfs\n")
        self.assertEqual(repo_alias("bits.cern.ch", p), "cvmfs/bits.cern.ch")

    def test_last_assignment_wins(self):
        """server.conf is sourced by a shell, so a later line overrides."""
        p = self.conf(
            "CVMFS_UPSTREAM_STORAGE=S3,/tmp,stale/alias@/etc/a.conf\n"
            "CVMFS_UPSTREAM_STORAGE=S3,/tmp,cvmfs/bits.cern.ch@/etc/b.conf\n")
        self.assertEqual(repo_alias("bits.cern.ch", p), "cvmfs/bits.cern.ch")

    def test_export_form_is_an_assignment(self):
        """`export KEY=value` is how a shell-sourced file commonly writes this,
        and it must not lose to a stale earlier line.

        NEGATIVE CONTROL: drop the `export ` handling and this returns
        'stale/alias' -- which then surfaces as bucket_root_url complaining
        that the URL and the alias disagree, blaming the configuration for a
        parser bug. Verified.
        """
        p = self.conf(
            "CVMFS_UPSTREAM_STORAGE=S3,/tmp,stale/alias@/etc/a.conf\n"
            "export CVMFS_UPSTREAM_STORAGE=S3,/tmp,cvmfs/bits.cern.ch@/etc/b.conf\n")
        self.assertEqual(repo_alias("bits.cern.ch", p), "cvmfs/bits.cern.ch")

    def test_commented_out_line_does_not_override(self):
        """A stale value commented out AFTER the live one is the ordering that
        would actually hurt. Can only fail if the reader starts treating a
        leading '#' as insignificant."""
        p = self.conf(
            "CVMFS_UPSTREAM_STORAGE=S3,/tmp,cvmfs/bits.cern.ch@/etc/b.conf\n"
            "#CVMFS_UPSTREAM_STORAGE=S3,/tmp,wrong/alias@/etc/c.conf\n"
            "# export CVMFS_UPSTREAM_STORAGE=S3,/tmp,worse/alias@/etc/d.conf\n")
        self.assertEqual(repo_alias("bits.cern.ch", p), "cvmfs/bits.cern.ch")

    def test_non_utf8_file_raises_StageError_not_a_traceback(self):
        """main() catches StageError only, so a stray byte must not escape as
        UnicodeDecodeError."""
        fd, p = tempfile.mkstemp(suffix=".conf")
        os.write(fd, b"# caf\xe9 latin-1\nCVMFS_UPSTREAM_STORAGE="
                     b"S3,/tmp,cvmfs/bits.cern.ch@/etc/b.conf\n")
        os.close(fd)
        self.addCleanup(os.unlink, p)
        self.assertEqual(repo_alias("bits.cern.ch", p), "cvmfs/bits.cern.ch")

    def test_missing_file_names_the_override(self):
        with self.assertRaises(StageError) as cm:
            repo_alias("bits.cern.ch", "/nonexistent/server.conf")
        self.assertIn("--repo-alias", str(cm.exception))

    def test_file_without_the_key(self):
        with self.assertRaises(StageError):
            repo_alias("bits.cern.ch", self.conf("CVMFS_USER=cvmfs\n"))


class TestBucketRootAndStagingURL(unittest.TestCase):
    """The regression this whole change exists for.

    NEGATIVE CONTROL: replace staging_url's body with the old one-segment chop,

        "%s/%s" % (repo_url.rstrip("/").rsplit("/", 1)[0], stage_prefix)

    and test_testbed_shape STILL PASSES while test_production_shape fails. That
    asymmetry is the bug: a one-segment alias hides it, and the testbed has a
    one-segment alias, so every measurement to date was blind to it. Verified.
    """

    PROD_URL = "http://cvmfs-bits.s3.cern.ch/cvmfs/bits.cern.ch"
    PROD_ALIAS = "cvmfs/bits.cern.ch"
    TB_URL = "http://minio:9000/cvmfs/test.cvmfs.io"
    TB_ALIAS = "test.cvmfs.io"
    STAGE = "staging/build07/alice/12345"

    def test_production_shape(self):
        """Two-segment alias, virtual-host bucket. The bucket root is the
        bare host, so the staged key is staging/... exactly as written."""
        self.assertEqual(bucket_root_url(self.PROD_URL, self.PROD_ALIAS),
                         "http://cvmfs-bits.s3.cern.ch")
        self.assertEqual(staging_url(self.PROD_URL, self.PROD_ALIAS, self.STAGE),
                         "http://cvmfs-bits.s3.cern.ch/" + self.STAGE)

    def test_testbed_shape(self):
        """One-segment alias, path-style bucket. The bucket root keeps the
        bucket segment, and the staged key is again staging/... ."""
        self.assertEqual(bucket_root_url(self.TB_URL, self.TB_ALIAS),
                         "http://minio:9000/cvmfs")
        self.assertEqual(staging_url(self.TB_URL, self.TB_ALIAS, self.STAGE),
                         "http://minio:9000/cvmfs/" + self.STAGE)

    def test_both_deployments_address_the_same_key(self):
        """The invariant that makes the testbed a valid rehearsal: whatever the
        bucket addressing style, the staged object's KEY is the staging prefix
        the spooler was given."""
        prod = staging_url(self.PROD_URL, self.PROD_ALIAS, self.STAGE)
        tb = staging_url(self.TB_URL, self.TB_ALIAS, self.STAGE)
        self.assertTrue(prod.endswith("/" + self.STAGE))
        self.assertTrue(tb.endswith("/" + self.STAGE))
        self.assertEqual(prod[len(bucket_root_url(self.PROD_URL, self.PROD_ALIAS)):],
                         tb[len(bucket_root_url(self.TB_URL, self.TB_ALIAS)):])

    def test_trailing_slashes_do_not_shift_the_result(self):
        for url in (self.PROD_URL + "/", self.PROD_URL + "//"):
            self.assertEqual(staging_url(url, self.PROD_ALIAS, self.STAGE),
                             "http://cvmfs-bits.s3.cern.ch/" + self.STAGE)
        for alias in ("/" + self.PROD_ALIAS, self.PROD_ALIAS + "/"):
            self.assertEqual(staging_url(self.PROD_URL, alias, self.STAGE),
                             "http://cvmfs-bits.s3.cern.ch/" + self.STAGE)
        self.assertEqual(staging_url(self.PROD_URL, self.PROD_ALIAS, "/" + self.STAGE + "/"),
                         "http://cvmfs-bits.s3.cern.ch/" + self.STAGE)

    def test_url_that_does_not_end_with_the_alias_is_REFUSED(self):
        """The console's stratum0_url and the repository's server.conf
        disagreeing is a configuration fault. Guessing past it addresses the
        wrong keys and then blames the prepare."""
        for url in ("http://cvmfs-bits.s3.cern.ch/cvmfs/other.cern.ch",
                    "http://cvmfs-bits.s3.cern.ch/bits.cern.ch",
                    "http://cvmfs-bits.s3.cern.ch/"):
            with self.assertRaises(StageError, msg="accepted %r" % url) as cm:
                bucket_root_url(url, self.PROD_ALIAS)
            msg = str(cm.exception)
            self.assertIn("BITS_STRATUM0_URL", msg)
            self.assertIn("CVMFS_UPSTREAM_STORAGE", msg)

    def test_alias_match_is_on_segment_boundaries(self):
        """Substring matching would cut a hostname in half: alias 'cvmfs.io'
        must NOT match the tail of '.../test.cvmfs.io'."""
        with self.assertRaises(StageError):
            bucket_root_url("http://minio:9000/cvmfs/test.cvmfs.io", "cvmfs.io")

    def test_stripping_everything_leaves_no_root(self):
        """Reaches the empty-root branch specifically: the alias eats the whole
        path AND the host, leaving a bare scheme."""
        with self.assertRaises(StageError):
            bucket_root_url("http:///cvmfs/bits.cern.ch", "cvmfs/bits.cern.ch")

    def test_alias_eating_the_hostname_is_refused(self):
        """'http://host'.endswith('/host') is true -- the alias would eat the
        second slash of '://' and leave 'http:/'."""
        with self.assertRaises(StageError):
            bucket_root_url("http://cvmfs-bits.s3.cern.ch", "cvmfs-bits.s3.cern.ch")

    def test_empty_inputs_refused(self):
        with self.assertRaises(StageError):
            bucket_root_url(self.PROD_URL, "")
        with self.assertRaises(StageError):
            bucket_root_url("/cvmfs/bits.cern.ch", self.PROD_ALIAS)
        with self.assertRaises(StageError):
            staging_url(self.PROD_URL, self.PROD_ALIAS, "")


if __name__ == "__main__":
    unittest.main()
