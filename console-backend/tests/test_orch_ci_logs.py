# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""POST /ops/ci/logs: a build job's host journal into VictoriaLogs, named by its token."""

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import ci_auth, config, main
from console_backend.orch import api, ci_logs
from test_orch_reconcile import _DB

CLAIMS = {"project_id": "42", "ref_protected": "true", "pipeline_id": "1", "job_id": "77",
          "runner_id": "37747"}


def entry(msg, **kw):
    return json.dumps(dict({"MESSAGE": msg, "__REALTIME_TIMESTAMP": "1795361936218000",
                            "_HOSTNAME": "pcphsft204", "_SYSTEMD_UNIT": "gitlab-runner.service",
                            "PRIORITY": "6", "_CMDLINE": "secret-cmdline", "__CURSOR": "s=1"}, **kw))


class CiLogsTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_FORGE_PROJECT": "42",
            "BITS_ORCH_DB": self.path, "BITS_ADMINS_POLICY": "* @carol",
            "BITS_LOGS_URL": "http://victorialogs:9428"})
        self.client = TestClient(main.app)
        self.gl.add(1, community="testbed")
        self._pass()
        self.written = []
        api._logs_sent.clear()
        p = patch.object(ci_logs, "write", lambda base, lines: self.written.append((base, lines)))
        p.start()
        self.addCleanup(p.stop)

    def post(self, body, claims=CLAIMS):
        with patch.object(ci_auth, "verify_ci_token", lambda *a, **k: claims):
            return self.client.post("/ops/ci/logs", content=body,
                                    headers={"Authorization": "Bearer jwt"})

    def test_lines_are_named_by_the_token_not_by_themselves(self):
        body = "\n".join([entry("Job succeeded job=87388958"),
                          entry("token glpat-abcdefghijklmnopqrst used", job_id="1", community="x"),
                          entry([104, 105, 255], _SYSTEMD_UNIT=None, _TRANSPORT="kernel")])
        r = self.post(body.encode())
        self.assertEqual(r.json(), {"accepted": 3})
        ((base, lines),) = self.written
        self.assertEqual(base, "http://victorialogs:9428")
        rows = [json.loads(x) for x in lines]
        for row in rows:
            self.assertEqual((row["job_id"], row["pipeline"], row["runner_id"], row["community"]),
                             ("77", "1", "37747", "testbed"))
            self.assertNotIn("secret-cmdline", json.dumps(row))
            self.assertNotIn("__CURSOR", row)
        self.assertEqual((rows[0]["host"], rows[0]["unit"], rows[0]["_time"][:4]),
                         ("pcphsft204", "gitlab-runner.service", "2026"))
        self.assertNotIn("glpat-abcdefghijklmnopqrst", rows[1]["_msg"])
        self.assertEqual((rows[2]["_msg"], rows[2]["unit"]), ("hi�", "kernel"))

    def test_refusals(self):
        self.assertEqual(self.client.post("/ops/ci/logs", content=b"{}").status_code, 401)
        self.assertEqual(self.post(b"{}", dict(CLAIMS, project_id="7")).status_code, 403)
        self.assertEqual(self.post(b"{}", dict(CLAIMS, ref_protected="false")).status_code, 403)
        self.assertEqual(self.post(b"{}", {k: v for k, v in CLAIMS.items() if k != "job_id"}).status_code, 403)
        self.assertEqual(self.post(b"not json").status_code, 400)
        self.assertEqual(self.post(b"[1]").status_code, 400)
        self.assertEqual(self.post(b"[" * 100000).status_code, 400)
        self.assertEqual(self.post(b"x" * (ci_logs.MAX_BYTES + 1)).status_code, 413)
        many = ("\n".join([entry("m")] * (ci_logs.MAX_LINES + 1)) + "\n").encode()
        if len(many) <= ci_logs.MAX_BYTES:
            self.assertEqual(self.post(many).status_code, 413)
        self.assertEqual(self.written, [])
        self.assertEqual(self.post(b"\n").json(), {"accepted": 0})
        main.settings.logs_url = ""
        self.assertEqual(self.post(entry("m").encode()).status_code, 503)

    def test_what_is_kept_out(self):
        key = ["-----BEGIN OPENSSH PRIVATE KEY-----", "b3BlbnNzaC1rZXktdjEAAAAA", "-----END OPENSSH PRIVATE KEY-----"]
        body = "\n".join([entry("type=EXECVE argc=2 a0=\"curl\" a1=secret-arg", _TRANSPORT="audit"),
                          entry("audit: type=1327 proctitle=736563726574 a0=secret-arg")]
                         + [entry(x, _PID="9") for x in key]
                         + [entry(None), entry("x" * (ci_logs.MSG_MAX + 10)), entry(["a", "b"])])
        self.assertEqual(self.post(body.encode()).json(), {"accepted": 7})
        msgs = [json.loads(x)["_msg"] for x in self.written[0][1]]
        self.assertNotIn("secret-arg", json.dumps(msgs))
        self.assertNotIn("736563726574", msgs[0])
        self.assertEqual(msgs[2:4], ["[REDACTED: private key line]"] * 2)
        self.assertIn("--all", msgs[4])
        self.assertTrue(msgs[5].endswith("(10 characters cut)"))
        self.assertEqual(msgs[6], "a\nb")

    def test_streams_stay_few(self):
        body = "\n".join([entry("m", _SYSTEMD_UNIT="session-%d.scope" % i) for i in range(3)]
                         + [entry("m", _SYSTEMD_UNIT="u%s.service" % chr(97 + i % 26) * (i // 26 + 1))
                            for i in range(ci_logs.MAX_STREAMS + 5)])
        self.post(body.encode())
        rows = [json.loads(x) for x in self.written[0][1]]
        self.assertEqual({r["unit"] for r in rows[:3]}, {"session-N.scope"})
        self.assertEqual(rows[0]["unit_name"], "session-0.scope")
        self.assertEqual((rows[-1]["host"], rows[-1]["unit"]), ("other", "other"))
        self.written.clear()
        self.post("\n".join(entry("m", _HOSTNAME="h%d" % i) for i in range(500)).encode())
        self.assertEqual(len({(json.loads(x)["host"], json.loads(x)["unit"])
                              for x in self.written[0][1]}), ci_logs.MAX_STREAMS + 1)

    def test_a_long_message_stays_under_victorialogs_line_limit(self):
        self.post(entry("\U0001F600" * ci_logs.MSG_MAX + "\u0001" * 1000).encode())
        self.assertLess(len(self.written[0][1][0].encode()), 256 << 10)

    def test_a_job_sends_its_share(self):
        with patch.object(ci_logs, "JOB_BYTES", 3000):
            line = (entry("m" * 900) + "\n").encode()
            self.assertEqual([self.post(line * 2).status_code for _ in range(2)], [200, 429])
            self.assertEqual(self.post(line * 2, dict(CLAIMS, job_id="78")).status_code, 200)
            with patch.object(ci_logs, "write", lambda base, lines: 1 / 0):
                self.assertEqual(self.post(line * 2, dict(CLAIMS, job_id="79")).status_code, 502)
            self.assertEqual(self.post(b"[1]", dict(CLAIMS, job_id="79")).status_code, 400)
            self.assertEqual(self.post(line * 2, dict(CLAIMS, job_id="79")).status_code, 200)

    def test_a_pipeline_not_recorded_yet_has_no_community(self):
        self.post(entry("m").encode(), dict(CLAIMS, pipeline_id="999"))
        self.assertEqual(json.loads(self.written[0][1][0])["community"], "")

    def test_victorialogs_down_is_a_502_the_job_retries(self):
        def down(base, lines):
            raise OSError("connection refused")
        with patch.object(ci_logs, "write", down):
            self.assertEqual(self.post(entry("m").encode()).status_code, 502)

    def test_the_setting_is_an_address(self):
        with self.assertRaises(ValueError):
            config.Settings(env={"BITS_LOGS_URL": "http://victorialogs:9428/insert?x=1"})


class WriteTest(unittest.TestCase):
    def test_into_the_ops_tenant_as_json_lines(self):
        seen = {}

        class R:
            def raise_for_status(self):
                pass

        def post(url, **kw):
            seen.update(kw, url=url)
            return R()
        with patch.object(ci_logs.requests, "post", post):
            ci_logs.write("http://vl:9428/", ['{"_msg":"a"}'])
        self.assertEqual(seen["url"], "http://vl:9428/insert/jsonline")
        self.assertEqual(seen["headers"]["AccountID"], "0")
        self.assertEqual(seen["headers"]["Content-Type"], "application/stream+json")
        self.assertEqual(seen["data"], b'{"_msg":"a"}\n')


if __name__ == "__main__":
    unittest.main()
