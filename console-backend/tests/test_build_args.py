# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bits build options a console build may carry (build_args.py; the same file is
bits-console's .gitlab/check-build-args.py)."""

import subprocess
import sys
import unittest

from console_backend import build_args

IMAGE = "registry.cern.ch/alisw/slc9-builder"
SENT = ("--force-rebuild ROOT --architecture slc9_x86-64 --docker --docker-image %s -j 8"
        " --docker-extra-args=--cpus=8 --sandbox off --debug --defaults gcc14::opt::testbed"
        " --day= --set release=LCG_107 --flavour x=y --builders 4" % IMAGE)


class ProblemTest(unittest.TestCase):
    def test_what_the_console_dispatcher_and_nightlies_send(self):
        for args in (SENT, "--builders --debug", "--set proxy=x --set pathname=y --set lcg_branch=LCG_110", "--no-system ROOT,zlib --builders=4 --day Mon",
                     "--disable a,b --run-tests --only-deps --pip-build wheel --fetch-repos", ""):
            self.assertIsNone(build_args.problem(args.split()), args)

    def test_what_leaves_the_container_or_changes_the_store(self):
        for args, says in (("--insecure", "--insecure"), ("--insec", "--insec"),       # no abbreviation
                           ("--remote-store s3://x", "--remote-store"), ("--write-store s3://x", "--write-store"),
                           ("--no-require-signed-reuse", "signed"), ("--trust-groups a", "--trust-groups"),
                           ("-w /tmp", "-w"), ("--work-dir=/tmp", "--work-dir"), ("--config-dir /", "--config-dir"),
                           ("--reference-sources /", "--reference-sources"), ("--container-use-workdir", "workdir"),
                           ("--cvmfs-prefix /cvmfs/x", "--cvmfs-prefix"), ("--plugin x", "--plugin"),
                           ("--docker-extra-args=--privileged", "--privileged"),
                           ("--docker-extra-args=-v/:/host", "-v/:/host"), ("--docker-extra-args --cpus=4", "needs"),
                           ("--sandbox-network on", "--sandbox-network"), ("--sandbox-allow-write /", "allow-write"),
                           ("-e LD_PRELOAD=x", "-e"), ("--monitor-url http://x", "--monitor-url"),
                           ("--force-rebuild --insecure", "needs a value"), ("-j4", "-j4"), ("ROOT", "not the value"),
                           ("--debug=1", "no value"), ("--sandbox none", "none"), ("--set", "needs a value"),
                           ("--builders 4 y", "'y'"), ("--builders x", "'x'"), ("-j 300", "300"),
                           ("-v /:/host", "-v"), ("--docker-platform x", "--docker-platform"),
                           # out of the publish path, or into the environment
                           ("--day ..", "'..'"), ("--day .", "'.'"), ("--set release=../../x", "../../x"),
                           ("--set release=a/b", "a/b"), ("--set LD_PRELOAD=/tmp/x.so", "LD_PRELOAD"),
                           ("--set a=1,X=/y", "a=1,X=/y"), ("--flavour PATH=x", "PATH=x"),
                           ("--set https_proxy=evil.example", "https_proxy"), ("--set no_proxy=x", "no_proxy"),
                           ("--set path=tmp", "path=tmp"), ("--set fpath=x", "fpath")):
            why = build_args.problem(args.split())
            self.assertIsNotNone(why, args)
            self.assertIn(says, why, args)

    def test_only_the_platforms_image(self):
        self.assertIsNone(build_args.problem(SENT.split(), image=IMAGE))
        self.assertIn("only the platform's image", build_args.problem(SENT.split(), image="other"))
        self.assertIn("(none)", build_args.problem(SENT.split(), image=""))
        self.assertIsNone(build_args.problem(["--debug"], image=""))           # on the host
        self.assertIn("--docker: the platform has no image", build_args.problem(["--docker"], image=""))
        for args in (["--debug"], ["--docker"], ["--docker-image", IMAGE]):    # not out of its container
            self.assertIn("runs in its platform's image", build_args.problem(args, image=IMAGE), args)
        self.assertIsNone(build_args.problem(SENT.split()))                 # no image given: not checked

    def test_as_the_pipeline_runs_it(self):
        path = build_args.__file__
        run = lambda *a: subprocess.run([sys.executable, path, *a], capture_output=True, text=True)   # noqa: E731
        self.assertEqual(run("--image", IMAGE, "--", *SENT.split()).returncode, 0)
        r = run("--image", "", "--", "--docker-image", IMAGE)
        self.assertEqual((r.returncode, r.stderr.strip()), (1, "BITS_BUILD_ARGS: --docker-image: only the platform's image (none)"))
        self.assertEqual(run("--", "--insecure").returncode, 1)
        self.assertEqual(run().returncode, 0)


if __name__ == "__main__":
    unittest.main()
