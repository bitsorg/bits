# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin skeleton: setup code, session, Host and Origin guards, helper status."""

import os
import stat
import tempfile
import unittest

from fastapi.testclient import TestClient

from console_backend.admin import app as admin

ORIGIN = "http://localhost:8800"


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.t, self.said = [1000.0], []
        self.state = admin.State(now=lambda: self.t[0], write=self.said.append)
        self.asked = []
        helper = lambda request: self.asked.append(request) or {"ok": True, "signer": "loaded"}
        self.client = TestClient(admin.create_app(self.state, helper, [ORIGIN]),
                                 base_url="http://localhost:8800")

    def login(self, code, origin=ORIGIN):
        return self.client.post("/login", content="code=" + code, follow_redirects=False,
                                headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})

    def test_the_code_signs_in_once(self):
        self.assertEqual(self.said, [self.state.code])
        self.assertNotIn("helper", self.client.get("/").text.lower())
        code = self.state.code
        r = self.login(code.lower().replace("-", " "))  # as typed, any case or spacing
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        for part in ("HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(part.lower(), cookie.lower())
        page = self.client.get("/").text
        self.assertIn("&quot;signer&quot;: &quot;loaded&quot;", page)
        self.assertEqual(self.asked, [{"op": "status"}])
        self.assertEqual(self.said, [code, self.state.code])  # used: a new one is written
        self.assertNotEqual(self.login(code).status_code, 303)

    def test_wrong_codes_lock_out(self):
        for _ in range(admin.FAILURES):
            self.assertNotEqual(self.login("AAAA-AAAA-AAAA-AAAA").status_code, 303)
        self.assertIn("Too many wrong codes", self.login(self.state.code).text)
        self.t[0] += admin.WINDOW + 1
        self.assertEqual(self.login(self.state.code).status_code, 303)
        for _ in range(admin.FAILURES):
            self.login("AAAA-AAAA-AAAA-AAAA")
        self.state.renew_code()  # a new code clears the lockout (restart)
        self.assertEqual(self.login(self.state.code).status_code, 303)

    def test_the_code_expires(self):
        old = self.state.code
        self.t[0] += admin.CODE_TTL + 1
        self.assertNotEqual(self.login(old).status_code, 303)
        self.assertNotEqual(self.state.code, old)
        self.assertEqual(len(self.said), 2)  # the new one is written
        self.assertEqual(self.login(self.state.code).status_code, 303)

    def test_sessions_expire_and_log_out(self):
        self.login(self.state.code)
        self.t[0] += admin.SESSION_TTL + 1
        self.assertIn("Each code signs in once", self.client.get("/").text)
        self.login(self.state.code)
        self.client.post("/logout", headers={"Origin": ORIGIN})
        self.assertIn("Each code signs in once", self.client.get("/").text)

    def test_other_sites_are_refused(self):
        self.assertEqual(self.login(self.state.code, origin="http://evil.example").status_code, 403)
        self.assertEqual(self.login(self.state.code, origin="http://localhost:3000").status_code, 403)
        self.assertEqual(self.client.put("/login", headers={"Origin": "http://localhost:3000"}).status_code, 403)
        self.assertEqual(self.client.post("/login", content="code=" + self.state.code).status_code, 403)
        rebound = TestClient(admin.create_app(self.state, lambda r: {}, [ORIGIN]), base_url="http://evil.example:8800")
        self.assertEqual(rebound.get("/").status_code, 421)

    def test_headers_and_health(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.json(), {"status": "ok"})
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn(self.state.code, self.client.get("/").text)

    def test_bind(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "admin.sock")
            open(path, "w").close()
            with self.assertRaises(SystemExit):  # not a socket: left alone
                admin.bind(path)
            os.unlink(path)
            admin.bind(path).close()  # a stale socket is replaced
            sock = admin.bind(path)
            try:
                self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660)
            finally:
                sock.close()

    def test_write_code(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "setup-code")
            admin.write_code("ABCD-EFGH-JKLM-NPQR", path)
            with open(path) as f:
                self.assertEqual(f.read(), "ABCD-EFGH-JKLM-NPQR\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
            self.assertEqual(os.listdir(d), ["setup-code"])

    def test_helper_unreachable(self):
        self.assertEqual(admin.ask_helper({"op": "status"}, "/nonexistent/h.sock")["ok"], False)


if __name__ == "__main__":
    unittest.main()
