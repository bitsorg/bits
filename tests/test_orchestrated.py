# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits in a store whose certified set bits-orchestrator owns (MANIFESTS/orchestrator.json)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bits_helpers import certify, compliance, orchestrated, publish, stale_boms

MARKER = {"issuer": "bits-orchestrator/bkt", "manifests": "MANIFESTS/common-manifest",
          "since": "2026-10-20T08:00:00Z"}


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _S3:
    def __init__(self, objects=None, fail=None, listable=False):
        self.objects, self.fail, self.listable = dict(objects or {}), fail, listable

    def get_object(self, Bucket, Key):
        if self.fail:
            raise _Err(self.fail)
        if Key not in self.objects:
            raise _Err("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if not self.listable:
            raise _Err("AccessDenied")
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class _Parser:
    class Error(Exception):
        pass

    def error(self, msg):
        raise self.Error(msg)


class ReadTest(unittest.TestCase):

    def test_no_marker(self):
        self.assertIsNone(orchestrated.read(_S3(), "bkt"))

    def test_the_marker(self):
        s3 = _S3({orchestrated.MARKER: json.dumps(MARKER).encode()})
        self.assertEqual(orchestrated.read(s3, "bkt"), MARKER)

    def test_an_unreadable_marker_still_says_the_orchestrator_owns_the_set(self):
        m = orchestrated.read(_S3({orchestrated.MARKER: b"{not json"}), "bkt")
        self.assertEqual(m["issuer"], "bits-orchestrator")

    def test_a_store_that_cannot_say(self):
        with self.assertRaises(orchestrated.Unknown):
            orchestrated.read(_S3(fail="AccessDenied"), "bkt")

    def test_a_403_for_a_marker_the_listing_does_not_show_is_no_marker(self):
        self.assertIsNone(orchestrated.read(_S3(fail="AccessDenied", listable=True), "bkt"))

    def test_a_store_that_is_not_s3_has_no_orchestrator(self):
        self.assertIsNone(orchestrated.for_store("rsync://host/path"))


class ConsoleTest(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self.answers = []

        def call(url, obj=None, bearer=None):
            self.calls.append((url, obj, bearer))
            return self.answers.pop(0)
        self.console = orchestrated.Console("https://console.example/", "tok")
        self.console._call = call

    def test_decertify_is_requested_as_the_token_holder(self):
        self.answers = [(201, {"id": 7, "state": "approved"})]
        a = self.console.decertify("lcg", "b1", "el9", "line one\nline two")
        self.assertEqual(a["id"], 7)
        url, body, bearer = self.calls[0]
        self.assertEqual((url, bearer), ("https://console.example/ops/actions", "tok"))
        self.assertEqual(body, {"kind": "decertify", "params": {
            "group": "lcg", "build_id": "b1", "arch": "el9", "reason": "line one line two"}})

    def test_a_refused_request_says_why(self):
        self.answers = [(403, {"detail": "bob is not an admin for lcg"})]
        with self.assertRaisesRegex(orchestrated.RequestError, "403.*not an admin"):
            self.console.decertify("lcg", "b1")

    def test_settle_follows_the_action(self):
        self.answers = [(200, {"id": 7, "state": "running"}), (200, {"id": 7, "state": "done"})]
        with patch("bits_helpers.orchestrated.time.sleep"):
            a = self.console.settle({"id": 7, "state": "approved"})
        self.assertEqual(a["state"], "done")
        self.assertEqual(self.calls[-1][0], "https://console.example/ops/actions/7")

    def test_decertify_package_is_requested_as_the_token_holder(self):
        self.answers = [(201, {"id": 8, "state": "approved"})]
        self.console.decertify_package("lcg", "Secret", "compliance")
        url, body, bearer = self.calls[0]
        self.assertEqual((url, bearer), ("https://console.example/ops/actions", "tok"))
        self.assertEqual(body, {"kind": "decertify-package", "params": {
            "group": "lcg", "package": "Secret", "reason": "compliance"}})

    def test_the_groups_of_the_set(self):
        self.answers = [(200, {"groups": {"ship": {}, "lcg": {}}, "entries": []})]
        self.assertEqual(self.console.groups(), ["lcg", "ship"])

    def test_the_outcome_of_a_decertify(self):
        out = orchestrated.Console.outcome
        self.assertIn("2 BOM(s) without it", out({"state": "done", "result": {
            "boms_replaced": 2, "boms_removed": 1}})[1])
        self.assertEqual(out({"state": "done", "result": None}), (True, "done"))
        self.assertTrue(out({"state": "approved"})[0])
        ok, words = out({"state": "done", "result": json.dumps({"still_certified_by": ["ship"]})})
        self.assertTrue(ok)
        self.assertIn("still certified by ship", words)
        ok, words = out({"state": "failed", "error": "not in lcg's certified set"})
        self.assertFalse(ok)
        self.assertIn("not in lcg", words)

    def test_a_token_does_not_follow_a_redirect(self):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        from bits_helpers import sign_console
        code, _ = sign_console._json_call("http://127.0.0.1:%d/ops/x" % srv.server_port,
                                          bearer="tok")
        srv.server_close()
        self.assertEqual((code, seen), (302, [("/ops/x", "Bearer tok")]))

    def test_a_token_is_never_sent_in_clear(self):
        console = orchestrated.Console("http://console.example", "tok")
        with self.assertRaisesRegex(orchestrated.RequestError, "non-https"):
            console.decertify("lcg", "b1")


class CertifyTest(unittest.TestCase):

    def args(self, **over):
        base = dict(fromManifest="latest", dryRun=False, workDir="/tmp/sw", architecture="el9",
                    publishStore="https://s3.example/bkt", certifyGroup="lcg",
                    manifestsRemote=None, certifyRef=None, gitlabToken=None,
                    approval="passkey", console="https://c", consoleCafile=None,
                    consoleInsecure=False, certifier=None)
        base.update(over)
        return SimpleNamespace(**base)

    def run_certify(self, marker, **over):
        bom = [("el9", {"build_id": "bid", "packages": []})]
        with patch.object(publish, "_orchestrator_marker", return_value=marker), \
             patch.object(publish, "_publish_from_manifest", return_value=("bid", bom, {})), \
             patch.object(publish, "_submit_certification_mr") as mr, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value=None), \
             patch("bits_helpers.sign_console.preapprove_via_console") as pre:
            publish.doCertify(self.args(**over), _Parser())
        return pre, mr

    def test_the_preapproval_is_the_end_no_token_no_mr(self):
        pre, mr = self.run_certify(MARKER)
        pre.assert_called_once()
        mr.assert_not_called()

    def test_approval_none_only_publishes(self):
        pre, mr = self.run_certify(MARKER, approval="none")
        pre.assert_not_called()
        mr.assert_not_called()

    def test_unmarked_still_needs_the_token_for_the_mr(self):
        with self.assertRaises(_Parser.Error):
            self.run_certify(None)

    def test_a_store_that_cannot_say_is_certified_through_an_mr(self):
        with patch("bits_helpers.orchestrated.for_store",
                   side_effect=orchestrated.Unknown("cannot read")), \
             patch.object(publish, "warning") as warn:
            self.assertIsNone(publish._orchestrator_marker("b3://bkt", "/tmp/sw", "el9"))
        self.assertIn("merge request", warn.call_args[0][0])


class SignTest(unittest.TestCase):

    def test_sign_refuses_a_store_the_orchestrator_signs(self):
        with patch("bits_helpers.orchestrated.for_store", return_value=MARKER):
            with self.assertRaisesRegex(_Parser.Error, "bits-orchestrator/bkt signs"):
                certify._refuse_orchestrated("b3://bkt", "/tmp/sw", "el9", _Parser())

    def test_sign_refuses_a_store_that_cannot_say(self):
        with patch("bits_helpers.orchestrated.for_store",
                   side_effect=orchestrated.Unknown("x")):
            with self.assertRaisesRegex(_Parser.Error, "cannot tell"):
                certify._refuse_orchestrated("b3://bkt", "/tmp/sw", "el9", _Parser())


class EnforceTest(unittest.TestCase):
    """--enforce in a store the orchestrator certifies: decertify, never sign."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        with open(os.path.join(self.d, "secret.sh"), "w") as fh:
            fh.write("package: Secret\nversion: \"1\"\nlicense: LicenseRef-Secret\n"
                     "redistributable: false\n---\n")
        self.rec = compliance.scan_recipes(self.d)
        self.objects = {
            orchestrated.MARKER: json.dumps(MARKER),
            "MANIFESTS/b1/h-1.el9.json": json.dumps({"build_id": "b1", "packages": [
                {"package": "Secret", "hash": "beef01", "effective_architecture": "el9"},
                {"package": "Good", "hash": "aa11", "effective_architecture": "el9"}]}),
        }
        self.deleted = []
        outer = self

        class S3:
            def get_paginator(self, _m):
                def paginate(Bucket, Prefix=""):
                    return [{"Contents": [{"Key": k} for k in sorted(outer.objects)
                                          if k.startswith(Prefix) and k not in outer.deleted]}]
                return SimpleNamespace(paginate=paginate)

            def get_object(self, Bucket, Key):
                if Key not in outer.objects:
                    raise _Err("NoSuchKey")
                return {"Body": io.BytesIO(outer.objects[Key].encode())}

            def delete_object(self, Bucket, Key):
                outer.deleted.append(Key)

            def put_object(self, Bucket, Key, Body):
                outer.objects[Key] = Body.decode()

        c = patch.object(compliance, "_s3_client", return_value=(S3(), "bkt", "https://s3.x"))
        c.start()
        self.addCleanup(c.stop)
        env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"})
        env.start()
        self.addCleanup(env.stop)

    class Console:
        def __init__(self, groups=("lcg",), state="done"):
            self._groups, self.state, self.asked = list(groups), state, []

        def groups(self):
            return self._groups

        def check(self):
            pass

        outcome = staticmethod(orchestrated.Console.outcome)

        def decertify_package(self, group, package, reason):
            self.asked.append((group, package))
            return {"id": len(self.asked), "state": "approved"}

        def settle(self, action, timeout=60):
            return dict(action, state=self.state,
                        result={"boms_replaced": 1, "boms_removed": 0})

    def test_the_package_is_decertified_in_each_group(self):
        console = self.Console(groups=("lcg", "ship"))
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  console=console), 0)
        self.assertEqual(console.asked, [("lcg", "Secret"), ("ship", "Secret")])
        kept = json.loads(self.objects["MANIFESTS/b1/h-1.el9.json"])      # purged as before
        self.assertEqual([p["package"] for p in kept["packages"]], ["Good"])

    def test_a_package_only_the_signed_manifests_still_list_is_decertified(self):
        del self.objects["MANIFESTS/b1/h-1.el9.json"]
        self.objects["MANIFESTS/common-manifest-el9.json"] = json.dumps({"packages": [
            {"package": "secret", "hash": "beef01", "group": "lcg"},
            {"package": "Good", "hash": "aa11", "group": "lcg"}]})
        console = self.Console()
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  console=console), 0)
        self.assertEqual(console.asked, [("lcg", "secret")])

    def test_a_decertify_still_waiting_for_the_worker_is_requested(self):
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  console=self.Console(state="approved")), 0)

    def test_no_group_to_decertify_in_fails_before_anything_is_touched(self):
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  console=self.Console(groups=())), 1)
        self.assertEqual(json.loads(self.objects["MANIFESTS/b1/h-1.el9.json"])["packages"][0]
                         ["package"], "Secret")

    def test_a_decertify_that_fails_fails_the_enforcement(self):
        console = self.Console(state="failed")
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  console=console), 1)

    def test_a_key_is_refused_before_anything_is_touched(self):
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  key_pem="k.pem", console=self.Console()), 1)
        self.assertEqual(self.deleted, [])

    def test_no_console_is_refused_before_anything_is_touched(self):
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d), 1)
        self.assertEqual(self.deleted, [])

    def test_a_dry_run_needs_no_console(self):
        self.assertEqual(compliance.enforce_store("b3://bkt", self.rec, self.d,
                                                  dry_run=True), 0)
        self.assertEqual(self.deleted, [])


class StaleBomTargetsTest(unittest.TestCase):

    def test_group_from_the_directory_arch_as_the_set_files_it(self):
        bom = {"build_id": "b1", "packages": [{"hash": "h", "tarball_sha256": "s",
                                               "effective_architecture": "el9"}]}
        wanted, bad = stale_boms.decertify_targets({
            "/c/manifests/lcg/b1.el9.x.json": bom,
            "/c/manifests/lcg/b1.el9.y.json": bom,
            "/c/elsewhere/b1.json": bom,
            "/c/manifests/lcg/noid.json": dict(bom, build_id="noid.json"),
            "/c/manifests/lcg/empty.json": {"build_id": "b2", "packages": []}})
        self.assertEqual(wanted, {("lcg", "b1", "el9"): ["/c/manifests/lcg/b1.el9.x.json",
                                                         "/c/manifests/lcg/b1.el9.y.json"]})
        self.assertEqual(bad, ["/c/elsewhere/b1.json", "/c/manifests/lcg/empty.json",
                               "/c/manifests/lcg/noid.json"])


if __name__ == "__main__":
    unittest.main()
