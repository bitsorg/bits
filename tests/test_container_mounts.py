"""The volumes of a build's container: only its work dir writable (build.container_mounts)."""

import os
import shlex
import tempfile
import unittest

from bits_helpers.build import container_mounts, host_dirs_read_only


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

if __name__ == "__main__":
    unittest.main()
