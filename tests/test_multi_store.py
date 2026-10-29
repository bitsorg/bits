# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Several read and write stores, each bucket with its own credentials (issue #130)."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import sync

ARCH = "slc9_x86-64"
SPEC = {"package": "zlib", "version": "1.3", "remote_hashes": ["aa" * 20], "cachedTarball": ""}


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
@patch.dict(os.environ, {"BITS_AWS_KEYS_FILE": "/nonexistent"})
class RemoteFromUrlTest(unittest.TestCase):
  def test_lists_give_one_helper_per_store(self):
    h = sync.remote_from_url("b3://prod, https://s3.cern.ch/pr", "b3://pr,b3://backup", ARCH, "/sw")
    self.assertIsInstance(h, sync.MultiRemoteSync)
    self.assertEqual([type(r).__name__ for r in h.readers], ["Boto3RemoteSync", "HttpRemoteSync"])
    self.assertEqual(h.readers[1].remoteStore, "https://s3.cern.ch/swift/v1/pr")
    self.assertEqual([r.writeStore for r in h.readers], ["", ""])          # readers never upload
    self.assertEqual([w.writeStore for w in h.writers], ["pr", "backup"])
    self.assertEqual(h.writeStore, "pr,backup")

  def test_write_stores_alone_are_read_too(self):
    h = sync.remote_from_url("", "b3://pr,b3://backup", ARCH, "/sw")
    self.assertEqual([r.remoteStore for r in h.readers], ["pr", "backup"])

  def test_two_buckets_get_two_clients(self):
    h = sync.remote_from_url("b3://prod", "b3://pr", ARCH, "/sw")
    self.assertIsInstance(h, sync.DualRemoteSync)
    self.assertEqual((h.reader.remoteStore, h.reader.writeStore), ("prod", ""))
    self.assertEqual((h.writer.remoteStore, h.writer.writeStore), ("pr", "pr"))
    self.assertIsInstance(sync.remote_from_url("b3://pr", "b3://pr", ARCH, "/sw"),
                          sync.Boto3RemoteSync)                         # one store: as before


class MultiRemoteSyncTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(__import__("shutil").rmtree, self.work, True)
    self.readers = [MagicMock(remoteStore="prod"), MagicMock(remoteStore="pr")]
    self.writers = [MagicMock(writeStore="pr"), MagicMock(writeStore="backup")]
    self.multi = sync.MultiRemoteSync(self.readers, self.writers)
    self.multi.workdir, self.multi.architecture = self.work, ARCH

  def test_reads_stop_at_the_first_store_that_has_it(self):
    def fetch(spec):
      d = os.path.join(self.work, sync.resolve_store_path(ARCH, spec["remote_hashes"][0]))
      os.makedirs(d)
      open(os.path.join(d, "zlib-1.3-1.%s.tar.gz" % ARCH), "w").close()
    self.readers[0].fetch_tarball.side_effect = fetch
    self.multi.fetch_tarball(SPEC)
    self.readers[1].fetch_tarball.assert_not_called()
    other = dict(SPEC, remote_hashes=["bb" * 20])           # not in the first store
    self.readers[0].fetch_tarball.side_effect = OSError("403")
    self.multi.fetch_tarball(other)                          # a failing store is skipped
    self.readers[1].fetch_tarball.assert_called_once_with(other)
    self.readers[0].fetch_source.return_value = False
    self.readers[1].fetch_source.return_value = True
    self.assertTrue(self.multi.fetch_source("c", "f", "/d"))
    self.readers[0].list_store_tarballs.return_value = []
    self.readers[1].list_store_tarballs.return_value = ["x"]
    self.assertEqual(self.multi.list_store_tarballs(ARCH, "aa"), ["x"])
    self.writers[0].list_store_tarballs.return_value = ["w"]
    self.readers[1].list_store_tarballs.return_value = []
    self.assertEqual(self.multi.list_store_tarballs(ARCH, "aa"), [])   # write stores not read

  def test_revision_markers_of_every_store_are_merged(self):
    for helper, markers in zip(self.readers + self.writers,
                               ({"1": "a"}, {"1": "b", "2": "c"}, {"3": "d"}, RuntimeError("x"))):
      if isinstance(markers, Exception):
        helper.read_rev_markers.side_effect = markers
      else:
        helper.read_rev_markers.return_value = markers
    self.assertEqual(self.multi.read_rev_markers("zlib", "1.3", ARCH),
                     {"1": "a", "2": "c", "3": "d"})

  def test_uploads_go_to_every_write_store(self):
    self.multi.upload_symlinks_and_tarball(SPEC)
    self.multi.upload_source("/p", "c", "f")
    for w in self.writers:
      w.upload_symlinks_and_tarball.assert_called_once_with(SPEC)
      w.upload_source.assert_called_once_with("/p", "c", "f")
    self.multi.upload_symlinks_and_tarball(dict(SPEC, cachedTarball="/x.tar.gz"))
    self.assertEqual(self.writers[0].upload_symlinks_and_tarball.call_count, 1)  # recalled
    self.writers[0].writestore_has_tarball.return_value = True
    self.writers[1].writestore_has_tarball.return_value = False
    self.assertFalse(self.multi.writestore_has_tarball(SPEC))
    self.multi.writeStore = ""                  # a development package: no uploads at all
    self.assertEqual([w.writeStore for w in self.writers], ["", ""])

  def test_unreachable_read_stores_are_dropped_one_by_one(self):
    multi = sync.MultiRemoteSync(self.readers, [])
    with patch("bits_helpers.sync.store_unreachable", side_effect=["ConnectionError", ""]):
      kept, down = sync.drop_unreachable(multi)
    self.assertEqual((kept, down), (self.readers[1], [("prod", "ConnectionError")]))
    self.assertEqual(sync.drop_unreachable(self.multi), (self.multi, []))   # it writes


class S3SettingsTest(unittest.TestCase):
  def setUp(self):
    self.keys = tempfile.NamedTemporaryFile("w", delete=False)
    self.keys.write("AWS_ACCESS_KEY_ID=global-key\nAWS_SECRET_ACCESS_KEY=global-secret\n"
                    "[bucket cms-pr]  # the PR bucket\naws_access_key_id = pr-key\naws_secret_access_key = pr-secret\n"
                    "endpoint_url = https://pr.example\n")
    self.keys.close()
    os.chmod(self.keys.name, 0o600)
    self.addCleanup(os.unlink, self.keys.name)
    env = {"BITS_AWS_KEYS_FILE": self.keys.name, "AWS_ACCESS_KEY_ID": "run-key",
           "AWS_SECRET_ACCESS_KEY": "run-secret", "S3_ENDPOINT_URL": "https://run.example"}
    p = patch.dict(os.environ, env)
    p.start()
    self.addCleanup(p.stop)

  def test_a_bucket_section_its_variables_else_the_run(self):
    pr = sync.s3_settings("cms-pr")
    self.assertEqual((pr["access_key"], pr["secret_key"], pr["endpoint"]),
                     ("pr-key", "pr-secret", "https://pr.example"))
    prod = sync.s3_settings("cms-prod")
    self.assertEqual((prod["access_key"], prod["endpoint"]), ("run-key", "https://run.example"))
    with patch.dict(os.environ, {"BITS_S3_CMS_PR_ACCESS_KEY_ID": "env-key",
                                 "BITS_S3_CMS_PR_SECRET_ACCESS_KEY": "env-secret"}):
      self.assertEqual(sync.s3_settings("cms-pr")["secret_key"], "env-secret")
    with patch.dict(os.environ, {"BITS_S3_CMS_PROD_ACCESS_KEY_ID": "half"}):
      # A key without its secret is not mixed with another source's secret.
      self.assertEqual(sync.s3_settings("cms-prod")["secret_key"], None)

  def test_the_run_settings_ignore_bucket_sections(self):
    self.assertEqual(sync._load_aws_keys_file(self.keys.name)["access_key"], "global-key")

  def test_other_sections_still_count_for_the_run(self):
    with tempfile.NamedTemporaryFile("w") as f:          # as copied from ~/.aws/credentials
      f.write("[cern]\naws_access_key_id = k\naws_secret_access_key = s\n")
      f.flush()
      os.chmod(f.name, 0o600)
      self.assertEqual(sync._load_aws_keys_file(f.name)["access_key"], "k")
      self.assertEqual(sync._load_aws_keys_file(f.name, "cern"), {})

  def test_the_client_uses_its_buckets_credentials(self):
    try:
      import boto3                                       # noqa: F401
    except ImportError:
      self.skipTest("boto3 is not installed")
    with patch("boto3.client") as client:
      sync.Boto3RemoteSync("b3://cms-pr", "", ARCH, "/sw")
    kw = client.call_args.kwargs
    self.assertEqual((kw["aws_access_key_id"], kw["endpoint_url"]), ("pr-key", "https://pr.example"))


class TemplateTest(unittest.TestCase):
  def test_recipes_see_no_s3_secret(self):
    import subprocess
    with open(os.path.join(os.path.dirname(sync.__file__), "build_template.sh")) as f:
      text = f.read()
    start = text.index("# Cleanup variables which should not be exposed")
    snippet = text[start:text.index("unset _bits_v", start)] + "unset _bits_v\nenv\n"
    env = dict(os.environ, AWS_ACCESS_KEY_ID="1", AWS_SESSION_TOKEN="2",
               BITS_S3_CMS_PR_SECRET_ACCESS_KEY="3", BITS_AWS_SECRET_ACCESS_KEY="4",
               BITS_S3_CMS_PR_ENDPOINT_URL="https://pr.example")
    out = subprocess.run(["bash", "-c", snippet], env=env, capture_output=True,
                         text=True, check=True).stdout
    self.assertNotRegex(out, r"(?m)^(AWS_ACCESS_KEY_ID|AWS_SESSION_TOKEN|BITS_S3_CMS_PR_SECRET"
                             r"_ACCESS_KEY|BITS_AWS_SECRET_ACCESS_KEY)=")
    self.assertIn("BITS_S3_CMS_PR_ENDPOINT_URL=https://pr.example", out)   # not a secret


class SystemCheckEnvTest(unittest.TestCase):
  def test_recipe_checks_see_no_s3_secret(self):
    from bits_helpers.cmd import DockerRunner
    with patch.dict(os.environ, {"BITS_S3_CMS_PR_SECRET_ACCESS_KEY": "3", "AWS_SESSION_TOKEN": "2"}):
      with DockerRunner(None) as run:
        _, out = run("env")
    self.assertNotIn("BITS_S3_CMS_PR_SECRET_ACCESS_KEY", out)
    self.assertNotIn("AWS_SESSION_TOKEN", out)


class DoctorTest(unittest.TestCase):
  def test_a_package_is_looked_up_in_every_store(self):
    from bits_helpers import doctor
    answers = {"https://a": (doctor.WARN, "not found"), "https://b": (doctor.PASS, "available")}
    with patch.object(doctor, "_probe_tarball_in_store", side_effect=lambda s, a, u, i: answers[u]):
      self.assertEqual(doctor._probe_in_stores({}, ARCH, "https://a,https://b"),
                       (doctor.PASS, "available  in https://b"))
      self.assertEqual(doctor._probe_in_stores({}, ARCH, "https://a"), (doctor.WARN, "not found"))



class HttpsStoresTest(unittest.TestCase):
  def test_each_https_store_keeps_its_host(self):
    from bits_helpers.publish import _normalize_s3_store
    with patch.dict(os.environ, {"BITS_AWS_KEYS_FILE": "/nonexistent"}, clear=False):
      for k in [k for k in os.environ if k.startswith("BITS_S3_")]:
        os.environ.pop(k)
      self.assertEqual(_normalize_s3_store("https://a.example/prod"), "b3://prod")
      self.assertEqual(_normalize_s3_store("https://b.example/swift/v1/pr"), "b3://pr")
      self.assertEqual(sync.s3_settings("prod")["endpoint"], "https://a.example")
      self.assertEqual(sync.s3_settings("pr")["endpoint"], "https://b.example")


class OperationsOnEveryStoreTest(unittest.TestCase):
  """publish, certify, gc and compliance with several write stores."""

  def _args(self, **kw):
    from argparse import Namespace
    return Namespace(**dict(dict(workDir="/sw", architecture=ARCH, dryRun=False,
                                 fromManifest=None, package=None, publishView=None), **kw))

  def test_publish_goes_to_every_store(self):
    from bits_helpers import publish
    with patch.object(publish, "_publish_from_manifest") as pub:
      publish.doPublish(self._args(publishStore="b3://a, b3://b"), MagicMock())
    self.assertEqual([c.args[2] for c in pub.call_args_list], ["b3://a", "b3://b"])

  def test_certify_publishes_everywhere_and_certifies_the_first(self):
    from bits_helpers import publish
    bom = lambda sha: [(ARCH, {"packages": [{"hash": "h1", "tarball_sha256": sha}]})]
    args = self._args(publishStore="b3://a,b3://b", approval="passkey", certifier=None,
                      gitlabToken=None, certifyGroup=None, manifestsRemote=None, certifyRef=None)
    with patch.object(publish, "_orchestrator_marker", return_value={"manifests": "m"}) as mark, \
         patch.object(publish, "_publish_from_manifest",
                      side_effect=[("bid", bom("x"), {}), ("bid", bom("y"), {})]) as pub, \
         patch.object(publish, "_preapprove_build") as approve, \
         patch.object(publish, "warning") as warn, \
         patch("bits_helpers.orchestrated.describe", return_value="the orchestrator"):
      publish.doCertify(args, MagicMock())
    self.assertEqual(mark.call_args.args[0], "b3://a")
    self.assertEqual([c.args[2] for c in pub.call_args_list], ["b3://a", "b3://b"])
    self.assertEqual(approve.call_args.args[3], bom("x"))           # the first store's
    self.assertIn("h1@" + ARCH, warn.call_args.args[3])            # b holds another tarball

  def test_gc_sweeps_every_store(self):
    from bits_helpers import gc
    writers = {"b3://a": MagicMock(remoteStore="a"), "b3://b": MagicMock(remoteStore="b")}
    with patch("bits_helpers.sync.remote_from_url", side_effect=lambda r, w, a, d: writers[r]), \
         patch.object(gc, "_localize_manifest", return_value="/m.json"), \
         patch.object(gc, "gather_s3_store_objects", return_value=[]) as gather, \
         patch.object(gc, "collect_garbage",
                      return_value=dict(verified=True, delete=[], kept=0, young=0, unsafe=0)):
      gc.doGc(self._args(gcStore="b3://a,b3://b", trustManifest="ma,mb", graceDays=7,
                         allowEmpty=False), MagicMock())
      parser = MagicMock(error=MagicMock(side_effect=SystemExit))
      with self.assertRaises(SystemExit):                  # one manifest for two stores
        gc.doGc(self._args(gcStore="b3://a,b3://b", trustManifest="m", graceDays=7,
                           allowEmpty=False), parser)
    self.assertEqual([c.args[1] for c in gather.call_args_list], ["a", "b"])
    self.assertIn("one --trust-manifest per store", parser.error.call_args.args[0])

  def test_compliance_enforces_on_every_store(self):
    from bits_helpers import compliance
    with tempfile.TemporaryDirectory() as recipes:
      with open(os.path.join(recipes, "zlib.sh"), "w") as f:
        f.write("package: zlib\nversion: '1'\nlicense: Zlib\n---\n")
      args = self._args(packages=[], recipesDir=recipes, enforce=True, noStoreCheck=False,
                        complianceStore="b3://a,b3://b", enforceKey=None)
      with patch.object(compliance, "enforce_store", side_effect=[0, 2]) as enforce, \
           patch.object(compliance, "_console", return_value=None):
        self.assertEqual(compliance.doCompliance(args, MagicMock()), 2)
    self.assertEqual([c.args[0] for c in enforce.call_args_list], ["b3://a", "b3://b"])


if __name__ == "__main__":
  unittest.main()
