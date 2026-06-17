# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Staggered OS-priority ('nice') ladder for concurrent --builders jobs.

Idea (Predrag, 2026-06): when several packages build at once, give each a
different OS scheduling priority instead of letting all of them fight for the
CPU as equals.  A pool of `slots` nice levels — 0, step, 2*step, ... (clamped to
maxnice) — is handed out lowest-first; each starting build claims the lowest
free level and releases it on completion.  So at any moment one running build
sits at nice 0 (full speed) and the rest are progressively niced down, and when
the lead build finishes the freed nice-0 slot is taken over by the next one.

This makes CPU oversubscription degrade *gracefully* — the lead build keeps
making near-full-speed progress and the machine stays responsive — but it does
NOT bound memory: the per-job memory cap (mem_per_job / effective_jobs) and the
ResourceManager admission control remain responsible for that.  Niceness is a
soft CPU-share hint, not a thread-count limit, so it complements rather than
replaces the job-count budgeting.

This is on by default for ``--builders > 1`` (disable with ``--no-build-nice``).

This module also provides :class:`ReniceWatchdog`, a background thread that
boosts the priority of a long-running 'straggler' build: a package that was
handed a low-priority (high nice) slot can end up the last job still running
and, because it was niced down, crawl.  The watchdog periodically renices the
longest-running niced-down build back toward nice 0 -- one at a time.
"""

import os
import subprocess
import threading
import time

try:
    import psutil
except Exception:  # psutil is an optional dependency
    psutil = None


def _run_docker_exec(cmd):
    """Run a ``docker/podman`` command, returning ``(returncode, stdout, stderr)``.

    Best-effort: a container that has already finished (``--rm``) is gone, so
    the command fails harmlessly and we return a non-zero code with no output.
    """
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
        )
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except Exception:
        return 1, "", ""


# Heavy CPU-bound build steps whose long-running instances are worth boosting.
# These are the GCC/Clang/Fortran back-ends (and the linker) that actually do
# the work; the front-end drivers (gcc/g++/gfortran) are short-lived.
_COMPILER_HINTS = (
    "cc1plus", "cc1", "f951", "lto1", "lto-wrapper",
    "g++", "gcc", "gfortran", "clang", "clang++", "rustc", "ld", "ld.gold",
    "ld.lld", "collect2", "as",
)


def cpu_shares_for_nice(nice_level, base=1024):
    """Map an OS nice level to a Docker/cgroup ``--cpu-shares`` weight.

    Under ``--docker`` each concurrent build runs in its own container (its own
    cgroup), so an outer ``nice`` cannot rank the builds against each other — the
    host scheduler allocates CPU between the build *containers* in proportion to
    their cgroup CPU weight.  ``docker run --cpu-shares=W`` sets that weight, so
    it is the container-level equivalent of the nice ladder.

    The mapping mirrors the CFS nice weight table (each nice step ~= 1.25x), so a
    containerised build gets the same relative priority ordering as a niced
    native build: nice 0 -> 1024 (Docker default), nice 5 -> ~335,
    nice 10 -> ~110, nice 15 -> ~36.  Clamped to Docker's minimum of 2.
    """
    shares = int(round(base * (0.8 ** max(0, int(nice_level)))))
    return max(2, shares)


class NiceLadder:
    """A small thread-safe pool of OS 'nice' levels for concurrent builds.

    Parameters
    ----------
    slots:
        Number of concurrent build slots (normally ``--builders``).  Sized so
        the pool never exhausts: at most this many build tasks run at once.
    step:
        Nice increment between successive slots.  Slot rank ``k`` maps to
        ``min(k * step, maxnice)``.  ``step=1`` gives a gentle 0,1,2,3 ladder;
        larger steps separate the slots more aggressively.
    maxnice:
        Upper clamp (Linux allows 0..19 for unprivileged processes).
    """

    def __init__(self, slots, step=5, maxnice=19):
        self.step = max(0, int(step))
        self.maxnice = int(maxnice)
        self._free = list(range(max(1, int(slots))))
        self._lock = threading.Lock()

    def _level(self, rank):
        return min(rank * self.step, self.maxnice)

    def acquire(self):
        """Claim the lowest free slot.  Returns ``(token, nice_level)``.

        ``token`` must be passed back to :meth:`release`.  If the pool is
        somehow exhausted (more concurrent builds than slots), returns
        ``(None, maxnice)`` so the extra build still runs, just fully niced.
        """
        with self._lock:
            rank = self._free.pop(0) if self._free else None
        if rank is None:
            return (None, self.maxnice)
        return (rank, self._level(rank))

    def release(self, token):
        """Return a slot claimed by :meth:`acquire` to the pool."""
        if token is None:
            return
        with self._lock:
            if token not in self._free:
                self._free.append(token)
                self._free.sort()


class ReniceWatchdog:
    """Boost the priority of long-running 'straggler' builds, one at a time.

    With the nice ladder a build handed a low-priority (high nice) slot can
    become the last job still running and, because it was niced down, crawl --
    especially a single long Fortran/C++ translation unit.  This watchdog runs
    in a background thread and every ``interval`` seconds scans the build
    processes spawned by *this* bits process, finds the longest-running one
    that is still niced down and has been running longer than ``boost_after``
    seconds, and renices its whole process subtree back toward ``target_nice``.
    Only one build is boosted per interval, so the worst stragglers are
    restored to full speed first.

    Raising a native process's priority (lowering its nice value) requires
    privilege on Linux (root / CAP_SYS_NICE, or a raised RLIMIT_NICE).  When
    that is not permitted the watchdog logs a single warning and then stays
    quiet; it never raises.

    Docker/podman builds run in a separate container with an isolated PID
    namespace, so the host cannot see or renice the compiler processes inside.
    Instead bits names each build container and registers it via
    :meth:`register_container`; the watchdog peeks inside the named container
    (``docker exec ... ps``) for a long-running compile and renices that
    process to a higher priority with ``docker exec --user 0 ... renice`` (run
    as root inside the container, so it can raise priority freely).
    """

    def __init__(self, boost_after=600, interval=30, target_nice=0,
                 docker_boost_nice=-10, log=None, docker_exec=None):
        self.boost_after = max(1, int(boost_after))
        self.interval = max(1, int(interval))
        self.target_nice = int(target_nice)
        self.docker_boost_nice = int(docker_boost_nice)
        self._log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread = None
        self._handled = set()        # native pids / (container, pid) already handled
        self._warned_eperm = False
        self._lock = threading.Lock()
        self._containers = {}        # name -> {"start": t, "bin": docker_bin}
        self._docker_exec = docker_exec or _run_docker_exec
        self._docker_disabled = False  # set if `ps` is missing in the build image
        self._warned_ps = False

    def register_container(self, name, docker_bin="docker"):
        """Register a named build container so a straggler can be boosted."""
        with self._lock:
            self._containers[name] = {"start": time.time(), "bin": docker_bin}

    def start(self):
        """Launch the watchdog thread."""
        if psutil is None:
            self._log("renice-watchdog: psutil unavailable; native straggler boosting "
                      "disabled (docker container boosting still active)")
        self._thread = threading.Thread(target=self._run, name="renice-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Signal the thread to exit and wait briefly for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _candidates(self):
        """Return ``[(proc, elapsed, nice), ...]`` for direct child build
        processes that are niced down and older than ``boost_after``."""
        out = []
        now = time.time()
        try:
            children = psutil.Process(os.getpid()).children(recursive=False)
        except Exception:
            return out
        for p in children:
            try:
                if p.pid in self._handled:
                    continue
                nice = p.nice()
                if nice is None or nice <= self.target_nice:
                    continue
                elapsed = now - p.create_time()
                if elapsed < self.boost_after:
                    continue
                out.append((p, elapsed, nice))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def _boost(self, proc):
        """Renice *proc* and its descendants toward ``target_nice``.

        Returns True if at least one process was reniced, False if not
        permitted (privilege required to raise priority).
        """
        procs = [proc]
        try:
            procs += proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        ok = False
        for p in procs:
            try:
                if p.nice() > self.target_nice:
                    p.nice(self.target_nice)
                ok = True
            except psutil.AccessDenied:
                return False
            except psutil.NoSuchProcess:
                continue
        return ok

    def _list_container_procs(self, name, docker_bin):
        """Return ``(procs, ps_missing)`` for processes inside *name*.

        *procs* is ``[(pid, etimes, comm), ...]`` (empty if the container is
        gone).  *ps_missing* is True when the failure was ``ps`` not being
        installed in the image (as opposed to the container having exited),
        detected from a 126/127 exit or a "not found" message.  Uses
        ``docker exec ... ps`` run as root so it sees every process.
        """
        rc, out, err = self._docker_exec([docker_bin, "exec", "--user", "0", name,
                                          "ps", "-eo", "pid=,etimes=,comm="])
        if rc != 0:
            blob = (err or "").lower()
            ps_missing = (rc in (126, 127)
                          or "not found" in blob
                          or "executable file not found" in blob)
            return [], ps_missing
        procs = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid, etimes = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            procs.append((pid, etimes, parts[2].strip()))
        return procs, False

    def _docker_proc_candidates(self):
        """Return ``[(name, docker_bin, pid, etimes, comm), ...]`` for heavy
        compile processes inside registered containers that have been running
        longer than ``boost_after`` and have not been handled yet.

        If ``ps`` is missing from the build image the docker path is disabled
        after a single warning (renicing inside the container is impossible
        without it)."""
        if self._docker_disabled:
            return []
        now = time.time()
        out = []
        with self._lock:
            items = list(self._containers.items())
        for name, info in items:
            # Cheap container-age gate so we do not exec into young builds.
            if now - info["start"] < self.boost_after:
                continue
            procs, ps_missing = self._list_container_procs(name, info["bin"])
            if ps_missing:
                self._docker_disabled = True
                if not self._warned_ps:
                    self._warned_ps = True
                    self._log("renice-watchdog: 'ps' is not available in the build image, so "
                              "in-container compile processes cannot be found; straggler "
                              "renicing is disabled. Install the 'procps' package in the build "
                              "image to enable it.")
                return []
            for pid, etimes, comm in procs:
                if etimes < self.boost_after:
                    continue
                if not any(h in comm for h in _COMPILER_HINTS):
                    continue
                if (name, pid) in self._handled:
                    continue
                out.append((name, info["bin"], pid, etimes, comm))
        return out

    def _renice_in_container(self, name, docker_bin, pid):
        """Renice *pid* inside *name* to ``docker_boost_nice`` (run as root so
        it can raise priority).  Returns True on success."""
        rc, _, _ = self._docker_exec([docker_bin, "exec", "--user", "0", name,
                                      "renice", "-n", str(self.docker_boost_nice),
                                      "-p", str(pid)])
        return rc == 0

    def _run(self):
        while not self._stop.wait(self.interval):
            # Native (process) stragglers: renice the subtree toward nice 0.
            cands = self._candidates()
            if cands:
                cands.sort(key=lambda t: t[1], reverse=True)  # longest-running first
                proc, elapsed, nice = cands[0]
                try:
                    pname = proc.name()
                except Exception:
                    pname = "pid %d" % proc.pid
                self._handled.add(proc.pid)  # don't retry this pid either way
                if self._boost(proc):
                    self._log("renice-watchdog: boosted long-running build (pid %d, %s) from "
                              "nice %d to %d after %d min", proc.pid, pname, nice,
                              self.target_nice, int(elapsed // 60))
                elif not self._warned_eperm:
                    self._warned_eperm = True
                    self._log("renice-watchdog: not permitted to raise build priority "
                              "(needs root / CAP_SYS_NICE); straggler boosting disabled")
                continue

            # Docker stragglers: renice the long-running compile *inside* the
            # named container (one per interval), via `docker exec ... renice`.
            dproc = self._docker_proc_candidates()
            if dproc:
                dproc.sort(key=lambda t: t[3], reverse=True)  # longest etimes first
                name, dbin, pid, etimes, comm = dproc[0]
                self._handled.add((name, pid))  # boost once
                if self._renice_in_container(name, dbin, pid):
                    self._log("renice-watchdog: boosted in-container compile %s (pid %d in %r) "
                              "to nice %d after %d min", comm, pid, name,
                              self.docker_boost_nice, int(etimes // 60))
