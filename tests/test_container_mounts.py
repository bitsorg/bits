"""The volumes of a build's container: only its work dir writable (build.container_mounts)."""

import shlex
import unittest

from bits_helpers.build import container_mounts


class ContainerMountsTest(unittest.TestCase):
    def test_only_the_work_dir_is_writable(self):
        words = shlex.split(container_mounts("/home/r/sw", "/container/bits/sw", "-v /src:/container/bits/sw/SOURCES:ro ",
                                             "/cfg dir", "/sw/SPECS/x", "/home/r/bits"))
        vols = [words[i + 1] for i, w in enumerate(words) if w == "-v"]
        self.assertEqual(vols, ["/home/r/sw:/container/bits/sw", "/src:/container/bits/sw/SOURCES:ro",
                                "/cfg dir:/pkgdist.bits:ro", "/sw/SPECS/x/build.sh:/build.sh:ro",
                                "/home/r/bits:/bits:ro"])     # bits, which the host runs next: read-only
        self.assertEqual(len(words), 2 * len(vols))


if __name__ == "__main__":
    unittest.main()
