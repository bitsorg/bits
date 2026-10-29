# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits trace: a CVMFS file's package, and the backend's answer shown."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from bits_helpers import trace

META = {"package": {"name": "ROOT", "version": "v6.40.02", "revision": "2", "hash": "8687"},
        "effective_architecture": "x86_64-el9-gcc15-opt", "build_id": "release_x",
        "bits_version": "0.6", "dist": {"commit": "43badb4296a8"}}


class TraceTest(unittest.TestCase):
    def test_split_cvmfs(self):
        self.assertEqual(trace.split_cvmfs("/cvmfs/bits.cern.ch/a/b/"), ("bits.cern.ch", "a/b"))
        self.assertEqual(trace.split_cvmfs("/cvmfs/r"), ("r", ""))
        self.assertIsNone(trace.split_cvmfs("/tmp/cvmfs/r/x"))
        self.assertIsNone(trace.split_cvmfs("/cvmfs"))

    def test_package_meta_searched_upwards_within_the_repository(self):
        top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, top, True)
        pkg = os.path.join(top, "atlas", "ROOT", "v6-2")
        os.makedirs(os.path.join(pkg, "lib"))
        with open(os.path.join(pkg, ".meta.json"), "w") as fh:
            json.dump(META, fh)
        f = os.path.join(pkg, "lib", "libCore.so")
        open(f, "w").close()
        self.assertEqual(trace.package_meta(f, top=top), (pkg, META))
        self.assertEqual(trace.package_meta(os.path.join(top, "atlas"), top=top), (None, None))

    def test_the_backend_is_asked_with_the_token_and_shown(self):
        answer = {"published_path": "atlas/ROOT/v6-2", "indexed": True,
                  "publishes": [{"published_at": "2026-10-22T22:05:00Z", "replaced": False,
                                 "pipeline": 7, "known": True, "visible": True,
                                 "community": "atlas", "package": "Athena", "version": "25",
                                 "web_url": "https://gl/p/7", "requested_by": "alice",
                                 "approval": {"approver": "bob", "approved_at": "T", "via": "console",
                                              "event": {"seq": 3, "hash": "ab" * 32}}}],
                  "built_by_pipelines": [{"pipeline": 5, "known": True, "visible": False}]}
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["q"] = parse_qs(urlsplit(self.path).query)
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        info = trace.query("http://127.0.0.1:%d/" % srv.server_port, "tok", "r", "a/b", "release_x")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["q"], {"repo": ["r"], "path": ["a/b"], "build_id": ["release_x"]})
        text = trace.report("/cvmfs/r/a/b", "/cvmfs/r/atlas/ROOT/v6-2", META, info)
        self.assertIn("Package    ROOT v6.40.02-2  (x86_64-el9-gcc15-opt)", text)
        self.assertIn("approved by bob at T (passkey, console); audit event 3 abababab", text)
        self.assertIn("Built      pipeline 5 (of a community you do not administer)", text)
        with self.assertRaises(SystemExit):
            trace.query("http://127.0.0.1:1", "tok", "r", "a", "")    # nothing listens

    def test_the_token_goes_over_https_and_never_follows_a_redirect(self):
        with self.assertRaises(SystemExit) as cm:
            trace.query("http://backend.example", "tok", "r", "a", "")
        self.assertIn("https only", str(cm.exception))
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % srv.server_port)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(SystemExit) as cm:
            trace.query("http://127.0.0.1:%d" % srv.server_port, "tok", "r", "a", "")
        self.assertIn("HTTP 302", str(cm.exception))
        self.assertEqual(hits, ["Bearer tok"])                       # asked once, not followed


if __name__ == "__main__":
    unittest.main()
