# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""download.py runs no shell on names that come from a source URL: they ran on the
host, outside any sandbox, with the whole environment."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import download as dl


class NoShellTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)
    self.cwd = os.getcwd()
    os.chdir(self.work)                       # where a `touch` would land
    self.addCleanup(os.chdir, self.cwd)

  def test_a_source_name_runs_nothing(self):
    name = "a;touch pwned;b.tar.gz"

    def fetch(source, dest_dir, work_dir):
      with open(os.path.join(dest_dir, name), "w") as f:
        f.write("data")
    dest = os.path.join(self.work, "SOURCES", "pkg", "1", "x")
    with patch.dict(dl.downloadHandlers, {"https": fetch}):
      dl.download("https://example.org/" + name, dest, self.work)
    self.assertFalse(os.path.exists(os.path.join(self.work, "pwned")))
    with open(os.path.join(dest, name)) as f:
      self.assertEqual(f.read(), "data")


  def git_repo(self):
    import subprocess
    repo = os.path.join(self.work, "up", "repo.git")
    os.makedirs(repo)
    git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    with open(os.path.join(repo, "hello.txt"), "w") as f:
      f.write("hello\n")
    subprocess.run(git + ["add", "hello.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
    subprocess.run(git + ["tag", "v1"], check=True)
    return repo

  def test_a_git_source_runs_no_part_of_its_url(self):
    import tarfile
    repo, out = self.git_repo(), os.path.join(self.work, "out")
    os.makedirs(out)
    self.assertTrue(dl.downloadGit("git+file://%s?obj=main/v1" % repo, out, self.work))
    with tarfile.open(os.path.join(out, "repov1.tar.gz")) as tf:
      self.assertEqual(sorted(tf.getnames()), ["repov1", "repov1/hello.txt"])
    for bad in ("main/v1;touch pwned", "main/v1$(touch pwned)", "main/--help"):
      with patch("bits_helpers.download.warning"):
        self.assertFalse(dl.downloadGit("git+file://%s?obj=%s" % (repo, bad), out, self.work))
    self.assertFalse(os.path.exists(os.path.join(self.work, "pwned")))


  def test_a_pip_source_runs_only_pip(self):
    import subprocess
    out = os.path.join(self.work, "out")
    os.makedirs(out)
    with patch("bits_helpers.download.downloadUrllib2", return_value=False), \
         patch("bits_helpers.download.urlopen", side_effect=OSError("offline")), \
         patch("bits_helpers.download.warning") as warned:
      for opts in ("pip=touch pwned;pip", "pip=/bin/sh", "pip=/usr/bin/pipewire", "pip=bin/pip"):
        self.assertFalse(dl.downloadPip("pip://flask/3.1.1?%s&pip_options=--only-binary=:all:&output=/f.tgz"
                                        % opts, out, self.work))
      self.assertEqual(warned.call_count, 4)
      open(os.path.join(out, "flask-3.1.1.tar.gz.downloading"), "w").close()   # download()'s sentinel
      seen = []

      envs = []

      def run(cmd, **kw):
        seen.append(cmd)
        env = kw.get("env") or os.environ
        envs.append(("PREPUB_API_TOKEN" in env, env.get("PIP_INDEX_URL"), env.get("HTTPS_PROXY")))
        with open(os.path.join(kw["cwd"], "Flask-3.1.1.tar.gz"), "w") as f:
          f.write("sdist")
        return subprocess.CompletedProcess(cmd, 0, b"")
      with patch("subprocess.run", side_effect=run), \
           patch.dict(os.environ, {"PREPUB_API_TOKEN": "t", "PIP_INDEX_URL": "https://u:t@pypi.site/simple",
                                   "HTTPS_PROXY": "http://u:p@proxy:3128"}):
        self.assertTrue(dl.downloadPip("pip://flask/3.1.1?pip_options=--only-binary=:all:;touch+pwned"
                                       "&output=/flask-3.1.1.tar.gz", out, self.work))
    self.assertEqual(seen[0][:2], ["pip", "download"])
    self.assertEqual(seen[0][-2:], ["--", "flask==3.1.1"])
    self.assertIn("--only-binary=:all:;touch", seen[0])       # an argument, not a command
    self.assertEqual(envs, [(False, "https://u:t@pypi.site/simple", "http://u:p@proxy:3128")])
    self.assertEqual(sorted(os.listdir(out)), ["flask-3.1.1.tar.gz", "flask-3.1.1.tar.gz.downloading"])
    self.assertFalse(os.path.exists(os.path.join(self.work, "pwned")))


  def test_a_source_that_cannot_be_copied_stops_the_download(self):
    def fetch(source, dest_dir, work_dir):
      with open(os.path.join(dest_dir, "y.tar.gz"), "w") as f:
        f.write("y")
    with patch.dict(dl.downloadHandlers, {"https": fetch}), \
         patch("shutil.copy", side_effect=OSError("No space left on device")), \
         self.assertRaisesRegex(OSError, "Could not copy source .*No space"):
      dl.download("https://example.org/y.tar.gz", os.path.join(self.work, "src"), self.work)

  def test_a_copy_failing_its_checksum_is_not_kept(self):
    import hashlib
    calls = []

    def fetch(source, dest_dir, work_dir):
      calls.append(source)
      with open(os.path.join(dest_dir, "x.tar.gz"), "w") as f:
        f.write("truncated" if len(calls) == 1 else "good")
    good = "sha256:" + hashlib.sha256(b"good").hexdigest()
    with patch.dict(dl.downloadHandlers, {"https": fetch}):
      with self.assertRaises(SystemExit):
        dl.download("https://example.org/x.tar.gz", None, self.work, checksum=good, enforce_mode="enforce")
      dl.download("https://example.org/x.tar.gz", None, self.work, checksum=good, enforce_mode="enforce")
      self.assertEqual(len(calls), 2)                    # fetched anew, now right
      with self.assertRaises(SystemExit):                # no checksum declared: kept
        dl.download("https://example.org/x.tar.gz", None, self.work, checksum=None, enforce_mode="enforce")
      dl.download("https://example.org/x.tar.gz", None, self.work, checksum=good, enforce_mode="enforce")
    self.assertEqual(len(calls), 2)



class UrllibTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)

  def test_a_url_with_a_login(self):
    import io
    seen = []

    def urlopen(req, *a, **kw):
      seen.append((req.full_url, req.get_header("Authorization")))
      return io.BytesIO(b"data")
    with patch("bits_helpers.download.urlopen", side_effect=urlopen):
      self.assertTrue(dl.downloadUrllib2("https://user:pw@example.org/x.tgz", self.work, self.work))
    self.assertEqual(seen, [("https://example.org/x.tgz", "Basic dXNlcjpwdw==")])


  def test_a_body_cut_short_is_not_kept(self):
    import io
    asked = []

    class Cut(io.BytesIO):
      headers = {"Content-Length": "10"}

    def urlopen(req, *a, **kw):
      asked.append(kw.get("timeout"))
      return Cut(b"only")
    with patch("bits_helpers.download.urlopen", side_effect=urlopen):
      self.assertFalse(dl.downloadUrllib2("https://example.org/x.tgz", self.work, self.work))
    self.assertEqual(os.listdir(self.work), [])          # no file, no .tmp left
    self.assertEqual(asked, [dl.URL_TIMEOUT])


if __name__ == "__main__":
  unittest.main()
