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

This is opt-in (``--build-nice``) so it can be A/B tested against the default
scheduling in a realistic build.
"""

import threading


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
