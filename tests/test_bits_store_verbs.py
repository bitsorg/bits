# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Phase 3.4: the `gc` and `stats` verbs folded into the bitsStore tool
(`bits store gc` / `bits store stats`). These exercise the argparse surface and
prove the verbs are recognized past the default-`ls` logic and reach the
S3-credential gate — no live S3 needed."""

import os
import subprocess
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BITSSTORE = os.path.join(_ROOT, "bitsStore")


def _run(args):
    """Run bitsStore with a deterministic no-credentials environment."""
    env = dict(os.environ)
    env["BITS_AWS_KEYS_FILE"] = os.path.join(_ROOT, "tests", "_no_such_s3keys")
    env["BITS_S3_STORE"] = "https://s3.invalid/bucket"
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)
    return subprocess.run(["bash", _BITSSTORE] + args,
                          capture_output=True, text=True, env=env)


class TestStoreVerbs(unittest.TestCase):
    def test_gc_help_lists_trust_manifest(self):
        r = _run(["gc", "-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--trust-manifest", r.stdout)

    def test_stats_help_lists_manifests(self):
        r = _run(["stats", "-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--manifests", r.stdout)

    def test_gc_requires_trust_manifest(self):
        r = _run(["gc"])
        self.assertEqual(r.returncode, 2)                 # argparse usage error
        self.assertIn("trust-manifest", r.stderr)

    def test_gc_recognized_and_reaches_cred_gate(self):
        # With --trust-manifest satisfied but no creds, gc must reach the S3
        # credential gate — proving it is NOT swallowed by the default `ls` verb.
        r = _run(["gc", "--trust-manifest", "/nope"])
        self.assertIn("no S3 credentials", r.stderr)

    def test_stats_recognized_and_reaches_cred_gate(self):
        r = _run(["stats"])
        self.assertIn("no S3 credentials", r.stderr)

    def test_upload_help_lists_package(self):
        r = _run(["upload", "-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("PACKAGE", r.stdout)

    def test_upload_requires_package(self):
        r = _run(["upload"])
        self.assertEqual(r.returncode, 2)                 # argparse usage error

    def test_upload_recognized_and_reaches_cred_gate(self):
        r = _run(["upload", "zlib"])
        self.assertIn("no S3 credentials", r.stderr)


if __name__ == "__main__":
    unittest.main()
