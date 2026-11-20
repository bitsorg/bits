"""bits' git runs nothing a repository's own hooks or config name (git._git)."""

import os
import shutil
import subprocess
import tempfile
import unittest

from bits_helpers import git as bits_git


class RepoConfigTest(unittest.TestCase):
    def test_a_mirrors_hook_and_ext_url_do_not_run(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        run = lambda *a: subprocess.run(a, check=True, capture_output=True)   # noqa: E731
        run("git", "init", "-q", "--bare", os.path.join(d, "mirror"))
        run("git", "init", "-q", os.path.join(d, "up"))
        run("git", "-C", os.path.join(d, "up"), "-c", "user.email=a@b", "-c", "user.name=a",
            "commit", "-q", "--allow-empty", "-m", "x")
        mirror, hit = os.path.join(d, "mirror"), os.path.join(d, "ran")
        hook = os.path.join(mirror, "hooks", "reference-transaction")    # as a build could leave them
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\n" % hit)
        os.chmod(hook, 0o755)
        run("git", "-C", mirror, "config", "protocol.ext.allow", "always")
        run("git", "-C", mirror, "config", "url.ext::touch %s.insteadOf" % hit, "https://example.invalid/")
        err, out = bits_git._git(("fetch", "-f", os.path.join(d, "up"), "+refs/heads/*:refs/heads/*"),
                                 mirror, False, [])
        self.assertEqual(err, 0, out)                                       # fetched, no hook
        err, _ = bits_git._git(("fetch", "https://example.invalid/x"), mirror, False, [])
        self.assertNotEqual(err, 0)                                         # ext:: refused
        self.assertFalse(os.path.exists(hit))


if __name__ == "__main__":
    unittest.main()
