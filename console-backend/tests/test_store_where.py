# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GET /store-admin/sbom/<pipeline>: where a build's packages are kept in bits-store,
from its SBOM, by bits-store's POST /_admin/where; overall admins only."""

import hashlib
import json
import unittest
from unittest import mock

import jwt
import requests
from fastapi.testclient import TestClient

from console_backend import audit, main, store_where
from console_backend.orch import api as orch_api

from test_store_admin import Answer, _configure

H = "9a41c07e" + "0" * 32
WHL = "a" * 64
SRC = "https://root.cern/download/root_v6.36.04.source.tar.gz"
PYPI = "https://files.pythonhosted.org/p/"
BINDINGS = [{"prefix": "/store", "format": "tarballs", "public": True},
            {"prefix": "/restricted", "format": "tarballs", "gate": "group"},
            {"prefix": "/sources/archives", "format": "sources", "public": True},
            {"prefix": "/sources/restricted", "format": "sources"},
            {"prefix": "/pypi", "format": "pypi", "public": True},
            {"prefix": "/MANIFESTS", "format": "manifests", "public": True}]
ARCH = [{"name": "bits:architecture", "value": "el9_aarch64"}]
DOC = {"metadata": {"component": {"bom-ref": "release", "properties": ARCH}},
       "components": [
           {"bom-ref": "ROOT@1", "name": "ROOT", "properties": [{"name": "bits:hash", "value": H}],
            "externalReferences": [{"type": "distribution", "url": SRC},
                                   {"type": "vcs", "url": "https://GitHub.com/root-project/root.git"},
                                   {"type": "vcs", "url": "https://gitlab.cern.ch:8443/sft/x.git"}]},
           {"bom-ref": "numpy@1", "name": "NumPy_x", "properties": [{"name": "bits:hash", "value": "bb22"},
                                                                    {"name": "bits:effective_architecture", "value": "share"}],
            "externalReferences": [{"type": "distribution", "url": PYPI + "Num_Py.x-2.3.4-cp313-cp313-any.whl",
                                    "hashes": [{"alg": "SHA-256", "content": WHL}]}]},
           {"bom-ref": "six@1", "name": "six", "externalReferences": [{"type": "distribution",
            "url": "https://bits.cern.ch:8444/pypi/files/six/%s/six-1.17.0-py2.py3-none-any.whl" % WHL}]},
           {"bom-ref": "zlib@system", "name": "zlib", "properties": [{"name": "bits:provided_by", "value": "system"}]},
           {"bom-ref": "odd@1", "name": "odd", "properties": [{"name": "bits:hash", "value": "../x"}]},
           "not a component", {"name": "no ref"}]}
md5 = lambda u: hashlib.md5(u.encode()).hexdigest()


class LooksTest(unittest.TestCase):
    def test_the_places_asked(self):
        places, qs = store_where.looks(DOC, BINDINGS, "lhcb", md5)
        m = md5(SRC)
        self.assertEqual(qs, [
            {"binding": "/store", "under": "el9_aarch64/store/9a/" + H},
            {"binding": "/restricted", "under": "lhcb/el9_aarch64/store/9a/" + H},   # a gate's: the community's folder
            {"binding": "/sources/archives", "under": "%s/%s" % (m[:2], m)},
            {"binding": "/sources/restricted", "under": "%s/%s" % (m[:2], m)},
            {"binding": "/store", "under": "share/store/bb/bb22"},
            {"binding": "/restricted", "under": "lhcb/share/store/bb/bb22"},
            {"binding": "/pypi", "under": "files/num-py-x/" + WHL},
            {"binding": "/pypi", "under": "files/six/" + WHL}])       # pip read it through the store
        self.assertEqual(sorted(places), ["ROOT@1", "numpy@1", "odd@1", "six@1", "zlib@system"])
        self.assertEqual(places["zlib@system"], [{"what": "host"}])
        self.assertEqual(places["ROOT@1"][2], {"what": "git", "url": "https://GitHub.com/root-project/root.git",
                                               "repo": "github.com/root-project/root"})
        self.assertEqual(places["odd@1"], [{"what": "build", "ask": []}])       # not a folder: not asked
        # without the community, a gated binding is not asked
        _, qs = store_where.looks(DOC, BINDINGS, "", md5)
        self.assertNotIn("/restricted", [q["binding"] for q in qs])

    def test_what_was_found(self):
        places, qs = store_where.looks(DOC, BINDINGS, "lhcb", md5)
        tar = {"key": "TARS/el9_aarch64/store/9a/%s/ROOT.tar.gz" % H, "size": 3, "reads": 2}
        found = [[tar], [], [], None, [], [], [{"key": "PYPI/files/num-py-x/%s/numpy.whl" % WHL, "origin": "fill"}], []]
        out = store_where.answer(places, qs, found, [{"repo": "github.com/root-project/root", "binding": "/git", "kept": 2,
                                                      "refreshed": None, "bytes": 9}], BINDINGS)
        build, source, git, other = out["ROOT@1"]
        self.assertEqual((other["repo"], other["mirror"]), ("gitlab.cern.ch:8443/sft/x", None))   # a port kept
        self.assertEqual(build, {"what": "build", "asked": True, "objects": [dict(tar, binding="/store", public=True)]})
        self.assertEqual((source["asked"], source["objects"], source["url"]), (True, [], SRC))     # not in the store
        self.assertEqual(git["mirror"], {"binding": "/git", "kept": 2, "refreshed": None})
        self.assertEqual(out["numpy@1"][1]["objects"][0]["binding"], "/pypi")
        self.assertEqual(out["odd@1"], [{"what": "build", "asked": False, "objects": []}])
        out = store_where.answer(places, qs, found, [], BINDINGS)
        self.assertIsNone(out["ROOT@1"][2]["mirror"])


class StoreSbomTest(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app)
        self.asked, self.tokens = [], []
        self.where = None

        def fake_get(session, url, params=None, headers=None, timeout=None, allow_redirects=True):
            self.tokens.append((url.rsplit("/", 1)[1], headers["Authorization"].split()[1]))
            if url.endswith("/_admin/access"):
                return Answer(200, {"bindings": BINDINGS + ["junk", {"format": "tarballs"}]})
            return Answer(200, {"repos": []})

        def fake_post(session, url, json=None, headers=None, timeout=None, allow_redirects=True):
            self.asked.append((url, json, session.trust_env))
            self.tokens.append(("where", headers["Authorization"].split()[1]))
            if self.where:
                return self.where
            return Answer(200, {"found": [[] for _ in json["look"]]})

        async def sbom(pipeline, name, build):
            self.assertEqual((pipeline, name), (48213, "sbom.cdx.json"))
            return "ROOT-v6-1", json.dumps(DOC).encode()
        for p in (mock.patch.object(requests.Session, "get", fake_get), mock.patch.object(requests.Session, "post", fake_post),
                  mock.patch.object(orch_api, "_published_sbom", sbom),
                  mock.patch.object(orch_api, "_read", lambda fn: {"community": "lhcb"}),
                  mock.patch.object(audit, "record", lambda kind, **kw: None)):
            p.start()
            self.addCleanup(p.stop)

    def get(self, user, path="/store-admin/sbom/48213"):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_an_overall_admin(self):
        r = self.get("carol")
        self.assertEqual((r.status_code, r.headers.get("cache-control")), (200, "no-store"))
        self.assertEqual(r.json()["build"], "ROOT-v6-1")
        self.assertEqual(r.json()["places"]["ROOT@1"][0], {"what": "build", "asked": True, "objects": []})
        (url, body, trust_env), = self.asked
        self.assertEqual((url, len(body["look"]), trust_env), ("http://bits-store:8099/_admin/where", 8, False))
        caps = [(v, [(c["m"], c["p"]) for c in jwt.decode(t, options={"verify_signature": False})["cap"]])
                for v, t in self.tokens]                        # a token each: POST alone refuses an older store's
        self.assertEqual(caps, [("access", [("GET", "/_admin/access")]), ("where", [("POST", "/_admin/where")]),
                                ("git", [("GET", "/_admin/git")])])

    def test_asked_in_chunks(self):
        with mock.patch.object(store_where, "WHERE_MAX", 3):
            self.assertEqual(self.get("carol").status_code, 200)
        self.assertEqual([len(b["look"]) for _, b, _ in self.asked], [3, 3, 2])

    def test_no_git_origin_no_walk(self):
        doc = {"components": [c for c in DOC["components"] if isinstance(c, dict) and c.get("name") != "ROOT"]}

        async def sbom(pipeline, name, build):
            return "b", json.dumps(doc).encode()
        with mock.patch.object(orch_api, "_published_sbom", sbom):
            self.assertEqual(self.get("carol").status_code, 200)
        self.assertEqual([v for v, _ in self.tokens], ["access", "where"])

    def test_an_sbom_of_another_shape(self):
        for doc in ({"components": 5}, {"components": [{"bom-ref": "a", "externalReferences": 5,
                                                         "properties": {"x": 1}}]},
                    {"components": [{"bom-ref": "a", "externalReferences": [
                        {"type": "distribution", "url": "http://[x/", "hashes": 5},
                        {"type": "vcs", "url": "https://h:x/p"}]}]},
                    {"metadata": 3}, [1]):
            async def sbom(pipeline, name, build, doc=doc):
                return "b", json.dumps(doc).encode()
            with mock.patch.object(orch_api, "_published_sbom", sbom):
                r = self.get("carol")
            self.assertIn(r.status_code, (200, 502), doc)
        with mock.patch.object(store_where, "PLACES_MAX", 5):
            self.assertEqual(self.get("carol").status_code, 413)

    def test_refused(self):
        self.assertEqual(self.get("alice").status_code, 403)       # lcg's admin, not an overall one
        self.assertEqual(self.client.get("/store-admin/sbom/48213").status_code, 401)
        self.assertEqual(self.get("carol", "/store-admin/sbom/x").status_code, 422)
        self.assertEqual(self.asked, [])
        _configure(BITS_STORE_ADMIN_URL=None)
        self.assertEqual(self.get("carol").status_code, 503)

    def test_an_older_store(self):
        for answer, says in ((Answer(405, {"error": "method"}), "0.8.8"), (Answer(401, None), "0.8.8"),
                             (Answer(404, {"error": "not found"}), "no index"), (Answer(200, {"found": [[]]}), "0.8.8"),
                             (Answer(200, ValueError("not JSON")), "without JSON")):
            self.where = answer
            r = self.get("carol")
            self.assertEqual(r.status_code, 502, answer.body)
            self.assertIn(says, r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
