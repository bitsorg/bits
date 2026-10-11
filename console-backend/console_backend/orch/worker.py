# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator worker: one process, one loop.

Each pass runs the steps in order. A step reads the database and the outside
world, then writes what it decided; a step that fails is logged, recorded as an
event when its error changes, and tried again on the next pass. M0 has one step,
running due actions; reconcile, builds, signing and schedules are added as steps
by later milestones.

    python -m console_backend.orch.worker      # BITS_ORCH_DB=/orch/orch.db
"""

import logging
import os
import signal
import threading
import time

from .. import config
from . import actions, authority, db, events, kinds

log = logging.getLogger("bits_orchestrator")


class Worker:
    def __init__(self, conn, settings, interval=30.0, heartbeat=None):
        self.conn = conn
        self.settings = settings
        self.interval = interval
        self.heartbeat = heartbeat
        self.deps = kinds.Deps(settings)
        self.authorize = authority.checker(settings)
        self.steps = [("actions", self._run_actions)]
        self._errors = {}                 # step -> last error text
        self._stop = threading.Event()

    def _run_actions(self):
        actions.run_due(self.conn, self.deps, self.authorize)

    def _note(self, step, error):
        """Record a step's failure or recovery as an event, but only when it
        changes, so a step failing every pass does not flood the log."""
        if self._errors.get(step) == error:
            return
        self._errors[step] = error
        try:
            with db.transaction(self.conn):
                events.append(self.conn, "worker.step_failed" if error else "worker.step_recovered",
                              "worker", events.WORKER, {"step": step, "error": error})
        except Exception:
            log.exception("cannot record step state for %s", step)

    def run_once(self):
        for name, step in self.steps:
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

    def start(self):
        with db.transaction(self.conn):
            events.append(self.conn, "worker.started", "worker", events.WORKER,
                          {"schema": db.SCHEMA_VERSION})
        n = actions.recover(self.conn)
        if n:
            log.warning("recovered %d interrupted action(s)", n)

    def loop(self):
        self.start()
        while not self._stop.is_set():
            began = time.monotonic()
            self.run_once()
            self._stop.wait(max(1.0, self.interval - (time.monotonic() - began)))
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
    with db.worker_lock(path):
        conn = db.connect(path)
        db.migrate(conn)
        w = Worker(conn, settings, interval=settings.orch_interval,
                   heartbeat=path + ".heartbeat")
        signal.signal(signal.SIGTERM, w.stop)
        signal.signal(signal.SIGINT, w.stop)
        log.info("orchestrator worker on %s (schema %d, every %ss)",
                 path, db.SCHEMA_VERSION, settings.orch_interval)
        w.loop()


if __name__ == "__main__":
    main()
