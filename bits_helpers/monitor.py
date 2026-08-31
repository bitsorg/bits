# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in build-host monitor.

A best-effort background thread that samples the *runner host* it runs on — load,
memory, the build filesystem, and the size of the local sw/ build-products
directory — plus the currently-building packages, and pushes them to a
VictoriaMetrics / Prometheus endpoint (``…/api/v1/import/prometheus``).

Design points:

* **Opt-in.** Off unless ``--monitor`` (or ``monitor: true`` in the system
  config) is set. bits-console always passes ``--monitor --monitor-url
  $METRICS_URL``; a plain CLI user opts in explicitly.
* **Never blocks or fails a build.** Every sample and every push is wrapped;
  errors are swallowed. The thread is a daemon and is joined with a short
  timeout at the end of the run.
* **Per-runner identity.** All metrics carry an ``instance`` label of
  ``<fqdn>-<runner-id>`` (runner id from ``BITS_RUNNER_ID`` / GitLab
  ``CI_RUNNER_ID``). Appending the runner id keeps these series distinct from a
  real node-exporter on the same host — so they never double-count — and
  distinguishes several runners sharing one machine.
* **node_exporter-compatible names.** load/memory/filesystem use the
  ``node_*`` names the dashboard already queries, so no query changes are needed
  for the host to appear as its own ring. The sw/ size is ``bits_sw_dir_bytes``
  and CPU core count is published as ``node_cpu_count`` (the dashboard falls back
  to it where per-cpu ``node_cpu_seconds_total`` is absent).

Runtime only — nothing here feeds a package hash.
"""
import os
import socket
import subprocess
import threading
import time

_MONITOR = None  # process-wide singleton (a build run has one host monitor)


def default_instance():
    """`<fqdn>-<runner-id>` (or just the host when no runner id is known)."""
    host = ""
    try:
        host = socket.getfqdn() or socket.gethostname()
    except Exception:
        host = "unknown"
    host = (host or "unknown").strip()
    rid = ""
    for var in ("BITS_RUNNER_ID", "CI_RUNNER_ID", "CI_RUNNER_SHORT_TOKEN"):
        v = os.environ.get(var, "").strip()
        if v:
            rid = v
            break
    return "%s-%s" % (host, rid) if rid else host


def _q(v):
    """Escape a Prometheus label value."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class BuildMonitor:
    def __init__(self, url, instance=None, interval=15.0, disk_interval=60.0,
                 sw_dir=None):
        self.url = (url or "").rstrip("/")
        self.instance = instance or default_instance()
        self.interval = max(2.0, float(interval or 15.0))
        # du can be expensive, so the disk/sw sample runs at its own, slower
        # cadence (never more often than the load/memory sample).
        self.disk_interval = max(self.interval, float(disk_interval or 60.0))
        self.sw_dir = sw_dir
        self._active = {}                 # "pkg|arch" -> (package, arch)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._slow_thread = None
        self._sw_bytes = None             # latest du(sw), filled by the slow loop
        self._diag_logged = False         # log the FIRST push outcome once

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        if not self.url:
            return self
        # Two threads: a FAST one that pushes cheap host metrics every interval,
        # and a SLOW one that runs du(sw) + `docker stats` on its own cadence.
        # du over a large sw/ tree can take minutes, so it MUST NOT sit on the
        # host-cadence thread (it would block every host sample until it returns).
        self._thread = threading.Thread(target=self._loop, name="bits-monitor",
                                        daemon=True)
        self._thread.start()
        self._slow_thread = threading.Thread(target=self._slow_loop,
                                             name="bits-monitor-slow", daemon=True)
        self._slow_thread.start()
        return self

    def stop(self, timeout=3.0):
        self._stop.set()
        for t in (self._thread, self._slow_thread):
            if t is not None:
                t.join(timeout)

    def set_active(self, package, arch, on=True):
        key = "%s|%s" % (package, arch)
        with self._lock:
            if on:
                self._active[key] = (package, arch)
            else:
                self._active.pop(key, None)

    # ── loops ────────────────────────────────────────────────────────────────
    def _loop(self):
        """FAST cadence: cheap host + filesystem(statvfs) + active + cached sw."""
        while not self._stop.wait(self.interval):
            try:
                self._push(self._fast_lines())
            except Exception:
                pass  # best-effort: a build must never be affected by monitoring
        try:                                   # one final flush on shutdown
            self._push(self._fast_lines())
        except Exception:
            pass

    def _slow_loop(self):
        """SLOW cadence: du(sw) (can take minutes) + `docker stats`. Runs on its
        own thread and pushes its own lines, so it never stalls the host push."""
        delay = 1.0                            # take a first sample soon
        while not self._stop.wait(delay):
            delay = self.disk_interval
            if self.sw_dir and os.path.isdir(self.sw_dir):
                b = self._du(self.sw_dir)
                if b is not None:
                    self._sw_bytes = b         # picked up by the fast push
            try:
                cl = self._container_lines()
                if cl:
                    self._push(cl)
            except Exception:
                pass

    def _fast_lines(self):
        """Everything cheap enough to sample on every host tick (no du/docker)."""
        out = self._host_lines() + self._active_lines() + self._fs_lines()
        if self._sw_bytes is not None:
            out.append("bits_sw_dir_bytes{%s} %d" % (self._lbl(), self._sw_bytes))
        return out

    # ── samplers (each returns a list of Prometheus text lines) ──────────────
    def _lbl(self, extra=""):
        base = 'instance="%s",job="bits"' % _q(self.instance)
        return base + ("," + extra if extra else "")

    def _host_lines(self):
        out = []
        try:
            with open("/proc/loadavg") as f:
                l1, l5, l15 = f.read().split()[:3]
            b = self._lbl()
            out += ["node_load1{%s} %s" % (b, l1),
                    "node_load5{%s} %s" % (b, l5),
                    "node_load15{%s} %s" % (b, l15)]
        except Exception:
            pass
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    parts = v.split()
                    if parts:
                        mem[k] = int(parts[0]) * 1024  # kB -> bytes
            b = self._lbl()
            for key, metric in (("MemTotal", "node_memory_MemTotal_bytes"),
                                ("MemAvailable", "node_memory_MemAvailable_bytes"),
                                ("SwapTotal", "node_memory_SwapTotal_bytes"),
                                ("SwapFree", "node_memory_SwapFree_bytes")):
                if key in mem:
                    out.append("%s{%s} %d" % (metric, b, mem[key]))
        except Exception:
            pass
        try:
            out.append("node_cpu_count{%s} %d" % (self._lbl(), os.cpu_count() or 1))
        except Exception:
            pass
        return out

    def _fs_lines(self):
        """The sw filesystem's size/avail via statvfs (fast — no du here)."""
        out = []
        sw = self.sw_dir
        if not sw or not os.path.isdir(sw):
            return out
        try:
            st = os.statvfs(sw)
            size = st.f_blocks * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            mp = self._mountpoint(sw)
            lbl = self._lbl('mountpoint="%s"' % _q(mp))
            out.append("node_filesystem_size_bytes{%s} %d" % (lbl, size))
            out.append("node_filesystem_avail_bytes{%s} %d" % (lbl, avail))
        except Exception:
            pass
        return out

    def _active_lines(self):
        """One gauge per package currently building (labels = package, arch)."""
        with self._lock:
            active = list(self._active.values())
        out = ["bits_build_active_count{%s} %d" % (self._lbl(), len(active))]
        for package, arch in active:
            lbl = self._lbl('package="%s",arch="%s"' % (_q(package), _q(arch)))
            out.append("bits_build_active{%s} 1" % lbl)
        return out

    def _container_lines(self):
        """Best-effort per-container CPU/RSS via `docker stats` for the build
        containers bits names ``bits-build-*``. Empty when docker is unavailable
        or no such container is running (e.g. builds not currently throttled)."""
        out = []
        docker = self._docker_bin()
        if not docker:
            return out
        try:
            r = subprocess.run(
                [docker, "stats", "--no-stream", "--no-trunc",
                 "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
                capture_output=True, text=True, timeout=8)
            if r.returncode != 0:
                return out
        except Exception:
            return out
        for row in r.stdout.splitlines():
            name, _, rest = row.partition("\t")
            if not name.startswith("bits-build-"):
                continue
            cpu_s, _, mem_s = rest.partition("\t")
            lbl = self._lbl('container="%s"' % _q(name))
            cpu = self._pct(cpu_s)
            if cpu is not None:
                out.append("bits_build_container_cpu_percent{%s} %s" % (lbl, cpu))
            rss = self._bytes(mem_s.split("/")[0])
            if rss is not None:
                out.append("bits_build_container_rss_bytes{%s} %d" % (lbl, rss))
        return out

    # ── push ─────────────────────────────────────────────────────────────────
    def _push(self, lines):
        if not lines or not self.url:
            return
        from bits_helpers.metrics import push_prometheus
        try:
            code = push_prometheus(self.url, "\n".join(lines) + "\n", timeout=3)
            if not self._diag_logged:      # confirm the push path once, loudly
                print("[monitor] first push OK (HTTP %s) -> %s as instance=%s"
                      % (code if code is not None else "?", self.url, self.instance), flush=True)
                self._diag_logged = True
        except Exception as e:
            if not self._diag_logged:      # make a silent NAT/firewall drop visible
                print("[monitor] first push FAILED -> %s: %s" % (self.url, e), flush=True)
                self._diag_logged = True
            # endpoint down / behind NAT — drop subsequent samples silently

    # ── small helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _du(path):
        try:
            r = subprocess.run(["du", "-sb", path], capture_output=True,
                               text=True, timeout=180)
            if r.returncode == 0:
                return int(r.stdout.split()[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _mountpoint(path):
        p = os.path.abspath(path)
        while not os.path.ismount(p) and p != "/":
            p = os.path.dirname(p)
        return p

    @staticmethod
    def _docker_bin():
        from shutil import which
        return which("docker") or which("podman")

    @staticmethod
    def _pct(s):
        try:
            return "%.1f" % float(str(s).strip().rstrip("%"))
        except Exception:
            return None

    @staticmethod
    def _bytes(s):
        s = str(s).strip()
        units = {"B": 1, "KB": 1e3, "KIB": 1024, "MB": 1e6, "MIB": 1024**2,
                 "GB": 1e9, "GIB": 1024**3, "TB": 1e12, "TIB": 1024**4}
        for u in sorted(units, key=len, reverse=True):
            if s.upper().endswith(u):
                try:
                    return int(float(s[:-len(u)].strip()) * units[u])
                except Exception:
                    return None
        try:
            return int(float(s))
        except Exception:
            return None


# ── module-level convenience API (build.py uses these) ───────────────────────
def start_monitor(url, instance=None, interval=15.0, disk_interval=60.0,
                  sw_dir=None):
    """Create and start the process-wide monitor. No-op (returns None) without a
    URL, so callers can pass through an unset endpoint harmlessly."""
    global _MONITOR
    if not url:
        return None
    _MONITOR = BuildMonitor(url, instance=instance, interval=interval,
                            disk_interval=disk_interval, sw_dir=sw_dir)
    return _MONITOR.start()


def note_build(package, arch, on=True):
    if _MONITOR is not None:
        _MONITOR.set_active(package, arch, on)


def stop_monitor(timeout=3.0):
    global _MONITOR
    if _MONITOR is not None:
        _MONITOR.stop(timeout)
        _MONITOR = None
