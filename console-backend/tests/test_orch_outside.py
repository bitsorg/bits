# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M4, second half: the outside copy of the audit chain."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from console_backend import config
from console_backend.orch import db, events, outside

# The shared test vector of bits-services/audit-log/test_auditlog.py: the
# receiver's hash function must stay this one.
VECTOR = [
    ({"seq": 1, "ts_ms": 1760000000000, "kind": "worker.started", "subject": "worker",
      "actor_kind": "worker", "actor_id": "orchestrator", "cause": None,
      "payload": '{"schema":5,"note":"é"}'},
     "d8e5ed955e51314b180e89473c75025f3b81370fe49e9df3a8ce0d4b826fb264"),
    ({"seq": 2, "ts_ms": 1760000000001, "kind": "shadow.match", "subject": "certification:7",
      "actor_kind": "worker", "actor_id": "orchestrator", "cause": "action:3", "payload": "{}"},
     "1790c4478017c426a5d33654235a2a38f36a4eb22dacf545d9aec3b84c9db2e0"),
]


class FakeOutside:
    """The audit-log service's rules, in memory."""

    def __init__(self):
        self.rows = []

    def head(self):
        if not self.rows:
            return {"seq": 0, "hash": events.GENESIS, "count": 0}
        return {"seq": self.rows[-1]["seq"], "hash": self.rows[-1]["hash"]}

    def send(self, rows):
        for r in rows:
            seq = len(self.rows)
            if r["seq"] <= seq:
                if self.rows[r["seq"] - 1] != r:
                    return False, {"error": "event %d differs" % r["seq"], "head": self.head()}
                continue
            prev = self.rows[-1]["hash"] if self.rows else events.GENESIS
            if r["seq"] != seq + 1 or r["prev_hash"] != prev or events._digest(prev, r) != r["hash"]:
                return False, {"error": "does not link", "head": self.head()}
            self.rows.append(dict(r))
        return True, {"head": self.head()}

    def events(self, after, limit=1000):
        return [dict(r) for r in self.rows[after:after + limit]]


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = db.connect(os.path.join(self.dir, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.out = FakeOutside()

    def add(self, n):
        for i in range(n):
            with db.transaction(self.conn):
                events.append(self.conn, "test.event", "s", events.WORKER, {"i": i})

    def kinds(self):
        return [e["kind"] for e in events.since(self.conn, limit=1000)]


class VectorTest(unittest.TestCase):
    def test_the_receiver_hashes_as_the_chain_does(self):
        prev = events.GENESIS
        for row, want in VECTOR:
            self.assertEqual(events._digest(prev, row), want)
            prev = want


class ShipTest(_DB):
    def test_events_are_sent_in_batches_after_the_outside_head(self):
        self.add(7)
        self.assertEqual(outside.ship(self.conn, self.out, batch=3), 7)
        self.assertEqual(self.out.head()["seq"], 7)
        self.assertEqual(outside.ship(self.conn, self.out), 0)
        self.add(2)
        self.assertEqual(outside.ship(self.conn, self.out), 2)
        self.assertEqual(outside.status(self.conn)["outside"]["seq"], 9)
        self.assertEqual(outside.verify(self.conn, self.out), (9, None))
        self.assertEqual(outside.ship(self.conn, None), 0)

    def test_a_rewritten_event_stops_shipping_until_cleared(self):
        self.add(3)
        outside.ship(self.conn, self.out)
        self.conn.execute("UPDATE events SET hash = ? WHERE seq = 3", ("f" * 64,))
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            self.assertEqual(outside.ship(self.conn, self.out), 0)
        alarm = outside.status(self.conn)["alarm"]
        self.assertIn("event 3 changed", alarm["reason"])
        self.assertEqual(self.kinds().count("outside.diverged"), 1)
        n = self.out.head()["seq"]
        self.assertEqual(outside.ship(self.conn, self.out), 0)          # stays stopped
        self.assertEqual((self.out.head()["seq"], self.kinds().count("outside.diverged")), (n, 1))
        self.assertIn("differs", outside.verify(self.conn, self.out)[1])
        self.assertTrue(outside.clear(self.conn, "carol"))
        self.assertFalse(outside.clear(self.conn, "carol"))
        self.assertIn("outside.cleared", self.kinds())

    def test_the_outside_head_is_checked_against_this_chain(self):
        self.add(3)
        outside.ship(self.conn, self.out)
        self.out.rows[2]["hash"] = "e" * 64          # (the fake keeps it; the service would not)
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            outside.ship(self.conn, self.out)
        self.assertIn("differs from the outside copy", outside.status(self.conn)["alarm"]["reason"])

    def test_an_edit_that_keeps_the_hashes_is_found_by_the_daily_walk(self):
        self.add(3)
        outside.ship(self.conn, self.out)
        self.conn.execute("UPDATE events SET payload = '{\"i\":7}' WHERE seq = 2")
        self.assertEqual(outside.ship(self.conn, self.out), 0)     # not seen by the quick check
        self.assertIsNone(outside.status(self.conn)["alarm"])
        self.conn.execute("UPDATE kv SET value = json_set(value, '$.full_ms', 0)"
                          " WHERE key = 'outside.verified'")
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            outside.ship(self.conn, self.out)
        self.assertIn("breaks at event 2", outside.status(self.conn)["alarm"]["reason"])
        self.assertIn("differs", outside.verify(self.conn, self.out)[1])

    def test_an_outside_copy_that_lost_events(self):
        self.add(4)
        outside.ship(self.conn, self.out)
        del self.out.rows[2:]
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            self.assertEqual(outside.ship(self.conn, self.out), 0)
        self.assertIn("went back from event 4 to 2", outside.status(self.conn)["alarm"]["reason"])
        outside.clear(self.conn, "carol")                   # it was re-seeded on purpose
        self.assertGreater(outside.ship(self.conn, self.out), 0)
        self.assertEqual(outside.verify(self.conn, self.out)[1], None)

    def test_batches_stay_under_the_services_body_limit(self):
        for i in range(6):
            with db.transaction(self.conn):
                events.append(self.conn, "big", "s", events.WORKER, {"x": "y" * 60000})
        sizes = []
        send = self.out.send
        self.out.send = lambda rows: (sizes.append(len(rows)), send(rows))[1]
        with patch.object(outside, "BATCH_BYTES", 130000):
            self.assertEqual(outside.ship(self.conn, self.out), 6)
        self.assertEqual(sizes, [2, 2, 2])

    def test_a_database_older_than_the_outside_copy(self):
        self.add(4)
        outside.ship(self.conn, self.out)
        self.conn.execute("DELETE FROM events WHERE seq > 2")      # restored from an old copy
        self.conn.execute("DELETE FROM kv WHERE key = 'outside.verified'")
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            outside.ship(self.conn, self.out)
        self.assertIn("older copy", outside.status(self.conn)["alarm"]["reason"])
        # The alarm itself is the local event 3 now: not the outside one.
        self.assertIn("event 3 differs", outside.verify(self.conn, self.out)[1])

    def test_a_refused_batch_is_an_alarm(self):
        self.add(2)
        self.out.send = lambda rows: (False, {"error": "nope", "head": {"seq": 0}})
        with self.assertLogs("bits_orchestrator.outside", "ERROR"):
            self.assertEqual(outside.ship(self.conn, self.out), 0)
        self.assertIn("refused", outside.status(self.conn)["alarm"]["reason"])

    def test_verify_finds_a_broken_outside_chain(self):
        self.add(3)
        outside.ship(self.conn, self.out)
        self.out.rows[1]["payload"] = '{"i":9}'
        self.assertIn("own chain breaks at 2", outside.verify(self.conn, self.out)[1])


class RecoverTest(_DB):
    def restored(self):
        self.add(4)
        outside.ship(self.conn, self.out)
        self.conn.execute("DELETE FROM events WHERE seq > 2")      # restored from an old copy
        self.conn.execute("DELETE FROM kv WHERE key = 'outside.head'")

    def test_a_restored_database_gets_back_the_events_it_lacks(self):
        self.restored()
        self.assertEqual(outside.behind(self.conn, self.out), (2, 4))
        self.assertEqual(outside.recover(self.conn, self.out, "carol"), 2)
        self.assertEqual(events.verify(self.conn), (5, None))
        self.assertEqual(self.kinds()[-1], "outside.recovered")
        self.assertEqual(self.out.rows, [outside._row(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE seq <= 4 ORDER BY seq")])
        self.assertEqual(outside.ship(self.conn, self.out), 1)      # and the chain goes on
        self.assertIsNone(outside.status(self.conn)["alarm"])
        self.assertIsNone(outside.behind(self.conn, self.out))
        self.assertEqual(outside.recover(self.conn, self.out, "carol"), 0)

    def test_the_set_is_brought_up_to_the_recovered_events(self):
        from console_backend.orch import certified
        a, b = b'{"build_id": "a", "packages": []}', b'{"build_id": "b", "packages": []}'
        with db.transaction(self.conn):
            certified.change(self.conn, add={"lcg/a.json": a})
        outside.ship(self.conn, self.out)
        copy = os.path.join(self.dir, "copy.db")
        self.conn.execute("VACUUM INTO ?", (copy,))               # the backup
        with db.transaction(self.conn):                          # a out, b in, since
            certified.change(self.conn, add={"lcg/b.json": b}, remove={"lcg/a.json"})
            events.append(self.conn, "mirror.pushed", "certified", events.WORKER,
                          {"version": 2, "commit": "c2", "added": 1, "removed": 1})
        outside.ship(self.conn, self.out)
        self.conn.close()
        self.conn = db.connect(copy)                             # restored
        with self.assertRaisesRegex(outside.OutsideError, "--mirror"):
            outside.recover(self.conn, self.out, "carol")        # b's bytes are not here
        mirror = os.path.join(self.dir, "mirror")
        os.makedirs(os.path.join(mirror, "manifests", "lcg"))
        with open(os.path.join(mirror, "manifests", "lcg", "b.json"), "wb") as fh:
            fh.write(b)
        self.conn.execute("INSERT INTO boms (sha256, body, size, first_ms) VALUES (?,?,?,0)",
                          (__import__("hashlib").sha256(b).hexdigest(), b, len(b)))
        with self.assertRaisesRegex(outside.OutsideError, "--mirror"):
            outside.recover(self.conn, self.out, "carol")        # held here is not enough
        self.assertGreater(outside.recover(self.conn, self.out, "carol", mirror=mirror), 0)
        live = [r[0] for r in self.conn.execute(
            "SELECT path FROM certified WHERE removed_version IS NULL")]
        self.assertEqual(live, ["lcg/b.json"])                   # a stays out
        self.assertEqual(list(json.loads(outside._kv(self.conn, "certify.decertified"))), ["a"])
        self.assertEqual((outside._kv(self.conn, "mirror.version"), certified.current(self.conn)[2]),
                         ("2", "c2"))                            # not pushed again
        self.assertEqual(certified.current(self.conn)[0], 2)
        with db.transaction(self.conn):                          # numbering goes on
            self.assertEqual(certified.change(self.conn, remove={"lcg/b.json"})[0], 3)

    def _set_history(self, before, after):
        """A database copied after *before*, the outside copy also after *after*;
        returns the restored copy's path."""
        before(self.conn)
        outside.ship(self.conn, self.out)
        copy = os.path.join(self.dir, "copy.db")
        self.conn.execute("VACUUM INTO ?", (copy,))
        after(self.conn)
        outside.ship(self.conn, self.out)
        self.conn.close()
        self.conn = db.connect(copy)

    def test_a_build_decertified_in_one_arch_only_is_blocked(self):
        from console_backend.orch import certified
        el9, el8 = (b'{"build_id": "B", "packages": [{"hash": "h", "tarball_sha256": "s",'
                    b' "effective_architecture": "%s"}]}' % a for a in (b"el9", b"el8"))

        def before(conn):
            with db.transaction(conn):
                certified.change(conn, add={"lcg/B.el9.json": el9, "lcg/B.el8.json": el8})

        def after(conn):
            with db.transaction(conn):
                certified.change(conn, remove={"lcg/B.el9.json"})
        self._set_history(before, after)
        outside.recover(self.conn, self.out, "carol")
        self.assertEqual(list(json.loads(outside._kv(self.conn, "certify.decertified"))), ["B"])

    def test_a_set_that_does_not_match_its_digest_is_not_recovered(self):
        from console_backend.orch import certified

        def after(conn):
            with db.transaction(conn):
                certified.change(conn, add={"lcg/a.json": b"{}"})
        self._set_history(lambda conn: None, after)
        mirror = os.path.join(self.dir, "mirror")
        os.makedirs(os.path.join(mirror, "manifests", "lcg"))
        with open(os.path.join(mirror, "manifests", "lcg", "a.json"), "wb") as fh:
            fh.write(b"{}")
        with patch.object(certified, "digest_of", return_value="forged"):
            with self.assertRaisesRegex(outside.OutsideError, "nothing recovered"):
                outside.recover(self.conn, self.out, "carol", mirror=mirror)
        self.assertEqual(certified.current(self.conn)[0], 0)

    def test_a_pipeline_given_up_on_is_followed_past(self):
        from console_backend.orch import certified

        def after(conn):
            certified._give_up(conn, 41, "c41", "test")
        self._set_history(lambda conn: None, after)
        outside.recover(self.conn, self.out, "carol")
        self.assertEqual(certified.followed(self.conn), 41)

    def test_an_outside_copy_that_does_not_link_gives_nothing(self):
        self.restored()
        self.out.rows[3]["payload"] = '{"i":99}'                     # edited outside
        with self.assertRaises(outside.OutsideError):
            outside.recover(self.conn, self.out, "carol")
        self.assertEqual(events.verify(self.conn), (2, None))

    def test_a_database_that_diverged_is_not_recovered(self):
        self.restored()
        with db.transaction(self.conn):                              # started on the old copy
            events.append(self.conn, "worker.started", "worker", events.WORKER, {})
        self.assertIsNone(outside.behind(self.conn, self.out))      # event 3 is not theirs
        self.assertEqual(outside.recover(self.conn, self.out, "carol"), 0)

    def test_the_worker_does_not_start_on_a_restored_database(self):
        from types import SimpleNamespace
        from console_backend.orch import worker
        self.restored()
        w = SimpleNamespace(outside=self.out, conn=self.conn)
        with self.assertRaises(outside.OutsideError) as cm:
            worker.Worker._check_outside(w)
        self.assertIn("recover", str(cm.exception))
        self.assertEqual(events.verify(self.conn), (2, None))        # nothing appended
        with self.assertRaises(RuntimeError), db.transaction(self.conn):
            events.append(self.conn, "approval.created", "x", events.WORKER, {})  # the console
        outside.recover(self.conn, self.out, "carol")
        worker.Worker._check_outside(w)
        with db.transaction(self.conn):
            events.append(self.conn, "worker.started", "worker", events.WORKER, {})

    def test_signing_waits_for_a_comparison_after_start(self):
        from types import SimpleNamespace
        from console_backend.orch import worker
        s = config.Settings(env={"BITS_ORCH_AUDIT_URL": "http://audit-log:8443"})
        self.add(1)
        outside.ship(self.conn, self.out)
        self.assertIsNone(outside.sign_hold(self.conn, s))

        class Down:
            def head(self):
                raise outside.OutsideError("unreachable")
        with self.assertLogs("bits_orchestrator", "WARNING"):
            worker.Worker._check_outside(SimpleNamespace(outside=Down(), conn=self.conn))
        self.assertIn("not been checked", outside.sign_hold(self.conn, s))

    def test_malformed_outside_events_are_not_imported(self):
        self.restored()
        r = dict(self.out.rows[2], actor_kind="nobody")
        r["hash"] = events._digest(r["prev_hash"], r)
        self.out.rows[2] = r
        self.out.rows[3] = dict(self.out.rows[3], prev_hash=r["hash"])
        self.out.rows[3]["hash"] = events._digest(r["hash"], self.out.rows[3])
        with self.assertRaises(outside.OutsideError):
            outside.recover(self.conn, self.out, "carol")
        self.assertEqual(events.verify(self.conn), (2, None))

    def test_signing_waits_for_the_outside_copy(self):
        s = config.Settings(env={"BITS_ORCH_AUDIT_URL": "http://audit-log:8443"})
        now = db.now_ms()
        self.assertIn("not been checked yet", outside.sign_hold(self.conn, s, now))
        self.add(1)
        outside.ship(self.conn, self.out)
        self.assertIsNone(outside.sign_hold(self.conn, s))
        self.assertIn("not been checked for", outside.sign_hold(self.conn, s, now + 11 * 60000))
        outside._alarm(self.conn, "rewritten", None)
        self.assertIn("diverged (rewritten)", outside.sign_hold(self.conn, s))
        self.assertIsNone(outside.sign_hold(self.conn, config.Settings(env={})))


class FakeHTTP:
    def __init__(self, status, body):
        self.status, self.body, self.calls = status, body, []

    def request(self, method, url, headers, json, timeout, verify):
        self.calls.append((method, url, headers, json, verify))
        st, body = self.status, self.body

        class R:
            status_code = st

            def json(self):
                return body
        return R()


class ClientTest(unittest.TestCase):
    def test_settings_token_file_and_responses(self):
        self.assertIsNone(outside.Client.from_settings(config.Settings(env={})))
        with self.assertRaises(outside.OutsideError):
            outside.Client.from_settings(config.Settings(env={"BITS_ORCH_AUDIT_URL": "https://a"}))
        for url in ("http://cvmfs-bits-01.cern.ch:8443", "http://10.0.0.1:8443"):
            with self.assertRaises(outside.OutsideError):
                outside.Client.from_settings(config.Settings(env={"BITS_ORCH_AUDIT_URL": url,
                                                                  "BITS_ORCH_AUDIT_TOKEN": "t"}))
        c = outside.Client.from_settings(config.Settings(env={      # the service on this host
            "BITS_ORCH_AUDIT_URL": "http://audit-log:8443", "BITS_ORCH_AUDIT_TOKEN": "t"}))
        self.assertEqual(c.url, "http://audit-log:8443")
        with self.assertRaises(outside.OutsideError):
            outside.Client.from_settings(config.Settings(env={"BITS_ORCH_AUDIT_URL": "https://a",
                                                              "BITS_ORCH_AUDIT_TOKEN": "/nope"}))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        tok = os.path.join(d, "t")
        with open(tok, "w") as fh:
            fh.write("sekrit\n")
        c = outside.Client.from_settings(config.Settings(env={
            "BITS_ORCH_AUDIT_URL": "https://cvmfs-bits-01:8443/", "BITS_ORCH_AUDIT_TOKEN": tok,
            "BITS_ORCH_AUDIT_CAFILE": "/ca.pem"}))
        c.http = FakeHTTP(409, {"error": "x", "head": {"seq": 1}})
        ok, data = c.send([{"seq": 1}])
        self.assertEqual((ok, data["head"]["seq"]), (False, 1))
        method, url, headers, body, verify = c.http.calls[0]
        self.assertEqual((method, url, headers["Authorization"], verify),
                         ("POST", "https://cvmfs-bits-01:8443/v1/events", "Bearer sekrit", "/ca.pem"))
        c.http = FakeHTTP(401, {"error": "a bearer token is required"})
        with self.assertRaises(outside.OutsideError):
            c.head()


if __name__ == "__main__":
    unittest.main()
