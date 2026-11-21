# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A build's pip index (bits_helpers.pypi_index) against a local stand-in for
bits-store's public /pypi: set when it answers, the environment's own kept."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from bits_helpers import pypi_index


class Index(BaseHTTPRequestHandler):
  asked = []

  def log_message(self, *a):
    pass

  def do_GET(self):
    Index.asked.append((self.path, self.headers.get("Accept")))
    ok = self.path == "/pypi/simple/pip/" and self.headers.get("Accept") == pypi_index.ACCEPT
    self.send_response(200 if ok else 404)
    self.end_headers()


class PypiIndexTest(unittest.TestCase):
  def setUp(self):
    self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Index)
    threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    self.addCleanup(self.srv.server_close)
    self.addCleanup(self.srv.shutdown)
    self.url = "http://127.0.0.1:%d/pypi/simple" % self.srv.server_address[1]
    Index.asked = []
    pypi_index._reachable.clear()
    pypi_index._trusting.clear()
    self.inspected = []

    def inspect(cmd, **kw):                      # docker image inspect: the image's Env
      self.inspected.append(cmd[-1])
      env = ["PATH=/usr/bin"] + (["PIP_CERT=/opt/bits/ca-bundle.crt"] if cmd[-1] == "new" else [])
      return type("Done", (), {"stdout": json.dumps(env) + "\n"})()
    p = patch.object(pypi_index.subprocess, "run", inspect)
    p.start()
    self.addCleanup(p.stop)
    env = {k: v for k, v in os.environ.items()
           if k not in ("BITS_PYPI_INDEX", "PIP_INDEX_URL", "PIP_CERT", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")}
    p = patch.dict(os.environ, env, clear=True)
    p.start()
    self.addCleanup(p.stop)

  def site(self, url):
    p = patch.object(pypi_index.site, "get", lambda k, d=None: url if k == "store.pypi" else d)
    p.start()
    self.addCleanup(p.stop)

  def test_not_configured(self):
    self.site(None)
    self.assertEqual(pypi_index.build_environment(True, "new"), [])
    self.assertEqual(Index.asked, [])

  def test_answering(self):
    self.site(self.url + "/")
    os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ca.pem"
    self.assertEqual(pypi_index.build_environment(True, "new"), [("PIP_INDEX_URL", self.url)])   # the image's CA
    self.assertEqual(pypi_index.build_environment(False),
                     [("PIP_INDEX_URL", self.url), ("PIP_CERT", "/etc/ca.pem")])
    self.assertEqual(Index.asked, [("/pypi/simple/pip/", pypi_index.ACCEPT)])          # once per run

  def test_the_environment_wins(self):
    self.site(self.url)
    os.environ.update(PIP_INDEX_URL="https://mine/simple", PIP_CERT="/mine.pem", CURL_CA_BUNDLE="/ca.pem")
    self.assertEqual(pypi_index.build_environment(False), [])
    # the host's environment does not reach a container: the store is its index
    self.assertEqual(pypi_index.build_environment(True, "new"), [("PIP_INDEX_URL", self.url)])
    del os.environ["PIP_INDEX_URL"]
    self.assertEqual(pypi_index.build_environment(False), [("PIP_INDEX_URL", self.url)])   # their PIP_CERT

  def test_an_image_without_the_ca(self):
    self.site(self.url)
    self.assertEqual(pypi_index.build_environment(True, "old"), [])     # pip's own index, as before
    self.assertEqual(pypi_index.build_environment(True, "old"), [])
    self.assertEqual(pypi_index.build_environment(True, None), [])
    self.assertEqual(pypi_index.build_environment(True, "new"), [("PIP_INDEX_URL", self.url)])
    self.assertEqual(self.inspected, ["old", "new"])                   # each image asked once
    self.assertEqual(pypi_index.build_environment(False), [("PIP_INDEX_URL", self.url)])   # no image: the host

  def test_not_answering(self):
    self.site(self.url.replace("/pypi/", "/other/"))
    self.assertEqual(pypi_index.build_environment(True, "new"), [])
    closed = "http://127.0.0.1:1/pypi/simple"
    os.environ["BITS_PYPI_INDEX"] = closed                      # before the site's
    self.assertEqual(pypi_index.configured(), closed)
    self.assertEqual(pypi_index.build_environment(True, "new"), [])
    self.assertEqual(len(Index.asked), 1)


if __name__ == "__main__":
  unittest.main()
