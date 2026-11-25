# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""With --confine-work-dir the host makes a build's "latest" links: BUILD/<pkg>-latest
before it runs, <pkg>/latest[-<family>] and its tarball's version link once it succeeded.
Without it the build script makes them, as before."""

import os
import shutil
import tempfile
import threading
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch

from bits_helpers.build import runBuildCommand

ARCH = "slc7_x86-64"
HASH = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
SPEC = {"package": "zlib", "version": "v1.3.1", "revision": "1", "hash": HASH,
        "architecture": "", "is_devel_pkg": False, "requires": [], "build_family": "rel"}
TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "bits_helpers", "build_template.sh")


class LatestLinksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def run_build(self, rc, builders=1, devel=False, confined=True):
        args = Namespace(builders=builders, workDir=self.dir, debug=False, resourceMonitoring=False,
                         architecture=ARCH, autoCleanup=False, aggressiveCleanup=False,
                         develPrefix="dev")
        sched = Namespace(buildFailures=[], errors={}, buildFailuresLock=threading.Lock(),
                          log=MagicMock())
        seen = {}

        def execute(cmd, printer, **kw):    # what the build sees while it runs
            seen["build"] = os.path.realpath(os.path.join(self.dir, "BUILD", "zlib-latest"))
            seen["latest"] = os.path.lexists(os.path.join(self.dir, ARCH, "zlib", "latest"))
            os.makedirs(os.path.join(self.dir, ARCH, "zlib", "v1.3.1-1"), exist_ok=True)
            return rc
        with patch.dict(os.environ, {"BITS_BUILD_WORK_DIR": self.dir, "BITS_DIST_HASH": "0123456789"}), \
             patch("bits_helpers.build.execute", side_effect=execute), \
             patch("bits_helpers.build.detectArch", return_value=ARCH), \
             patch("bits_helpers.build.doFinalSync"), \
             patch("bits_helpers.build.make_own_paths") as made, \
             patch("bits_helpers.monitor.note_build"):
            spec = dict(SPEC, is_devel_pkg=devel, _own_paths=["own"] if confined else None)
            runBuildCommand(sched, "zlib", {"zlib": spec}, args, "true", None, self.dir, self.dir, None)
        self.assertEqual(made.call_count, int(confined))
        return seen

    def link(self, *path):
        p = os.path.join(self.dir, *path)
        return os.readlink(p) if os.path.islink(p) else None

    def test_links_made_by_the_host(self):
        seen = self.run_build(0)
        self.assertEqual(seen, {"build": os.path.join(os.path.realpath(self.dir), "BUILD", HASH),
                                "latest": False})         # BUILD's before, latest after
        self.assertEqual(self.link("BUILD", "zlib-latest"), HASH)
        self.assertEqual(self.link(ARCH, "zlib", "latest"), "v1.3.1-1")
        self.assertEqual(self.link(ARCH, "zlib", "latest-rel"), "v1.3.1-1")
        self.assertEqual(self.link("TARS", ARCH, "zlib", "zlib-v1.3.1-1.%s.tar.gz" % ARCH),
                         "../../%s/store/de/%s/zlib-v1.3.1-1.%s.tar.gz" % (ARCH, HASH, ARCH))

    def test_a_devel_build_has_its_prefix_link(self):
        self.run_build(0, devel=True)
        self.assertEqual(self.link("BUILD", "zlib-latest-dev"), HASH)

    def test_a_failed_build_is_not_latest(self):
        os.makedirs(os.path.join(self.dir, ARCH, "zlib"))
        os.symlink("v1.3.0-1", os.path.join(self.dir, ARCH, "zlib", "latest"))
        self.run_build(7, builders=2)
        self.assertEqual(self.link("BUILD", "zlib-latest"), HASH)       # its log
        self.assertEqual(self.link(ARCH, "zlib", "latest"), "v1.3.0-1")
        self.assertIsNone(self.link(ARCH, "zlib", "latest-rel"))

    def test_without_it_the_host_makes_none(self):
        self.run_build(0, confined=False)
        self.assertEqual(os.listdir(self.dir), [ARCH])           # the recipe's install only
        self.assertEqual(os.listdir(os.path.join(self.dir, ARCH, "zlib")), ["v1.3.1-1"])

    def test_the_build_script_makes_them_unless_confined(self):
        with open(TEMPLATE) as fh:
            text = fh.read()
        guard = 'if [ "${BITS_CONFINE_WORK_DIR:-}" != 1 ]; then   # confined: made by bits on the host\n'
        self.assertEqual(text.count(guard), 2)
        for made in ('  /bin/ln -snf "$PKGHASH" "$BITS_BUILD_WORK_DIR/BUILD/$PKGNAME-latest"\n',
                     '  /bin/ln -snf "${_VERREV}" "$(dirname "$PKGPATH")/latest"\n',
                     '[ "${BITS_CONFINE_WORK_DIR:-}" = 1 ] || ln -nfs "../../$HASH_PATH/$PACKAGE_WITH_REV"'):
            self.assertIn(made, text)


if __name__ == "__main__":
    unittest.main()
