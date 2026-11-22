# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Running builds' progress in /ops/builds, from the metrics their jobs push."""

from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import build_progress
from test_orch_reconcile import _DB
from test_store_admin import Answer


def _s(name, value, **labels):
    return {"metric": dict({"__name__": name, "instance": "h-1", "job": "bits"}, **labels),
            "value": [1795000000, str(value)]}


# Pipeline 1: job 10 (older) and its retry 11; pipeline 2 has no total yet.
AT = [_s("", 1795000000, pipeline="1", ci_job="10"), _s("", 1795000500, pipeline="1", ci_job="11"),
      _s("", 1795000400, pipeline="2", ci_job="20")]
LAST = [_s("bits_build_packages_started", 40, pipeline="1", ci_job="10"),
        _s("bits_build_packages_total", 200, pipeline="1", ci_job="10"),
        _s("bits_build_package_started", 40, pipeline="1", ci_job="10", package="Python"),
        _s("bits_build_packages_started", 7, pipeline="1", ci_job="11"),
        _s("bits_build_packages_total", 160, pipeline="1", ci_job="11"),
        _s("bits_build_package_started", 6, pipeline="1", ci_job="11", package="ROOT"),
        _s("bits_build_package_started", 7, pipeline="1", ci_job="11", package="G" * 300),
        _s("bits_build_packages_started", 3, pipeline="2", ci_job="20"),
        _s("bits_build_packages_ended", 1, pipeline="9", ci_job="90")]   # no time: left out


class ProgressTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_FORGE_PROJECT": "42",
            "BITS_ADMINS_POLICY": "testbed @alice\nother @bob\n* @carol", "BITS_ORCH_DB": self.path,
            "BITS_METRICS_URL": "http://victoriametrics:8428"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.gl.add(1, community="testbed")
        self.gl.add(2, community="other")
        self._pass()
        self.asked, self.down, self.last = [], None, LAST

        def get(session, url, params=None, timeout=None, allow_redirects=True):
            self.asked.append((url, params, session.trust_env))
            if self.down:
                raise self.down
            rows = AT if params["query"].startswith("tlast") else self.last
            return Answer(200, {"status": "success", "data": {"result": rows}})
        p = patch.object(requests.Session, "get", get)
        p.start()
        self.addCleanup(p.stop)

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_the_job_that_pushed_last(self):
        got = build_progress.for_pipelines("http://vm:8428", [2, 1, 1])
        self.assertEqual(got, {1: {"started": 7, "total": 160, "package": "G" * 100, "ended": False,
                                   "at": 1795000500}})
        (url, params, trust_env), (_, last, _) = self.asked
        self.assertEqual((url, trust_env, params["nocache"]), ("http://vm:8428/api/v1/query", False, "1"))
        self.assertEqual(params["query"], 'tlast_over_time(bits_build_packages_started{pipeline=~"1|2"}[1h])')
        self.assertIn('{__name__=~"bits_build_packages_(started|total|ended)|bits_build_package_started",'
                      'pipeline=~"1|2"}[1h]', last["query"])
        self.assertEqual(build_progress.for_pipelines("http://vm:8428", []), {})

    def test_a_build_done(self):
        self.last = LAST + [_s("bits_build_packages_ended", 1, pipeline="1", ci_job="11")]
        self.assertIs(build_progress.for_pipelines("http://vm:8428", [1])[1]["ended"], True)

    def test_running_builds_carry_it_when_asked(self):
        self.gl.add(3, community="testbed")
        self._pass()
        self.conn.execute("UPDATE pipelines SET outcome = 'success' WHERE id = 3")
        self.conn.commit()
        self.assertNotIn("progress", self._get("carol", "/ops/builds").json()["builds"][0])
        self.assertEqual(self.asked, [])                 # not asked: VictoriaMetrics not asked
        rows = {b["pipeline"]: b for b in self._get("carol", "/ops/builds?progress=1").json()["builds"]}
        self.assertEqual(rows[1]["progress"]["started"], 7)
        self.assertNotIn("progress", rows[2])            # no total pushed yet
        self.assertNotIn("progress", rows[3])            # not running
        self.assertEqual(self.asked[0][1]["query"].count("|"), 1)
        self.assertEqual(self._get("alice", "/ops/builds/1").json()["progress"]["total"], 160)

    def test_without_victoriametrics_the_builds_still_come(self):
        self.down = requests.ConnectionError("refused")
        r = self._get("carol", "/ops/builds?progress=1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("progress", r.json()["builds"][0])
        self.assertEqual(len(self.asked), 1)             # the first failure ends it
        main.settings.metrics_url = ""
        self.asked.clear()
        self.assertEqual(self._get("alice", "/ops/builds/1").status_code, 200)
        self.assertEqual(self.asked, [])

    def test_an_odd_answer_is_unreachable(self):
        self.last = [{"metric": {"pipeline": "1", "ci_job": "11"}, "value": [1, "1"]}]   # no __name__
        with self.assertRaises(build_progress.store_series.Unreachable):
            build_progress.for_pipelines("http://vm:8428", [1])
