# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits login: asks the console for a store token, shows the approve link, polls
with its own secret and writes ~/.bits/token readable by its owner only."""

import io
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import sign_console, store_login


class TestStoreLogin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, ".bits", "token")

    def _run(self, results, request=(200, {"request_id": "r1", "poll": "s3cret", "code": "K7Q2MX"}),
             console="https://bits.cern.ch/"):
        calls, shown, res = [], {}, iter(results)

        def call(url, obj=None):
            calls.append((url, obj))
            if url.endswith("/store/cli/request"):
                return request
            r = next(res)
            return r if isinstance(r, tuple) else (200, r)
        with patch.object(sign_console, "_json_call", call), \
             patch.object(sign_console, "_print_qr", lambda u: shown.__setitem__("url", u)), \
             patch.object(store_login.time, "sleep", lambda s: None), \
             patch("sys.stdout", new_callable=io.StringIO):
            user = store_login.login(console, "lcg", self.file, timeout=10)
        return user, calls, shown

    def test_writes_the_token(self):
        user, calls, shown = self._run([{"status": "pending"},
                                        {"status": "approved", "token": "tok.en.x", "user": "alice",
                                         "expires": 1800000000}])
        self.assertEqual(user, "alice")
        self.assertEqual(shown["url"], "https://bits.cern.ch/approve?store=r1")
        self.assertEqual(calls[0][0], "https://bits.cern.ch/store/cli/request")
        self.assertEqual(calls[0][1]["group"], "lcg")
        # polls go to the request's result, with the poll secret in the body
        self.assertEqual(calls[1], ("https://bits.cern.ch/store/cli/r1/result", {"poll": "s3cret"}))
        with open(self.file) as fh:
            self.assertEqual(fh.read(), "tok.en.x\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ["token"])   # no temp file left

    def test_replaces_a_readable_file_with_a_private_one(self):
        os.makedirs(os.path.dirname(self.file))
        with open(self.file, "w") as fh:
            fh.write("old\n")
        os.chmod(self.file, 0o644)
        old = os.umask(0)
        try:
            self._run([{"status": "approved", "token": "new", "user": "a", "expires": 0}])
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        with open(self.file) as fh:
            self.assertEqual(fh.read(), "new\n")

    def test_refusals_exit_without_a_file(self):
        for results in ([{"status": "refused"}], [{"status": "delivered"}], [(404, {})],
                        [(400, {"detail": "bad"})]):
            with self.assertRaises(SystemExit, msg=results):
                self._run(results)
        with self.assertRaises(SystemExit):
            self._run([], request=(403, {"detail": "no"}))
        with self.assertRaises(SystemExit):
            self._run([], request=(200, {"request_id": "r1"}))     # no poll secret
        with self.assertRaises(SystemExit):                       # timeout
            with patch.object(store_login.time, "monotonic", side_effect=[0, 0, 100]):
                self._run([{"status": "pending"}] * 5)
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(os.listdir(os.path.dirname(self.file)), [])          # temp files removed

    def test_an_unwritable_file_stops_before_asking(self):
        os.makedirs(self.file)                       # the token file is a directory
        with self.assertRaises(SystemExit):
            self._run([])                            # no request: _run's fake would record it
        os.rmdir(self.file)
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with self.assertRaises(SystemExit) as cm:
            self._run([{"status": "delivered"}])
        self.assertIn("run bits login again", str(cm.exception))

    def test_transient_errors_keep_polling(self):
        user, _, _ = self._run([(502, {}), (0, {}), {"status": "approving"},
                                {"status": "approved", "token": "t", "user": "bob", "expires": 0}])
        self.assertEqual(user, "bob")

    def test_refuses_plain_http_console(self):
        with self.assertRaises(SystemExit):
            store_login.login("http://bits.example", "lcg", self.file)

    def test_needs_a_console(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("bits_helpers.site.get", lambda *a, **k: None), \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                store_login.main(["--group", "lcg"])


if __name__ == "__main__":
    unittest.main()
