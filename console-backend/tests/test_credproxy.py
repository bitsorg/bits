# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""M6: the backend and the worker reach GitLab and the store through the
credentials proxy, holding only its gate tokens."""

import importlib.util
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from console_backend import authz, config, credproxy, forge_ops
from console_backend.orch import kinds


class FakeAgent:
    """The proxy's agent socket: a line with a route name in, its port and gate
    token out."""

    def __init__(self, path, routes=("gitlab-ops", "gitlab-read", "gitlab-groups", "s3"), port=4711):
        self.path, self.routes, self.port, self.asked = path, set(routes), port, []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                route = c.makefile().readline().strip()
                self.asked.append(route)
                reply = ({"port": self.port, "host": "0.0.0.0", "service": route,
                          "token": "gate-" + route} if route in self.routes
                         else {"error": "unknown service %r" % route})
                c.sendall((json.dumps(reply) + "\n").encode())

    def close(self):
        self.srv.close()


class _Proxy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = os.path.join(self.dir, "agent.sock")
        self.agent = FakeAgent(self.sock)
        self.addCleanup(self.agent.close)
        credproxy._cache.clear()
        self.addCleanup(credproxy._cache.clear)
        self.addCleanup(credproxy._clients.clear)
        self.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gitlab.cern.ch/api/v4", "BITS_FORGE_PROJECT": "42",
            "BITS_FORGE_OPS_TOKEN": "env-ops", "BITS_CRED_PROXY_AGENT_SOCKET": self.sock,
            "BITS_ORCH_STORE": "b3://lcgapp-bits-testing::rw"})


class CredProxyTest(_Proxy):
    def test_gitlab_through_the_proxy(self):
        self.assertEqual(credproxy.gitlab(self.settings, "ops"),
                         ("http://credentials-proxy:4711/api/v4", "gate-gitlab-ops"))
        self.assertEqual(credproxy.gitlab(self.settings, "read")[1], "gate-gitlab-read")
        credproxy.gitlab(self.settings, "ops")
        self.assertEqual(self.agent.asked, ["gitlab-ops", "gitlab-read"])     # cached
        self.agent.port = 4712                                                  # a restart
        self.assertEqual(credproxy.endpoint(self.settings, "gitlab-ops", refresh=True)[0],
                         "http://credentials-proxy:4712")

    def test_without_the_proxy_the_environment(self):
        s = config.Settings(env={"GITLAB_API_URL": "https://gl/api/v4",
                                 "BITS_FORGE_OPS_TOKEN": "env-ops"})
        self.assertEqual(credproxy.gitlab(s, "ops"), ("https://gl/api/v4", "env-ops"))
        self.assertEqual(credproxy.gitlab(s, "read"), ("https://gl/api/v4", "env-ops"))
        self.assertEqual(credproxy.gitlab(s, "groups"), ("", ""))
        self.assertFalse(credproxy.has(s, "groups"))
        self.assertTrue(credproxy.has(self.settings, "groups"))

    def test_a_missing_proxy_or_route_is_an_outage(self):
        self.agent.routes.discard("gitlab-groups")
        with self.assertRaises(credproxy.Unavailable) as e:
            credproxy.gitlab(self.settings, "groups")
        self.assertIn("credentials proxy", str(e.exception))
        self.settings.cred_proxy_agent_socket = os.path.join(self.dir, "none.sock")
        with self.assertRaises(OSError):
            credproxy.gitlab(self.settings, "ops")

    def test_the_forge_sends_the_gate_token_to_the_proxy(self):
        seen = []

        class R:
            status_code = 200

            def json(self):
                return {"id": 7}
        with patch.object(forge_ops.requests, "get",
                          lambda url, headers, timeout: seen.append((url, headers)) or R()):
            f = forge_ops.GitLabForge.from_settings(self.settings)
            self.assertEqual(f.get_pipeline(7), {"id": 7})
        self.assertEqual(seen, [("http://credentials-proxy:4711/api/v4/projects/42/pipelines/7",
                                 {"PRIVATE-TOKEN": "gate-gitlab-ops"})])
        self.agent.close()
        os.unlink(self.sock)
        credproxy._cache.clear()
        with self.assertRaises(forge_ops.ForgeError) as e:      # the proxy went away
            f.get_pipeline(7)
        self.assertEqual(e.exception.status, 503)

    @unittest.skipUnless(importlib.util.find_spec("boto3"), "needs boto3 (in the image)")
    def test_the_worker_reads_and_its_store_go_through_the_proxy(self):
        checked = []
        p = patch.object(kinds, "_check_bucket", lambda c, b: checked.append(b))
        p.start()
        self.addCleanup(p.stop)
        credproxy._clients.clear()
        d = kinds.Deps(self.settings)
        r = d.reader()
        self.assertEqual((r._api, r._token), ("http://credentials-proxy:4711/api/v4", "gate-gitlab-read"))
        s3, bucket = d.store()
        self.assertEqual((s3.meta.endpoint_url, bucket),
                         ("http://credentials-proxy:4711", "lcgapp-bits-testing"))
        self.assertEqual(s3._request_signer._credentials.access_key, "gate-s3")
        self.assertIs(d.store()[0], s3)                                         # kept
        credproxy._cache.clear()
        self.agent.port = 4800
        self.assertEqual(d.store()[0].meta.endpoint_url, "http://credentials-proxy:4800")
        self.assertEqual(checked, ["lcgapp-bits-testing"] * 2)                 # once per client

    @unittest.skipUnless(importlib.util.find_spec("boto3"), "needs boto3 (in the image)")
    def test_a_public_store_is_read_with_no_credentials(self):
        checked = []
        p = patch.object(kinds, "_check_bucket",
                         lambda c, b, anonymous=False: checked.append((b, anonymous)))
        p.start()
        self.addCleanup(p.stop)
        self.settings.orch_store_anonymous = True          # wins over the proxy
        d = kinds.Deps(self.settings)
        s3, bucket = d.store()
        from botocore import UNSIGNED
        self.assertEqual((s3.meta.endpoint_url, bucket), ("https://s3.cern.ch", "lcgapp-bits-testing"))
        self.assertIs(s3.meta.config.signature_version, UNSIGNED)
        self.assertNotIn("s3", self.agent.asked)
        self.assertIs(d.store()[0], s3)                                         # kept
        self.assertEqual(checked, [("lcgapp-bits-testing", True)])
        self.settings.orch_s3_endpoint = "http://s3.cern.ch"
        with self.assertRaises(kinds.NotConfigured):
            kinds.Deps(self.settings).store()

    def test_a_bucket_that_is_not_public_is_not_configured(self):
        class E(Exception):
            response = {"ResponseMetadata": {"HTTPStatusCode": 403}}

        class C:
            def list_objects_v2(self, **kw):
                raise E()
        with self.assertRaises(kinds.NotConfigured) as e:
            kinds._check_bucket(C(), "b", anonymous=True)
        self.assertIn("anonymously", str(e.exception))

    def test_paths_are_encoded_once_more_for_the_proxy(self):
        seen = []

        class R:
            status_code = 200

            def json(self):
                return {}
        with patch.object(forge_ops.requests, "get",
                          lambda url, headers, timeout: seen.append(url) or R()):
            f = forge_ops.GitLabForge.from_settings(self.settings, "read",
                                                    project="buncic/bits-manifests")
            f.finished_pipelines("main")
            f._file_exists("communities/lcg/pipelines/a.json", "main")
            direct = forge_ops.GitLabForge("https://gl/api/v4", "t", "buncic/bits-manifests")
            direct.commit("abc")
            direct.runner(7)
        self.assertEqual(seen[-1], "https://gl/api/v4/runners/7")    # not under the project
        seen.pop()
        self.assertEqual(seen, [
            "http://credentials-proxy:4711/api/v4/projects/buncic%252Fbits-manifests/pipelines"
            "?ref=main&scope=finished&order_by=id&sort=desc&per_page=50",
            "http://credentials-proxy:4711/api/v4/projects/buncic%252Fbits-manifests/repository/"
            "files/communities%252Flcg%252Fpipelines%252Fa.json?ref=main",
            "https://gl/api/v4/projects/buncic%2Fbits-manifests/repository/commits/abc"])
        self.assertEqual(credproxy.segment(self.settings, "a/b"), "a%2Fb")
        self.assertEqual(credproxy.segment(config.Settings(env={}), "a/b"), "a/b")

    def test_a_bucket_the_proxy_cannot_read_is_not_configured(self):
        class E(Exception):
            response = {"ResponseMetadata": {"HTTPStatusCode": 404}}

        class C:
            def list_objects_v2(self, **kw):
                raise E()
        with self.assertRaises(kinds.NotConfigured):
            kinds._check_bucket(C(), "b")

    def test_a_route_missing_from_the_config_is_not_a_credential(self):
        self.agent.routes.discard("gitlab-groups")
        self.assertFalse(credproxy.has(self.settings, "groups"))
        self.assertTrue(credproxy.has(self.settings, "ops"))
        self.settings.cred_proxy_agent_socket = os.path.join(self.dir, "none.sock")
        self.assertTrue(credproxy.has(self.settings, "ops"))       # down, not unconfigured

    def test_the_admin_policy_reads_groups_through_the_proxy(self):
        self.settings.admins_group = "bits/admins"
        calls = []

        def members(api, token, group, inherited=True):
            calls.append((api, token, group))
            return {"carol"}
        with patch.object(authz.forge, "gitlab_group_members", members), \
                patch.object(authz.forge, "gitlab_subgroups", lambda api, token, root: []):
            pol = authz.tree_policy(self.settings)
        # The proxy decodes the path once: the group path goes encoded once more.
        self.assertEqual(calls, [("http://credentials-proxy:4711/api/v4", "gate-gitlab-groups",
                                  "bits%2Fadmins")])
        self.assertIn("carol", pol["*"])


if __name__ == "__main__":
    unittest.main()
