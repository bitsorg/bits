# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The site's git mirror (bits_helpers.git_mirror): git's configuration for it, and
bits' git commands through it, upstream again when they fail there."""

import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from bits_helpers import git as bits_git
from bits_helpers import git_mirror


class Health(BaseHTTPRequestHandler):
  def log_message(self, *a):
    pass

  def do_GET(self):
    self.send_response(200 if self.path == "/health" else 404)
    self.end_headers()


class GitMirrorTest(unittest.TestCase):
  def setUp(self):
    git_mirror._answers.clear()
    env = {k: v for k, v in os.environ.items()
           if k not in ("BITS_GIT_MIRROR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")}
    p = patch.dict(os.environ, env, clear=True)
    p.start()
    self.addCleanup(p.stop)

  def test_config(self):
    with patch.object(git_mirror.site, "get", lambda k, d=None: None):
      self.assertEqual(git_mirror.config(), [])                       # not configured
    os.environ["BITS_GIT_MIRROR"] = "https://store.example.org:8444/git/"
    with patch.object(git_mirror, "_up", lambda root: False):
      self.assertEqual(git_mirror.config(), [])                       # not answering
    os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ca.pem"
    with patch.object(git_mirror, "_up", lambda root: root == "https://store.example.org:8444"):
      self.assertEqual(git_mirror.config(), [
        ("url.https://store.example.org:8444/git/.insteadOf", "https://"),
        ("url.https://store.example.org:8444/.insteadOf", "https://store.example.org:8444/"),  # itself as is
        ("http.https://store.example.org:8444/.sslCAInfo", "/etc/ca.pem")])
    os.environ["BITS_GIT_MIRROR"] = "http://store.example.org/git"     # https only
    self.assertEqual(git_mirror.config(), [])

  def test_answers_asked_once(self):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = "http://127.0.0.1:%d" % srv.server_address[1]
    self.assertTrue(git_mirror._up(root))
    srv.shutdown()
    srv.server_close()
    self.assertTrue(git_mirror._up(root))                              # asked once per run
    self.assertFalse(git_mirror._up("http://127.0.0.1:1"))

  def test_insteadof_reaches_git(self):
    """The configuration git_mirror gives, passed as bits passes it, rewrites the URL."""
    d = tempfile.mkdtemp()
    self.addCleanup(lambda: __import__("shutil").rmtree(d))
    subprocess.run(["git", "init", "-q", "--bare", os.path.join(d, "up.git")], check=True)
    err, out = bits_git._git(("ls-remote", "https://example.invalid/up.git"), d, False,
                             [("url.file://%s/.insteadOf" % d, "https://example.invalid/")])
    self.assertEqual(err, 0, out)

  def test_upstream_when_the_mirror_fails(self):
    calls = []

    def run(cmd, timeout=None, **kw):
      calls.append(cmd)
      return (128, "fatal: 502") if "insteadOf" in cmd else (0, "ok")   # the mirror fails
    with patch.object(bits_git, "getstatusoutput", run), \
         patch.object(git_mirror, "config", lambda: [("url.https://m/git/.insteadOf", "https://")]):
      self.assertEqual(bits_git.git(("fetch", "origin"), "/tmp"), "ok")
      self.assertEqual(len(calls), 2)
      self.assertIn("insteadOf", calls[0])
      self.assertNotIn("insteadOf", calls[1])
      calls.clear()
      bits_git.git(("rev-parse", "HEAD"), "/tmp")                       # not a network command
      self.assertNotIn("insteadOf", calls[0])


if __name__ == "__main__":
  unittest.main()
