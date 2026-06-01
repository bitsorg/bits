"""Tests for bits_helpers/nice_ladder.py (optional --build-nice scheduling)."""

import os
import re
import shlex
import subprocess
import time
import unittest

from bits_helpers.nice_ladder import NiceLadder, cpu_shares_for_nice, ReniceWatchdog

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class TestNiceLadder(unittest.TestCase):

    def test_levels_lowest_first_with_step(self):
        lad = NiceLadder(4, step=5)
        levels = []
        tokens = []
        for _ in range(4):
            t, lvl = lad.acquire()
            tokens.append(t)
            levels.append(lvl)
        # slots 0..3 -> 0,5,10,15
        self.assertEqual(levels, [0, 5, 10, 15])

    def test_gentle_step_one(self):
        lad = NiceLadder(4, step=1)
        levels = [lad.acquire()[1] for _ in range(4)]
        self.assertEqual(levels, [0, 1, 2, 3])

    def test_clamped_to_maxnice(self):
        lad = NiceLadder(6, step=5, maxnice=19)
        levels = [lad.acquire()[1] for _ in range(6)]
        # 0,5,10,15,19(20 clamped),19(25 clamped)
        self.assertEqual(levels, [0, 5, 10, 15, 19, 19])

    def test_release_frees_lowest_slot_for_reuse(self):
        lad = NiceLadder(3, step=5)
        t0, l0 = lad.acquire()   # slot 0 -> 0
        t1, l1 = lad.acquire()   # slot 1 -> 5
        self.assertEqual((l0, l1), (0, 5))
        lad.release(t0)          # free the lead slot
        t2, l2 = lad.acquire()   # next build takes over the freed lead slot
        self.assertEqual(l2, 0)

    def test_exhaustion_returns_maxnice_not_crash(self):
        lad = NiceLadder(1, step=5)
        _t0, l0 = lad.acquire()      # only slot
        tok, lvl = lad.acquire()     # pool empty
        self.assertEqual((tok, lvl), (None, 19))
        lad.release(tok)             # releasing the None token is a no-op
        lad.release(tok)

    def test_double_release_is_idempotent(self):
        lad = NiceLadder(2, step=5)
        t, _ = lad.acquire()
        lad.release(t)
        lad.release(t)               # must not duplicate the slot
        levels = [lad.acquire()[1] for _ in range(2)]
        self.assertEqual(sorted(levels), [0, 5])

    def test_command_wrapping_is_valid_shell(self):
        # mirror the native wrapping done in runBuildCommand
        cmd = "env A=b bash -e -x /work/build.sh 2>&1"
        wrapped = "nice -n %d /bin/sh -c %s" % (10, shlex.quote(cmd))
        # the quoted payload round-trips back to the original command
        toks = shlex.split(wrapped)
        self.assertEqual(toks[:3], ["nice", "-n", "10"])
        self.assertEqual(toks[3:5], ["/bin/sh", "-c"])
        self.assertEqual(toks[5], cmd)


class TestCpuShares(unittest.TestCase):

    def test_nice_zero_is_docker_default(self):
        self.assertEqual(cpu_shares_for_nice(0), 1024)

    def test_monotonically_decreasing(self):
        seq = [cpu_shares_for_nice(n) for n in range(0, 20)]
        self.assertTrue(all(a >= b for a, b in zip(seq, seq[1:])))

    def test_mirrors_cfs_weight_table(self):
        # ~1.25x per nice step (0.8 factor): rough checkpoints
        self.assertEqual(cpu_shares_for_nice(5), 336)
        self.assertEqual(cpu_shares_for_nice(10), 110)
        self.assertEqual(cpu_shares_for_nice(15), 36)

    def test_floored_at_docker_minimum(self):
        self.assertGreaterEqual(cpu_shares_for_nice(100), 2)

    def test_injection_into_docker_run(self):
        # mirror the re.subn injection in runBuildCommand
        cmd = ("docker run --rm --entrypoint= --user 1:1 -v /w:/w "
               "img bash -ex /build.sh")
        shares = cpu_shares_for_nice(5)
        out, n = re.subn(r'\b(docker|podman)\s+run\s',
                         r'\1 run --cpu-shares=%d ' % shares, cmd, count=1)
        self.assertEqual(n, 1)
        self.assertIn("docker run --cpu-shares=336 --rm", out)

    def test_injection_matches_podman_too(self):
        cmd = "podman run --rm img bash -ex /build.sh"
        out, n = re.subn(r'\b(docker|podman)\s+run\s',
                         r'\1 run --cpu-shares=%d ' % 110, cmd, count=1)
        self.assertEqual(n, 1)
        self.assertTrue(out.startswith("podman run --cpu-shares=110 --rm"))


@unittest.skipIf(psutil is None, "psutil not available")
class TestReniceWatchdog(unittest.TestCase):

    def _spawn(self, nice_value):
        p = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: os.nice(nice_value))
        self.addCleanup(p.terminate)
        return p

    def test_detects_niced_straggler_and_ignores_lead(self):
        niced = self._spawn(10)
        lead = self._spawn(0)
        w = ReniceWatchdog(boost_after=1, interval=100, target_nice=0)
        time.sleep(1.3)  # exceed boost_after
        pids = {p.pid for (p, _, _) in w._candidates()}
        self.assertIn(niced.pid, pids)       # backed-off build is a candidate
        self.assertNotIn(lead.pid, pids)     # nice-0 lead is never boosted

    def test_below_threshold_is_not_a_candidate(self):
        niced = self._spawn(10)
        w = ReniceWatchdog(boost_after=3600, interval=100, target_nice=0)
        time.sleep(0.3)
        pids = {p.pid for (p, _, _) in w._candidates()}
        self.assertNotIn(niced.pid, pids)    # too young to boost yet

    def test_boost_is_graceful(self):
        # Boosting must never raise: returns False where raising priority is
        # not permitted (unprivileged), True where it is (root/CAP_SYS_NICE).
        niced = self._spawn(10)
        w = ReniceWatchdog(boost_after=1, target_nice=0)
        time.sleep(1.3)
        result = w._boost(psutil.Process(niced.pid))
        self.assertIn(result, (True, False))
        if result:
            self.assertLessEqual(psutil.Process(niced.pid).nice(), 0)


class TestReniceWatchdogDocker(unittest.TestCase):
    """In-container straggler boosting via `docker exec renice` (no real docker
    needed: the docker_exec callable is injected)."""

    # A fake `ps -eo pid=,etimes=,comm=` listing inside a build container:
    # a long g++ back-end (cc1plus), a fresh one, and a non-compiler shell.
    PS_OUTPUT = "  17 900 cc1plus\n   42  30 cc1plus\n   1 905 sh\n  18 902 make\n"

    def _watchdog(self):
        self.calls = []

        def fake_exec(cmd):
            self.calls.append(cmd)
            if "ps" in cmd:
                return 0, self.PS_OUTPUT
            return 0, ""   # renice (or anything else) succeeds

        return ReniceWatchdog(boost_after=600, interval=100,
                              docker_boost_nice=-10, docker_exec=fake_exec)

    def test_proc_candidate_is_long_compiler_only(self):
        w = self._watchdog()
        w.register_container("bits-build-root-abcd", "docker")
        # Container younger than boost_after -> no exec, no candidates.
        self.assertEqual(w._docker_proc_candidates(), [])
        # Age the container past the threshold.
        for v in w._containers.values():
            v["start"] -= 1000
        cands = w._docker_proc_candidates()
        pids = {pid for (_, _, pid, _, _) in cands}
        # Only the long-running compiler (pid 17, etimes 900) qualifies:
        # pid 42 too young, sh/make are not compilers.
        self.assertEqual(pids, {17})

    def test_run_issues_docker_exec_renice_to_boost_nice(self):
        w = self._watchdog()
        w.register_container("bits-build-acts-1234", "podman")
        ok = w._renice_in_container("bits-build-acts-1234", "podman", 17)
        self.assertTrue(ok)
        self.assertIn(
            ["podman", "exec", "--user", "0", "bits-build-acts-1234",
             "renice", "-n", "-10", "-p", "17"],
            self.calls)

    def test_handled_proc_not_recandidated(self):
        w = self._watchdog()
        w.register_container("c1", "docker")
        for v in w._containers.values():
            v["start"] -= 1000
        w._handled.add(("c1", 17))
        pids = {pid for (_, _, pid, _, _) in w._docker_proc_candidates()}
        self.assertNotIn(17, pids)


if __name__ == "__main__":
    unittest.main()
