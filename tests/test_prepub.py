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

    def test_submit_signs_and_does_not_send_the_token(self):
        """By default the request is signed and the secret never travels.

        The point of signing is that observing a request yields nothing
        reusable, so this asserts BOTH halves: a signature is present, and the
        Authorization header is absent. Sending both would hand an observer the
        very credential the signature exists to protect.
        """
        from bits_helpers import httpsig
        from bits_helpers.prepub import submit_job
        tar = self._make_tar()
        try:
            submit_job(self.url, "my-secret-token", "software.cern.ch", "atlas/24.0", tar)
            headers = self.server.last_post_headers
            sig = headers.get(httpsig.HEADER_NAME, "")
            self.assertTrue(sig.startswith("v1 key_id=prepub "), sig)
            self.assertEqual(headers.get("Authorization", ""), "")
            self.assertNotIn("my-secret-token", sig)
        finally:
            os.unlink(tar)

    def test_submit_signature_binds_the_fields_and_payload(self):
        """The signature must commit to the exact fields and tar sent."""
        from bits_helpers import httpsig
        from bits_helpers.prepub import sha256_file, submit_job
        tar = self._make_tar()
        try:
            submit_job(self.url, "my-secret-token", "software.cern.ch", "atlas/24.0", tar)
            parts = dict(kv.split("=", 1) for kv in
                         self.server.last_post_headers[httpsig.HEADER_NAME].split()[1:])
            digest = sha256_file(tar)
            self.assertEqual(parts["bh"], digest)
            self.assertEqual(parts["fd"], httpsig.fields_digest({
                "repo": "software.cern.ch",
                "path": "atlas/24.0",
                "tar_sha256": digest,
            }))
        finally:
            os.unlink(tar)

    def test_submit_bearer_auth_opt_in(self):
        """--prepub-bearer-auth restores the legacy header for an old server."""
        from bits_helpers import httpsig
        from bits_helpers.prepub import submit_job
        tar = self._make_tar()
        try:
            submit_job(self.url, "my-secret-token", "software.cern.ch", "atlas/24.0", tar,
                       bearer_auth=True)
            headers = self.server.last_post_headers
            self.assertEqual(headers.get("Authorization", ""), "Bearer my-secret-token")
            self.assertEqual(headers.get(httpsig.HEADER_NAME, ""), "")
        finally:
            os.unlink(tar)

    def test_submit_bearer_refused_without_tls_verification(self):
        """The bearer must not go over an unverified connection.

        A signature has no such restriction: the secret never leaves the
        process, so an intercepted request yields nothing reusable.
        """
        from bits_helpers.prepub import submit_job
        tar = self._make_tar()
        try:
            submit_job(self.url, "my-secret-token", "software.cern.ch", "atlas/24.0", tar,
                       bearer_auth=True, no_verify_tls=True)
            self.assertEqual(self.server.last_post_headers.get("Authorization", ""), "")
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


# ---------------------------------------------------------------------------
# The URI that is signed
# ---------------------------------------------------------------------------

class TestSignedURI(unittest.TestCase):
    """The signature must cover the request-target the SERVER sees.

    A hardcoded ``/api/v1/jobs`` is right only when prepub is at the root of
    its host. Behind a path prefix, or a reverse proxy that does not strip one,
    the request arrives as ``/prepub/api/v1/jobs`` while the client signed the
    bare path — every request 401s and neither log says why.
    """

    def test_no_prefix(self):
        from bits_helpers.prepub import _signed_uri
        self.assertEqual(_signed_uri("https://host:8080/api/v1/jobs"), "/api/v1/jobs")

    def test_path_prefix_is_included(self):
        from bits_helpers.prepub import _signed_uri
        self.assertEqual(_signed_uri("https://host/prepub/api/v1/jobs"),
                         "/prepub/api/v1/jobs")

    def test_query_string_is_included(self):
        from bits_helpers.prepub import _signed_uri
        self.assertEqual(_signed_uri("https://host/api/v1/jobs?a=b"), "/api/v1/jobs?a=b")

    def test_empty_path(self):
        from bits_helpers.prepub import _signed_uri
        self.assertEqual(_signed_uri("https://host"), "/")

    def test_submission_signs_the_prefixed_uri(self):
        """End to end: a prefixed base URL yields a signature over the prefix."""
        from bits_helpers import httpsig
        from bits_helpers.prepub import _signed_uri, submit_job

        server, url = _start_fake_prepub()
        # Serve the API under a prefix by making the stub answer that path too.
        try:
            fd, tar = tempfile.mkstemp(suffix=".tar")
            os.close(fd)
            with open(tar, "wb") as fh:
                fh.write(b"payload")
            try:
                # The stub only knows /api/v1/jobs, so submit against the plain
                # URL but assert the header commits to whatever _signed_uri says
                # — which is the value the request is actually sent to.
                submit_job(url, "sekrit", "software.cern.ch", "p/1", tar)
                raw = server.last_post_headers.get(httpsig.HEADER_NAME)
                self.assertIsNotNone(raw, "no signature header was sent")
                uri = _signed_uri(f"{url}/api/v1/jobs")
                fields = {"repo": "software.cern.ch", "path": "p/1",
                          "tar_sha256": _sha256(b"payload")}
                parts = dict(p.split("=", 1) for p in raw.split()[1:])
                expect = httpsig.canonical(
                    "POST", uri, httpsig.fields_digest(fields),
                    _sha256(b"payload"), int(parts["ts"]), parts["nonce"])
                import hmac as _hmac
                self.assertEqual(
                    parts["mac"],
                    _hmac.new(b"sekrit", expect.encode(), hashlib.sha256).hexdigest(),
                    "the MAC does not cover the URI the request was sent to")
            finally:
                os.unlink(tar)
        finally:
            server.shutdown()


class TestSignedRequestsAreNotReplayed(unittest.TestCase):
    """urllib3 must not retry a signed request.

    A retry replays the request VERBATIM, headers included. A signature is
    single-use and time-bounded, so the replay is refused as a nonce reuse:
    a transient 503 would reach the caller as a 401 that no amount of reading
    the server log explains. Retrying is done by the call sites instead, which
    re-sign each attempt.
    """

    def test_signed_session_has_no_urllib3_retries(self):
        from bits_helpers.prepub import _make_session
        s = _make_session(signed=True)
        for prefix in ("http://", "https://"):
            self.assertEqual(s.get_adapter(prefix + "x").max_retries.total, 0,
                             f"{prefix}: a signed session must not replay requests")

    def test_bearer_session_keeps_retrying(self):
        from bits_helpers.prepub import _make_session
        s = _make_session(signed=False)
        self.assertGreater(s.get_adapter("https://x").max_retries.total, 0)

    def test_submit_retries_with_a_fresh_signature(self):
        """A 5xx is retried, and the retry carries a DIFFERENT nonce."""
        from bits_helpers import httpsig
        from bits_helpers.prepub import submit_job

        seen = []

        class _Flaky(_FakePrepubHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                seen.append(self.headers.get(httpsig.HEADER_NAME))
                if len(seen) == 1:
                    self.send_error(503)
                    return
                resp = json.dumps({"job_id": "retried"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        server = HTTPServer(("127.0.0.1", 0), _Flaky)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        fd, tar = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        with open(tar, "wb") as fh:
            fh.write(b"payload")
        try:
            with patch("bits_helpers.prepub._SUBMIT_BACKOFF", 0):
                job_id = submit_job(url, "sekrit", "software.cern.ch", "p/1", tar)
            self.assertEqual(job_id, "retried")
            self.assertEqual(len(seen), 2, "the 503 was not retried")
            nonce = lambda h: dict(p.split("=", 1) for p in h.split()[1:])["nonce"]
            self.assertNotEqual(nonce(seen[0]), nonce(seen[1]),
                                "the retry replayed the first signature")
        finally:
            os.unlink(tar)
            server.shutdown()
