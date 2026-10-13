# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Auto-derivation of signed-reuse trust-manifest URLs from a remote store."""

import json
import os
import tempfile
import unittest

from bits_helpers.build import derive_trust_manifest_srcs as d

P = "MANIFESTS/common-manifest"
A = "x86_64-el8"


class TestDeriveTrustManifestSrcs(unittest.TestCase):

    def test_b3_store_maps_to_anonymous_swift_read_url(self):
        # The whole point of the fix: a b3:// write store (::rw) still yields the
        # signed manifest URLs, mapped to the bucket's anonymous S3 read path.
        self.assertEqual(
            d("b3://lcgapp-bits-testing::rw", P, A),
            ["https://s3.cern.ch/swift/v1/lcgapp-bits-testing/MANIFESTS/common-manifest-x86_64-el8.json",
             "https://s3.cern.ch/swift/v1/lcgapp-bits-testing/MANIFESTS/common-manifest-shared.json"])

    def test_s3_scheme_same_as_b3(self):
        self.assertEqual(
            d("s3://mybucket", P, A)[0],
            "https://s3.cern.ch/swift/v1/mybucket/MANIFESTS/common-manifest-x86_64-el8.json")

    def test_custom_endpoint(self):
        self.assertEqual(
            d("b3://b", P, A, endpoint="https://minio.example.com:9000/")[0],
            "https://minio.example.com:9000/swift/v1/b/MANIFESTS/common-manifest-x86_64-el8.json")

    def test_http_store_hosts_manifest_directly(self):
        self.assertEqual(
            d("https://s3.cern.ch/swift/v1/alibuild-repo", P, A),
            ["https://s3.cern.ch/swift/v1/alibuild-repo/MANIFESTS/common-manifest-x86_64-el8.json",
             "https://s3.cern.ch/swift/v1/alibuild-repo/MANIFESTS/common-manifest-shared.json"])

    def test_no_arch_yields_shared_only(self):
        self.assertEqual(d("b3://b", P, ""),
                         ["https://s3.cern.ch/swift/v1/b/MANIFESTS/common-manifest-shared.json"])

    def test_unsupported_or_empty_store_yields_nothing(self):
        # Fail-closed: no derivation -> no reuse (unchanged behaviour), never a crash.
        self.assertEqual(d("rsync://host/path", P, A), [])
        self.assertEqual(d("cvmfs://repo", P, A), [])
        self.assertEqual(d("", P, A), [])
        self.assertEqual(d(None, P, A), [])


class TestListStoreManifestSrcs(unittest.TestCase):
    """Listing-based derivation: trust every signed manifest in the store."""

    def _run(self, objs, store="b3://lcgapp-bits-testing::rw"):
        import bits_helpers.download as dl
        from bits_helpers.build import _list_store_manifest_srcs

        def fake_dl(url, destDir, work_dir, dest_filename=None):
            self.assertIn("format=json", url)
            with open(os.path.join(destDir, dest_filename), "w") as fh:
                json.dump(objs, fh)
            return True

        orig = dl.downloadUrllib2
        dl.downloadUrllib2 = fake_dl
        try:
            return _list_store_manifest_srcs(store, P, None, tempfile.mkdtemp())
        finally:
            dl.downloadUrllib2 = orig

    def test_lists_manifests_sorted_excluding_sig_and_nonprefix(self):
        objs = [{"name": "MANIFESTS/common-manifest-x86_64-el9-gcc15.json"},
                {"name": "MANIFESTS/common-manifest-x86_64-el9-gcc15.json.sig"},
                {"name": "MANIFESTS/common-manifest-x86_64-el10-gcc14-opt.json"},
                {"name": "OTHER/unrelated.json"}]
        b = "https://s3.cern.ch/swift/v1/lcgapp-bits-testing/MANIFESTS/common-manifest"
        self.assertEqual(self._run(objs),
                         [b + "-x86_64-el10-gcc14-opt.json", b + "-x86_64-el9-gcc15.json"])

    def test_no_work_dir_returns_empty(self):
        from bits_helpers.build import _list_store_manifest_srcs
        self.assertEqual(_list_store_manifest_srcs("b3://b", P, None, None), [])

    def test_unsupported_store_returns_empty(self):
        from bits_helpers.build import _list_store_manifest_srcs
        self.assertEqual(_list_store_manifest_srcs("rsync://h/p", P, None, "/tmp"), [])

    def test_download_failure_falls_back_to_empty(self):
        import bits_helpers.download as dl
        from bits_helpers.build import _list_store_manifest_srcs
        orig = dl.downloadUrllib2
        dl.downloadUrllib2 = lambda *a, **k: False   # fetch failed, wrote nothing
        try:
            self.assertEqual(
                _list_store_manifest_srcs("b3://b", P, None, tempfile.mkdtemp()), [])
        finally:
            dl.downloadUrllib2 = orig



class TestPreferredPrefixes(unittest.TestCase):
    """trust_manifest_prefix may name several prefixes: per architecture, the
    first that has a manifest wins; later ones fill in the rest."""

    def _run(self, listings, arch="x86_64-el9-gcc15-opt"):
        from unittest.mock import patch
        from bits_helpers import build
        root = "https://s3.cern.ch/swift/v1/b"
        with patch.object(build, "_list_store_manifest_srcs",
                          lambda store, prefix, ep, wd: ["%s/%s" % (root, n)
                                                         for n in listings.get(prefix, [])]):
            return build.trust_manifest_srcs("b3://b", "CANARY/common-manifest, MANIFESTS/common-manifest",
                                             None, "/tmp", arch)

    def test_the_canary_where_it_has_the_arch_production_elsewhere(self):
        srcs, guessed = self._run({
            "CANARY/common-manifest": ["CANARY/common-manifest-x86_64-el9-gcc15-opt.json"],
            "MANIFESTS/common-manifest": ["MANIFESTS/common-manifest-x86_64-el9-gcc15-opt.json",
                                          "MANIFESTS/common-manifest-x86_64-el10-gcc14-opt.json"]})
        self.assertEqual(sorted(s.split("/b/")[1] for s in srcs),
                         ["CANARY/common-manifest-x86_64-el9-gcc15-opt.json",
                          "MANIFESTS/common-manifest-x86_64-el10-gcc14-opt.json"])
        self.assertEqual(guessed, set())

    def test_an_empty_canary_leaves_production_alone(self):
        srcs, guessed = self._run({"MANIFESTS/common-manifest":
                                   ["MANIFESTS/common-manifest-x86_64-el9-gcc15-opt.json"]})
        self.assertEqual([s.split("/b/")[1] for s in srcs],
                         ["MANIFESTS/common-manifest-x86_64-el9-gcc15-opt.json"])
        self.assertEqual(guessed, set())

    def test_an_unlistable_store_guesses_every_prefix(self):
        srcs, guessed = self._run({})
        self.assertEqual(set(srcs), guessed)
        self.assertEqual(len(srcs), 4)            # own arch + shared, for each prefix

if __name__ == "__main__":
    unittest.main()
