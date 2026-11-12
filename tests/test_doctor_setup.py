"""Tests for `bits doctor` without packages (bits_helpers/doctor_setup.py)."""
import os
import unittest
from unittest.mock import patch, mock_open

from bits_helpers import doctor_setup as ds
from bits_helpers.doctor import PASS, FAIL, WARN, SKIP


class ModulesTest(unittest.TestCase):
  def test_missing_module_gives_pip_hint_for_this_python(self):
    with patch("bits_helpers.doctor_setup.importlib.util.find_spec",
               side_effect=lambda m: None if m == "boto3" else object()), \
         patch("bits_helpers.doctor_setup.os.geteuid", return_value=1000):
      status, detail = ds.check_modules(ds._B3_MODULES, FAIL)
    self.assertEqual(status, FAIL)
    self.assertIn("boto3", detail)
    self.assertIn("-m pip install --user boto3", detail)

  def test_all_present(self):
    with patch("bits_helpers.doctor_setup.importlib.util.find_spec", return_value=object()):
      self.assertEqual(ds.check_modules(ds._MODULES, FAIL)[0], PASS)


  def test_no_qrcode_is_a_warning(self):
    with patch("bits_helpers.doctor_setup.importlib.util.find_spec", return_value=None):
      status, detail = ds.check_qrcode()
    self.assertEqual(status, WARN)
    self.assertIn("python3-qrcode", detail)
    with patch("bits_helpers.doctor_setup.importlib.util.find_spec", return_value=object()):
      self.assertEqual(ds.check_qrcode()[0], PASS)


class RootTest(unittest.TestCase):
  def test_root_warns(self):
    with patch("bits_helpers.doctor_setup.os.geteuid", return_value=0):
      self.assertEqual(ds.check_root()[0], WARN)
    with patch("bits_helpers.doctor_setup.os.geteuid", return_value=1000), \
         patch("bits_helpers.doctor_setup.getpass.getuser", return_value="me"):
      self.assertEqual(ds.check_root()[0], PASS)


def _fake_run(outputs):
  def run(cmd, timeout=30, stdout_only=False):
    for prefix, result in outputs.items():
      if tuple(cmd[:len(prefix)]) == prefix:
        return result
    return 1, ""
  return run


class ContainerEngineTest(unittest.TestCase):
  def test_no_docker(self):
    with patch("bits_helpers.doctor_setup.shutil.which", return_value=None):
      self.assertEqual(ds.check_container_engine()[0][1], SKIP)

  def _podman(self, controllers, subids, fstype="xfs"):
    outputs = {("docker", "--version"): (0, "podman version 5.4.0"),
               ("docker", "info"): (0, "ok"),
               ("podman", "info"): (0, "/home/me/.local/share/containers/storage"),
               ("stat",): (0, fstype)}
    which = lambda name: "/usr/bin/" + name if name in ("docker",) else None
    with patch("bits_helpers.doctor_setup.shutil.which", side_effect=which), \
         patch("bits_helpers.doctor_setup._run", side_effect=_fake_run(outputs)), \
         patch("bits_helpers.args._is_rootless_podman", return_value=True), \
         patch("bits_helpers.args._rootless_podman_controllers", return_value=controllers), \
         patch("bits_helpers.doctor_setup.getpass.getuser", return_value="me"), \
         patch("bits_helpers.doctor_setup.os.getuid", return_value=1000), \
         patch("builtins.open", mock_open(read_data=subids)):
      return {name: (status, detail) for name, status, detail in ds.check_container_engine()}

  def test_rootless_podman_ok(self):
    got = self._podman({"cpu", "cpuset", "memory", "pids"}, "me:100000:65536\n")
    self.assertEqual(got["container engine"][0], PASS)
    self.assertEqual(got["podman resource limits"][0], PASS)
    self.assertEqual(got["podman subordinate ids"][0], PASS)
    self.assertEqual(got["podman storage"][0], PASS)

  def test_no_user_session_and_unknown_fs(self):
    got = self._podman(set(), "me:100000:65536\n", fstype="UNKNOWN (0x1234)")
    self.assertEqual(got["podman resource limits"][0], WARN)
    self.assertIn("enable-linger", got["podman resource limits"][1])
    self.assertEqual(got["podman storage"][0], WARN)

  def test_rootless_podman_problems(self):
    got = self._podman({"cpu", "memory", "pids"}, "other:100000:65536\n", fstype="nfs")
    self.assertEqual(got["podman resource limits"][0], WARN)
    self.assertIn("cpuset", got["podman resource limits"][1])
    self.assertEqual(got["podman subordinate ids"][0], FAIL)
    self.assertEqual(got["podman storage"][0], FAIL)


class S3StoreTest(unittest.TestCase):
  def test_no_credentials(self):
    with patch.dict(os.environ, {}, clear=True), \
         patch("bits_helpers.sync._load_aws_keys_file", return_value={}):
      status, detail = ds.check_s3_store("b3://bucket", object(), "write store")
    self.assertEqual(status, FAIL)
    self.assertIn("no S3 credentials", detail)


class RootHintTest(unittest.TestCase):
  def test_pip_hint_under_sudo_says_not_root(self):
    with patch("bits_helpers.doctor_setup.os.geteuid", return_value=0):
      self.assertIn("not sudo", ds._pip_hint(["boto3"]))


class S3cmdStoreTest(unittest.TestCase):
  def test_s3cmd_store(self):
    with patch("bits_helpers.doctor_setup.shutil.which", return_value=None):
      self.assertEqual(ds.check_s3cmd_store("s3://b")[0], FAIL)
    with patch("bits_helpers.doctor_setup.shutil.which", return_value="/usr/bin/s3cmd"), \
         patch("bits_helpers.doctor_setup.os.path.isfile", return_value=True):
      self.assertEqual(ds.check_s3cmd_store("s3://b")[0], PASS)


class RunSetupTest(unittest.TestCase):
  def test_boto_missing_is_fail_only_with_s3_store(self):
    class A:
      remoteStore = ""
      writeStore = ""
      workDir = "sw"
    no_boto = lambda m: None if m in ("boto3", "botocore") else object()
    with patch("bits_helpers.doctor_setup.importlib.util.find_spec", side_effect=no_boto), \
         patch("bits_helpers.doctor_setup.check_container_engine", return_value=[]), \
         patch("bits_helpers.doctor_setup.check_s3_store", return_value=(FAIL, "x")):
      got = {n: s for n, s, _ in ds.run_setup_checks(A())}
      self.assertEqual(got["python modules (b3:// store)"], WARN)
      self.assertEqual(got["python modules (login QR code)"], PASS)
      A.writeStore = "s3://bucket"      # s3cmd store: boto3 not needed
      with patch("bits_helpers.doctor_setup.check_s3cmd_store", return_value=(PASS, "x")):
        got = {n: s for n, s, _ in ds.run_setup_checks(A())}
      self.assertEqual(got["python modules (b3:// store)"], WARN)
      A.writeStore = "b3://bucket"
      got = {n: s for n, s, _ in ds.run_setup_checks(A())}
      self.assertEqual(got["python modules (b3:// store)"], FAIL)
      self.assertEqual(got["write store"], FAIL)


if __name__ == "__main__":
  unittest.main()
