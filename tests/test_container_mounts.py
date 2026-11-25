"""The volumes of a build's container: only its work dir writable (build.container_mounts)."""

import os
import shlex
import subprocess
import tempfile
import unittest

from bits_helpers.build import container_mounts, host_dirs_read_only, make_own_paths, own_paths

ENV = {"PKGHASH": "ab12cd", "PKGNAME": "zlib", "PKGVERSION": "v1.3", "PKGREVISION": "2",
       "EFFECTIVE_ARCHITECTURE": "el9", "BITS_FETCH_CACHE": "FETCH_CACHE/el9/zlib/v1.3"}
TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "bits_helpers", "build_template.sh")


class ContainerMountsTest(unittest.TestCase):
    def test_only_the_work_dir_is_writable(self):
        words = shlex.split(container_mounts("/home/r/sw", "/container/bits/sw", "-v /src:/container/bits/sw/SOURCES:ro ",
                                             "/cfg dir", "/sw/SPECS/x", "/home/r/bits"))
        vols = [words[i + 1] for i, w in enumerate(words) if w == "-v"]
        self.assertEqual(vols, ["/home/r/sw:/container/bits/sw", "/src:/container/bits/sw/SOURCES:ro",
                                "/cfg dir:/pkgdist.bits:ro", "/sw/SPECS/x/build.sh:/build.sh:ro",
                                "/home/r/bits:/bits:ro"])     # bits, which the host runs next: read-only
        self.assertEqual(len(words), 2 * len(vols))


    def test_what_the_host_uses_is_read_only(self):
        with tempfile.TemporaryDirectory() as t:
            w = os.path.join(t, "sw w")
            os.makedirs(os.path.join(w, "jobs", "42"))
            words = shlex.split(host_dirs_read_only(w, "/container/bits/sw", w + "/MIRROR"))
            self.assertEqual([words[i + 1] for i in range(0, len(words), 2)],
                             ["%s/%s:/container/bits/sw/%s:ro" % (w, d, d)
                              for d in ("REPOS", "LOGS", "MANIFESTS", "MIRROR", "jobs")])
            for d in ("REPOS", "LOGS", "MANIFESTS", "MIRROR"):      # made first: a build cannot
                self.assertTrue(os.path.isdir(os.path.join(w, d)), d)
            # a mirror elsewhere is not in the work dir's mount; no CI job dir: none
            other = os.path.join(t, "other")
            got = shlex.split(host_dirs_read_only(other, "/c", t))
            self.assertEqual([got[i + 1] for i in range(0, len(got), 2)],
                             ["%s/%s:/c/%s:ro" % (other, d, d) for d in ("REPOS", "LOGS", "MANIFESTS")])
            os.rename(os.path.join(w, "LOGS"), os.path.join(t, "elsewhere"))
            os.symlink(os.path.join(t, "elsewhere"), os.path.join(w, "LOGS"))   # as a build could leave it
            with self.assertRaises(SystemExit):
                host_dirs_read_only(w, "/c", None)

class ConfinedWorkDirTest(unittest.TestCase):
    """--confine-work-dir: the work dir read-only, the package's own paths writable."""

    def test_only_the_packages_own_paths_are_writable(self):
        with tempfile.TemporaryDirectory() as t:
            w = os.path.join(t, "sw")
            own = own_paths(ENV, w)
            self.assertFalse(os.path.exists(w))                        # nothing made yet
            make_own_paths(own, w)
            self.assertEqual([os.path.relpath(p, w) for p in own],
                             ["BUILD/ab12cd", "INSTALLROOT/ab12cd", "TMP/ab12cd", "TARS/el9/store/ab/ab12cd",
                              "el9/zlib/v1.3-2", "FETCH_CACHE/el9/zlib/v1.3"])
            self.assertTrue(all(os.path.isdir(p) for p in own))        # made before the container
            words = shlex.split(container_mounts(w, "/c", "", "/cfg", "/s", "/bits", own))
            vols = [words[i + 1] for i, x in enumerate(words) if x == "-v"]
            self.assertEqual(vols[0], w + ":/c:ro")
            self.assertEqual(vols[4:], ["%s/%s:/c/%s" % (w, r, r) for r in (
                "BUILD/ab12cd", "INSTALLROOT/ab12cd", "TMP/ab12cd", "TARS/el9/store/ab/ab12cd",
                "el9/zlib/v1.3-2", "FETCH_CACHE/el9/zlib/v1.3")])
            # without it, as before: the work dir writable, nothing else added
            self.assertEqual(shlex.split(container_mounts(w, "/c", "", "/cfg", "/s", "/bits"))[1], w + ":/c")

    def test_a_path_out_of_its_place_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            w = os.path.join(t, "sw")
            for env in (dict(ENV, PKGFAMILY=".."), dict(ENV, PKGVERSION="..", PKGREVISION=""),
                        dict(ENV, PKGNAME="../../PACKED")):
                with self.subTest(env=env), self.assertRaises(SystemExit):
                    make_own_paths(own_paths(env, w), w)
            self.assertEqual(sorted(os.listdir(t)), [])      # nothing made

    def test_a_path_through_a_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            w = os.path.join(t, "sw")
            os.makedirs(os.path.join(w, "el9"))
            os.makedirs(os.path.join(t, "other", "zlib"))
            os.symlink(os.path.join(t, "other", "zlib"), os.path.join(w, "el9", "zlib"))  # as a build could
            with self.assertRaises(SystemExit):
                make_own_paths(own_paths(ENV, w), w)
            self.assertFalse(os.path.exists(os.path.join(t, "other", "zlib", "v1.3-2")))

    def test_the_build_script_keeps_its_mount_points_only_when_confined(self):
        with open(TEMPLATE) as fh:
            text = fh.read()
        start = text.index('if [ "${BITS_CONFINE_WORK_DIR:-}" = 1 ]; then\n  _bits_reset()')
        func = text[start:text.index("\nfi\n", start) + 4]
        for confined, kept in (("1", True), ("", False)):
            with tempfile.TemporaryDirectory() as t:
                os.makedirs(os.path.join(t, "d", "sub"))
                open(os.path.join(t, "d", ".hidden"), "w").close()
                subprocess.run(["bash", "-ec", func + '_bits_reset "$1/d"; _bits_reset "$1/none"', "x", t],
                               check=True, env=dict(os.environ, BITS_CONFINE_WORK_DIR=confined))
                self.assertEqual(os.path.isdir(os.path.join(t, "d")), kept, confined)
                if kept:
                    self.assertEqual(os.listdir(os.path.join(t, "d")), [])     # emptied
        # without the flag, as before: the whole INSTALLROOT/<hash> copied
        self.assertIn('  rsync -aH "$WORK_DIR/INSTALLROOT/$PKGHASH/" "$WORK_DIR" & rsync_pid=$!\n', text)

if __name__ == "__main__":
    unittest.main()
