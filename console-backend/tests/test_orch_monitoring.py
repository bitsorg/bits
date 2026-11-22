# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GET /ops/monitoring: push-agent's snapshot, whole for a community's admins and
developers, its overview (hosts unnamed, no per-host history) for anyone signed in."""

import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import monitoring
from test_orch_reconcile import _DB


def _snap(ts):
    hist = [{"metric": {"instance": "pcphsft204"}, "values": [[1, 5]]}]
    return {"v": 2, "ts": ts,
            "current": {"cpu_count": 8, "hosts": [{"instance": "pcphsft204", "load1": 3},
                                                  {"instance": "bits", "load1": 1}]},
            "prepub": {"hosts": [{"instance": "bits-build-01"}], "states": {"done": 2},
                       "states_by_instance": {"bits-build-01": {"done": 2}}},
            "prepub_testbed": {"hosts": [{"instance": "cvmfs-testbed"}]},
            "series": {"1h": {"step": 60, "ram": [[1, 2]], "load_hist": hist, "mem_hist": hist,
                              "prepub_load_hist": hist, "prepub_tput_hist": [],
                              "prepub_testbed": {"prepub_disk_hist": hist, "prepub_perf": {}}}}}


class MonitoringTest(_DB):
    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.dir, "snapshot.json")
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_FORGE_PROJECT": "42",
            "BITS_ADMINS_POLICY": "lcg @alice\n* @carol", "BITS_DEVELOPERS_POLICY": "lcg @erin",
            "BITS_ORCH_DB": self.path, "BITS_MONITORING_SNAPSHOT": self.file})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        with open(self.file, "w") as f:
            json.dump(_snap(time.time()), f)

    def _get(self, user, q="?community=LCG"):
        return self.client.get("/ops/monitoring" + q, headers={"Authorization": "Bearer %s" % user})

    def test_whole_for_the_communitys_admins_and_developers(self):
        for user in ("alice", "erin", "carol"):
            self.assertEqual(self._get(user).json()["current"]["hosts"][0]["instance"], "pcphsft204", user)
        self.assertEqual(self._get("carol", "").json()["prepub"]["hosts"][0]["instance"], "bits-build-01")

    def test_an_overview_for_anyone_else(self):
        for user, q in (("dave", "?community=LCG"), ("erin", "?community=cms"), ("alice", "")):
            got = self._get(user, q).json()
            self.assertEqual([h["instance"] for h in got["current"]["hosts"]], ["host 1", "host 2"])
            self.assertEqual(got["current"]["hosts"][0]["load1"], 3)
            self.assertEqual(got["prepub"]["hosts"][0]["instance"], "bits-build-01")   # as configured
            self.assertEqual(got["prepub"]["states"], {"done": 2})
            text = json.dumps(got)
            for name in ("pcphsft204", "load_hist", "disk_hist"):
                self.assertNotIn(name, text)
            self.assertEqual(sorted(got["series"]["1h"]), ["prepub_testbed", "prepub_tput_hist", "ram", "step"])

    def test_none_too_old_or_off(self):
        self.assertEqual(self.client.get("/ops/monitoring").status_code, 401)
        with open(self.file, "w") as f:
            json.dump(_snap(time.time() - monitoring.MAX_AGE_S - 5), f)
        self.assertEqual(self._get("carol").status_code, 404)
        with open(self.file, "w") as f:
            f.write("{not json")
        self.assertEqual(self._get("carol").status_code, 404)
        main.settings.monitoring_snapshot = ""
        self.assertEqual(self._get("carol").status_code, 503)


if __name__ == "__main__":
    unittest.main()
