# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Auto-derivation of signed-reuse trust-manifest URLs from a remote store."""

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


if __name__ == "__main__":
    unittest.main()
