"""The tarball checksum written while packing (build_template.sh) and read back
by the manifest and the store upload (checksum.tarball_checksum)."""

from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

import bits_helpers
from bits_helpers.checksum import checksum_file, tarball_checksum


def _pack_block():
    """build_template.sh's packing branch, as bash (the template is %-formatted)."""
    src = Path(os.path.join(os.path.dirname(bits_helpers.__file__), "build_template.sh")).read_text()
    start = src.index('elif [ -z "$CACHED_TARBALL" ]; then')
    end = src.index('\nfi\nwait "$rsync_pid"', start)
    return "if false; then :\n" + src[start:end].replace("%%", "%") + "\nfi\n"


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestPackWritesChecksum(unittest.TestCase):
    def _pack(self, comp="gzip -n"):
        work = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, "INSTALLROOT", "abc", "bin"))
        with open(os.path.join(work, "INSTALLROOT", "abc", "bin", "x"), "w") as f:
            f.write("payload\n" * 1000)
        os.makedirs(os.path.join(work, "TARS", "store", "ab", "abc"))
        os.makedirs(os.path.join(work, "TARS", "arch", "p"))
        env = dict(os.environ, WORK_DIR=work, HASH_PATH="store/ab/abc",
                   PACKAGE_WITH_REV="p-1-1.arch.tar.gz", PKGHASH="abc", PKGNAME="p",
                   EFFECTIVE_ARCHITECTURE="arch", CACHED_TARBALL="",
                   BITS_TAR_COMPRESSOR=comp)
        # As in a build: the build branch before packing set pipefail.
        r = subprocess.run(["bash", "-c", "set -e\nset -o pipefail\n" + _pack_block()], env=env,
                           capture_output=True, text=True)
        return r, os.path.join(work, "TARS", "store", "ab", "abc", "p-1-1.arch.tar.gz")

    def test_sidecar_describes_the_tarball(self):
        r, tar = self._pack()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(tar + ".sha256") as f:
            digest, size = f.read().split()
        self.assertEqual(digest, checksum_file(tar))
        self.assertEqual(int(size), os.path.getsize(tar))
        self.assertFalse(os.path.exists(tar + ".processing.sum"))
        self.assertEqual(tarball_checksum(tar), digest)

    def test_failed_compressor_fails_the_pack(self):
        # The compressor is no longer last in the pipeline; its failure must
        # still fail the build rather than leave a truncated tarball.
        r, tar = self._pack(comp="false")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(tar))


class TestTarballChecksum(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        self.tar = os.path.join(d, "p.tar.gz")
        with open(self.tar, "wb") as f:
            f.write(b"x" * 100)
        self.real = "sha256:" + hashlib.sha256(b"x" * 100).hexdigest()
        self.fake = "sha256:" + "ab" * 32   # proves the sidecar was read

    def _side(self, text, age=0):
        with open(self.tar + ".sha256", "w") as f:
            f.write(text)
        st = os.stat(self.tar)
        os.utime(self.tar + ".sha256", (st.st_atime, st.st_mtime + age))

    def test_sidecar_used_when_it_describes_the_file(self):
        self._side("%s 100\n" % self.fake)
        self.assertEqual(tarball_checksum(self.tar), self.fake)

    def test_file_hashed_otherwise(self):
        for text, age in (("%s 99\n" % self.fake, 0),      # another size
                          ("%s 100\n" % self.fake, -10),   # older than the file
                          ("garbage", 0), ("sha256:zz 100", 0)):
            with self.subTest(text=text, age=age):
                self._side(text, age)
                self.assertEqual(tarball_checksum(self.tar), self.real)
        os.remove(self.tar + ".sha256")
        self.assertEqual(tarball_checksum(self.tar), self.real)


if __name__ == "__main__":
    unittest.main()
