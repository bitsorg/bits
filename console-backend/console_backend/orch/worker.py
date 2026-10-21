# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator worker: one process, one loop.

Each pass runs the steps in order. A step reads the database and the outside
world, then writes what it decided; a step that fails is logged, recorded as an
event when its error changes, and tried again on the next pass. The steps:
running due actions, then hints, discover, settle and index (reconcile.py: what
is true of builds, from GitLab and S3; with BITS_ORCH_QUEUE, after settle, dispatch
starts queued builds on free hosts, build_queue.py), with BITS_ORCH_CVMFS_STATUS the console's
CVMFS status document (cvmfs_status.py), then collect and backfill (failures.py:
every failed job's record, deduplicated into issues), then ship (outside.py: the
events to the outside copy of the audit chain) and the daily backup. Certification
in shadow (shadow.py) runs on a thread of its own, since a first comparison
hashes a whole architecture of the store; so do the worker's certifications
(certifier.py, M8a: the "certify" step only requests them). Schedules are added by
later milestones.

    python -m console_backend.orch.worker      # BITS_ORCH_DB=/orch/orch.db
"""

import json
import logging
import os
import signal
import threading
import time

from .. import config
from . import (actions, approvals, authority, build_queue, certifier, cvmfs_status, db, events,
               failures, kinds, mirror, outside, reconcile, shadow)

log = logging.getLogger("bits_orchestrator")


class Worker:
    def __init__(self, conn, settings, interval=30.0, heartbeat=None, path=None):
        self.conn = conn
        self.settings = settings
        self.interval = interval
        self.heartbeat = heartbeat
        self.path = path
        self.outside = outside.Client.from_settings(settings)
        self.shadow = None
        self.certifier = None
        self.deps = kinds.Deps(settings)
        self.authorize = authority.checker(settings)
        # Actions first, so an approved cancel never waits behind a long catch-up.
        self.steps = [
            ("actions", self._run_actions),
            ("hints", lambda: reconcile.apply_hints(self.conn, self.deps.reader())),
            ("discover", lambda: reconcile.discover(self.conn, self.deps.reader(),
                                                    settings.orch_lookback_days)),
            ("settle", lambda: reconcile.settle(self.conn, self.deps.reader())),
            ("index", lambda: reconcile.index(self.conn, self.deps.store())),
            ("collect", lambda: failures.collect(self.conn, self.deps.reader())),
            ("backfill", lambda: failures.backfill(self.conn, self.deps.reader(),
                                                   settings.orch_backfill_days,
                                                   settings.orch_lookback_days)),
            ("ship", lambda: outside.ship(self.conn, self.outside)),
            ("backup", self._backup),
        ]
        if settings.orch_certify:
            # Request the certifications of new builds, after they are
            # recorded; the certifier thread runs them.
            at = [n for n, _ in self.steps].index("index") + 1
            self.steps.insert(at, ("certify", lambda: certifier.trigger(
                self.conn, settings, self.deps.reader())))
            # Its own step: a paused or failing certify step never holds back a
            # revocation.
            self.steps.insert(at, ("withdraw", lambda: certifier.withdrawals(self.conn, settings)))
        if settings.orch_cvmfs_status:
            # The console's CVMFS status, from the builds just recorded.
            at = [n for n, _ in self.steps].index("index") + 1
            self.steps.insert(at, ("cvmfs_status", self._cvmfs_status))
        if settings.orch_queue:
            # Queued builds start on free hosts, once the running ones are settled.
            at = [n for n, _ in self.steps].index("settle") + 1
            self.steps.insert(at, ("dispatch", self._dispatch))
        if settings.orch_owns_set:
            # M8b: the set is the worker's; bits-manifests mirrors it, and the
            # store says whose manifests these are.
            self.steps.insert(len(self.steps) - 2, ("mirror", self._mirror))
            self.steps.insert(len(self.steps) - 2, ("marker", self._marker))
        self._errors = {}                 # step -> last error text
        self._stop = threading.Event()

    def _cvmfs_status(self):
        from .. import forge_ops
        forge, store = forge_ops.GitLabForge.from_settings(self.settings), self.deps.store()
        if forge is None or store is None:
            raise RuntimeError("BITS_ORCH_CVMFS_STATUS needs the ops token (BITS_FORGE_OPS_TOKEN)"
                               " and the store (BITS_ORCH_STORE)")
        cvmfs_status.step(self.conn, forge, store)

    def _dispatch(self):
        hs = build_queue.hosts(self.deps.reader(), self.settings.orch_build_hosts)
        try:
            # A certifying build's batch approval becomes its pipeline's pre-approval.
            build_queue.dispatch(self.conn, self.deps.forge(), hs, approvals.preapprove_queued)
        except build_queue.Stop as e:
            raise RuntimeError("builds wait: %s" % e)

    def _mirror(self):
        from .. import forge_ops
        forge = forge_ops.GitLabForge.from_settings(self.settings, "mirror",
                                                    project=self.settings.orch_manifests_project)
        mirror.push(self.conn, self.settings, forge)

    def _marker(self):
        if self._errors.get("marker") is None and getattr(self, "_marked", False):
            return
        from .. import credproxy
        store = self.deps.store()
        if store is None:
            raise RuntimeError("BITS_ORCH_STORE is not set: the worker that owns the set"
                               " writes its manifests there")
        mirror.write_marker(self.conn, self.settings, store, credproxy.s3_writer(self.settings)[0],
                            certifier.issuer(store), certifier.write_prefix(self.settings))
        self._marked = True

    def _backup(self):
        if self.settings.orch_backup_dir:
            path = db.backup(self.conn, self.settings.orch_backup_dir)
            if path:
                log.info("database copied to %s", path)

    def _run_actions(self):
        # Certifications run on the certifier thread: hashing a stack must
        # never delay a cancel.
        actions.run_due(self.conn, self.deps, self.authorize, exclude={certifier.KIND, certifier.WITHDRAW,
                                                                   certifier.DECERTIFY,
                                                                   certifier.DECERTIFY_PACKAGE})

    def _note(self, step, error, conn=None):
        """Record a step's failure or recovery as an event, but only when it
        changes, so a step failing every pass does not flood the log. *conn*:
        the connection of the thread the step runs on."""
        if self._errors.get(step) == error:
            return
        self._errors[step] = error
        conn = conn or self.conn
        try:
            with db.transaction(conn):
                events.append(conn, "worker.step_failed" if error else "worker.step_recovered",
                              "worker", events.WORKER, {"step": step, "error": error})
        except Exception:
            log.exception("cannot record step state for %s", step)

    def run_once(self):
        for name, step in self.steps:
            if self._stop.is_set():
                break
            try:
                step()
            except Exception as exc:
                log.exception("step %s failed", name)
                self._note(name, "%s: %s" % (type(exc).__name__, exc))
            else:
                if self._errors.get(name):
                    self._note(name, None)
        if self.heartbeat:
            with open(self.heartbeat, "w") as fh:
                fh.write(str(int(time.time())))

    def _check_outside(self):
        """Refuse to start on a database restored from a copy older than the
        outside one: the first event appended would fork the chain."""
        if self.outside is None:
            return
        # Signing waits for this run's first comparison, not one the database
        # (perhaps a restored copy) remembers.
        with db.transaction(self.conn):
            r = self.conn.execute("SELECT value FROM kv WHERE key = 'outside.head'").fetchone()
            if r:
                self.conn.execute("UPDATE kv SET value = ? WHERE key = 'outside.head'",
                                  (json.dumps(dict(json.loads(r["value"]), ms=0)),))
        try:
            gap = outside.behind(self.conn, self.outside)
        except outside.OutsideError as e:
            log.warning("cannot compare with the outside audit copy (%s); signing waits until"
                        " it is in step", e)
            return
        if gap:
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO kv (key, value) VALUES ('outside.behind', ?)"
                                  " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                  (json.dumps({"seq": gap[0], "outside": gap[1]}),))
            raise outside.OutsideError(
                "this database ends at event %d, the outside audit copy at %d: it was restored"
                " from an older copy. With the worker stopped, run `python -m"
                " console_backend.orch.outside recover --by <you>`" % gap)

    def start(self):
        self._check_outside()
        with db.transaction(self.conn):
            events.append(self.conn, "worker.started", "worker", events.WORKER,
                          {"schema": db.SCHEMA_VERSION})
        failures.seed_signatures(self.conn)
        n = actions.recover(self.conn)
        if n:
            log.warning("recovered %d interrupted action(s)", n)
        if self.path and self.settings.orch_manifests_project and not self.settings.orch_owns_set:
            # Until M8b: the set is read from bits-manifests and its certifications
            # compared. Once the worker owns the set, it mirrors it there instead.
            self.shadow = shadow.Runner(self.path, self.settings, self.deps, self.interval,
                                        self._stop, lambda c, step, err: self._note(step, err, c))
            self.shadow.start()
        if self.path and self.settings.orch_certify:
            self.certifier = certifier.Certifier(
                self.path, self.settings, lambda: kinds.Deps(self.settings), self.authorize,
                self.interval, self._stop, lambda c, step, err: self._note(step, err, c))
            self.certifier.start()

    def loop(self):
        self.start()
        while not self._stop.is_set():
            began = time.monotonic()
            self.run_once()
            self._stop.wait(max(1.0, self.interval - (time.monotonic() - began)))
        for t in (self.shadow, self.certifier):
            if t is not None:
                t.join(timeout=5)
        with db.transaction(self.conn):
            events.append(self.conn, "worker.stopped", "worker", events.WORKER, {})

    def stop(self, *_):
        self._stop.set()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = config.Settings()
    if not settings.orch_db:
        raise SystemExit("BITS_ORCH_DB is not set")
    problem = authority.unresolvable(settings)
    if problem:
        raise SystemExit(problem)
    path = settings.orch_db
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with db.worker_lock(path):
            conn = db.connect(path)
            db.migrate(conn)
            w = Worker(conn, settings, interval=settings.orch_interval,
                       heartbeat=path + ".heartbeat", path=path)
            signal.signal(signal.SIGTERM, w.stop)
            signal.signal(signal.SIGINT, w.stop)
            log.info("orchestrator worker on %s (schema %d, every %ss)",
                     path, db.SCHEMA_VERSION, settings.orch_interval)
            w.loop()
    except db.LockedError as e:
        raise SystemExit("bits-orchestrator: %s; not starting a second worker" % e)
    except outside.OutsideError as e:
        raise SystemExit("bits-orchestrator: %s" % e)


if __name__ == "__main__":
    main()
