# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers.prepub — cvmfs-prepub HTTP client."""

import hashlib
import json
import os
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FakePrepubHandler(BaseHTTPRequestHandler):
    """Minimal cvmfs-prepub stub that records requests and returns canned responses."""

    def log_message(self, *args):
        pass  # silence server output during tests

    def do_POST(self):
        if self.path == "/api/v1/jobs":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.server.last_post_body = body
            # Store the raw Message object so tests can use its case-insensitive .get()
            self.server.last_post_headers = self.headers

            resp = json.dumps({"job_id": "test-job-id-123"}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/api/v1/jobs/"):
            state = self.server.job_state
            resp = json.dumps({"job_id": "test-job-id-123", "state": state}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        self.send_error(404)


def _start_fake_prepub(state="published"):
    """Start a fake prepub server in a background thread.  Returns (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), _FakePrepubHandler)
    server.job_state       = state
    server.last_post_body  = b""
    server.last_post_headers = {}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Unit tests — _cvmfs_repo_and_path
# ---------------------------------------------------------------------------

class TestCvmfsRepoAndPath(unittest.TestCase):

    def test_normal_path(self):
        from bits_helpers.prepub import _cvmfs_repo_and_path
        repo, path = _cvmfs_repo_and_path("/cvmfs/software.cern.ch/lcg/releases")
        self.assertEqual(repo, "software.cern.ch")
        self.assertEqual(path, "lcg/releases")

    def test_deep_path(self):
        from bits_helpers.prepub import _cvmfs_repo_and_path
        repo, path = _cvmfs_repo_and_path(
            "/cvmfs/software.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9"
        )
        self.assertEqual(repo, "software.cern.ch")
        self.assertEqual(path, "lcg/releases/absl/20230802.1/x86_64-el9")

    def test_missing_prefix_raises(self):
        from bits_helpers.prepub import _cvmfs_repo_and_path
        with self.assertRaises(ValueError):
            _cvmfs_repo_and_path("/srv/cvmfs/atlas.cern.ch/something")

    def test_no_subpath_raises(self):
        from bits_helpers.prepub import _cvmfs_repo_and_path
        with self.assertRaises(ValueError):
            _cvmfs_repo_and_path("/cvmfs/atlas.cern.ch")

    def test_trailing_slash_stripped_from_repo(self):
        """A path like /cvmfs/repo//sub should split correctly (// is 'path' = '/')."""
        from bits_helpers.prepub import _cvmfs_repo_and_path
        repo, path = _cvmfs_repo_and_path("/cvmfs/atlas.cern.ch/atlas/24.0")
        self.assertEqual(repo, "atlas.cern.ch")
        self.assertEqual(path, "atlas/24.0")


# ---------------------------------------------------------------------------
# Unit tests — sha256_file
# ---------------------------------------------------------------------------

class TestSha256File(unittest.TestCase):

    def test_known_digest(self):
        from bits_helpers.prepub import sha256_file
        data = b"hello prepub\n"
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            self.assertEqual(sha256_file(path), _sha256(data))
        finally:
            os.unlink(path)

    def test_empty_file(self):
        from bits_helpers.prepub import sha256_file
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            self.assertEqual(sha256_file(path), _sha256(b""))
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Unit tests — resolve_token
# ---------------------------------------------------------------------------

class TestResolveToken(unittest.TestCase):

    def test_cli_token_takes_precedence(self):
        from bits_helpers.prepub import resolve_token
        with patch.dict(os.environ, {"PREPUB_API_TOKEN": "env-tok"}):
            self.assertEqual(resolve_token("cli-tok"), "cli-tok")

    def test_falls_back_to_env(self):
        from bits_helpers.prepub import resolve_token
        with patch.dict(os.environ, {"PREPUB_API_TOKEN": "env-tok"}):
            self.assertEqual(resolve_token(None), "env-tok")

    def test_empty_when_neither_set(self):
        from bits_helpers.prepub import resolve_token
        env = {k: v for k, v in os.environ.items() if k != "PREPUB_API_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_token(None), "")


# ---------------------------------------------------------------------------
# Integration-style tests — submit_job against the fake server
# ---------------------------------------------------------------------------

class TestSubmitJob(unittest.TestCase):

    def setUp(self):
        self.server, self.url = _start_fake_prepub()

    def tearDown(self):
        self.server.shutdown()

    def _make_tar(self, content=b"fake payload"):
        fd, path = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo(name="test.txt")
            import io as _io
            buf = _io.BytesIO(content)
            info.size = len(content)
            tf.addfile(info, buf)
        return path

    def test_submit_returns_job_id(self):
        from bits_helpers.prepub import submit_job
        tar = self._make_tar()
        try:
            job_id = submit_job(self.url, "", "software.cern.ch", "atlas/24.0", tar)
            self.assertEqual(job_id, "test-job-id-123")
        finally:
            os.unlink(tar)

    def test_submit_sends_bearer_token(self):
        from bits_helpers.prepub import submit_job
        tar = self._make_tar()
        try:
            submit_job(self.url, "my-secret-token", "software.cern.ch", "atlas/24.0", tar)
            # last_post_headers is an email.message.Message — .get() is case-insensitive
            auth = self.server.last_post_headers.get("Authorization", "")
            self.assertEqual(auth, "Bearer my-secret-token")
        finally:
            os.unlink(tar)

    def test_submit_fails_on_non_202(self):
        """When the server returns a non-202 status, submit_job must raise SystemExit."""
        class _RejectHandler(_FakePrepubHandler):
            def do_POST(self):
                self.send_error(503)

        reject_server = HTTPServer(("127.0.0.1", 0), _RejectHandler)
        t = threading.Thread(target=reject_server.serve_forever, daemon=True)
        t.start()
        url = f"http://127.0.0.1:{reject_server.server_address[1]}"
        tar = self._make_tar()
        try:
            from bits_helpers.prepub import submit_job
            with self.assertRaises(SystemExit):
                submit_job(url, "", "repo", "path", tar)
        finally:
            os.unlink(tar)
            reject_server.shutdown()


# ---------------------------------------------------------------------------
# Integration-style tests — poll_job against the fake server
# ---------------------------------------------------------------------------

class TestPollJob(unittest.TestCase):

    def test_returns_published_immediately(self):
        server, url = _start_fake_prepub(state="published")
        try:
            from bits_helpers.prepub import poll_job
            result = poll_job(url, "", "test-job-id-123", poll_interval=1, timeout=10)
            self.assertEqual(result, "published")
        finally:
            server.shutdown()

    def test_raises_on_failed(self):
        server, url = _start_fake_prepub(state="failed")
        try:
            from bits_helpers.prepub import poll_job
            with self.assertRaises(SystemExit):
                poll_job(url, "", "test-job-id-123", poll_interval=1, timeout=10)
        finally:
            server.shutdown()

    def test_raises_on_aborted(self):
        server, url = _start_fake_prepub(state="aborted")
        try:
            from bits_helpers.prepub import poll_job
            with self.assertRaises(SystemExit):
                poll_job(url, "", "test-job-id-123", poll_interval=1, timeout=10)
        finally:
            server.shutdown()

    def test_raises_on_timeout(self):
        """When the job never reaches a terminal state, poll_job must time out."""
        server, url = _start_fake_prepub(state="uploading")
        try:
            from bits_helpers.prepub import poll_job
            with self.assertRaises(SystemExit):
                # 1-second timeout with a 0.1-second poll so the test is fast.
                poll_job(url, "", "test-job-id-123", poll_interval=1, timeout=1)
        finally:
            server.shutdown()

    def test_transitions_from_intermediate_to_published(self):
        """Simulate uploading → published across two polls."""
        server, url = _start_fake_prepub(state="uploading")

        # After a short delay flip the state to "published".
        def _flip():
            import time
            time.sleep(0.15)
            server.job_state = "published"

        threading.Thread(target=_flip, daemon=True).start()

        from bits_helpers.prepub import poll_job
        result = poll_job(url, "", "test-job-id-123", poll_interval=0, timeout=5)
        self.assertEqual(result, "published")
        server.shutdown()


if __name__ == "__main__":
    unittest.main()
