# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for `bits checksums` (bits_helpers.checksums_cmd)."""

import os
import subprocess
import tempfile
import textwrap
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from bits_helpers import checksums_cmd as cc
from bits_helpers.checksum_store import parse_checksum_file

SHA = "c" * 40
URL = "https://example.com/src/"


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(text))


class _Repos(unittest.TestCase):
    """A recipe repository (lcg.bits) and a profile repository (stacks.bits)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lcg = os.path.join(self.tmp, "lcg.bits")
        self.stack = os.path.join(self.tmp, "stacks.bits")
        _w(os.path.join(self.lcg, "tarpkg.sh"), """\
            package: tarpkg
            version: "1.0"
            sources:
              - "%s%%(name)s-%%(version)s.tar.gz"
              - "(osx.*)%sosx-%%(version)s.tar.gz"
            patches:
              - "fix.patch:version=1.0"
            ---
            """ % (URL, URL))
        _w(os.path.join(self.lcg, "patches", "fix.patch"), "--- a\n+++ b\n")
        _w(os.path.join(self.lcg, "gitpkg.sh"), """\
            package: GitPkg
            version: v1
            tag: v1
            source: https://example.com/gitpkg.git
            ---
            """)
        _w(os.path.join(self.lcg, "both.sh"), """\
            package: both
            version: "2.0"
            tag: v2.0
            source: https://example.com/both.git
            sources:
              - "%sboth-%%(version)s.tar.gz"
            ---
            """ % URL)
        _w(os.path.join(self.lcg, "nosrc.sh"), "package: nosrc\nversion: v1\n---\n")
        _w(os.path.join(self.stack, "defaults-release.sh"), """\
            package: defaults-release
            version: v1
            variables:
              release: main
            ---
            """)
        _w(os.path.join(self.stack, "defaults-dev.sh"), """\
            package: defaults-dev
            version: v1
            overrides:
              tarpkg:
                version: "1.1"
              gitpkg:
                tag: v2
              gitpkg:osx:
                tag: v3
              both:
                version: "2.0"
            ---
            """)
        _w(os.path.join(self.stack, "defaults-head.sh"), """\
            package: defaults-head
            version: v1
            overrides:
              GitPkg:
                version: "%(tag_basename)s"
                tag: master
            ---
            """)

    def plan(self, profiles="all", repo=None, packages=()):
        return cc.plan(repo or self.stack, [self.lcg], profiles, list(packages), "ARCH")


class TestPlan(_Repos):

    def test_own_recipes_all_variants(self):
        todo = cc.plan(self.lcg, [], None, [], "ARCH")
        self.assertEqual(sorted(todo["tarpkg"]["sources"]),
                         [URL + "osx-1.0.tar.gz", URL + "tarpkg-1.0.tar.gz"])
        self.assertEqual(list(todo["tarpkg"]["patches"]), ["fix.patch"])
        self.assertEqual(todo["GitPkg"]["git"], [("https://example.com/gitpkg.git", "v1", False)])
        # tar mode (the default): the tarball, not the git tag
        self.assertEqual(list(todo["both"]["sources"]), [URL + "both-2.0.tar.gz"])
        self.assertEqual(todo["both"]["git"], [])
        self.assertFalse(any(todo["nosrc"][k] for k in ("sources", "patches", "git")))

    def test_profile_records_only_what_overrides_add(self):
        todo = self.plan()
        self.assertEqual(sorted(todo["tarpkg"]["sources"]),
                         [URL + "osx-1.1.tar.gz", URL + "tarpkg-1.1.tar.gz"])
        self.assertEqual(todo["tarpkg"]["origin"][URL + "tarpkg-1.1.tar.gz"], "dev")
        self.assertEqual(todo["tarpkg"]["patches"], {})          # unchanged patch
        tags = sorted(t for _, t, _ in todo["GitPkg"]["git"])
        self.assertEqual(tags, ["master", "v2", "v3"])            # plain + gated sets
        self.assertNotIn("both", todo)                            # override changes nothing
        self.assertNotIn("nosrc", todo)

    def test_selected_profiles_and_packages(self):
        todo = self.plan("head")
        self.assertEqual(list(todo), ["GitPkg"])
        self.assertEqual(self.plan("dev", packages=["TARPKG"]).keys(), {"tarpkg"})
        with self.assertRaises(ValueError):
            self.plan("nope")

    def test_git_source_mode(self):
        with patch.dict(os.environ, {"BITS_SOURCE_MODE": "git"}):
            todo = cc.plan(self.lcg, [], None, ["both"], "ARCH")
        self.assertEqual(todo["both"]["sources"], {})
        self.assertEqual(todo["both"]["git"], [("https://example.com/both.git", "v2.0", False)])

    def test_unresolvable_variant_is_an_error(self):
        _w(os.path.join(self.stack, "defaults-bad.sh"), """\
            package: defaults-bad
            version: v1
            overrides:
              tarpkg:
                version: "%(nosuchvar)s"
            ---
            """)
        todo = self.plan("bad")
        self.assertEqual(todo["tarpkg"]["errors"][0][2], "bad")

    def test_release_overrides_belong_to_the_release_profile(self):
        _w(os.path.join(self.stack, "defaults-release.sh"), """\
            package: defaults-release
            version: v1
            overrides:
              tarpkg:
                version: "1.5"
            ---
            """)
        todo = self.plan()
        origins = todo["tarpkg"]["origin"]
        self.assertEqual(origins[URL + "tarpkg-1.5.tar.gz"], "release")
        self.assertEqual(origins[URL + "tarpkg-1.1.tar.gz"], "dev")


class TestGitPin(unittest.TestCase):

    def _run(self, stdout, rc=0):
        res = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="fatal: nope\n")
        return patch("bits_helpers.checksums_cmd.subprocess.run", return_value=res)

    def test_annotated_tag_peeled(self):
        out = "%s\trefs/tags/v1\n%s\trefs/tags/v1^{}\n" % ("1" * 40, "2" * 40)
        with self._run(out):
            self.assertEqual(cc.git_pin("u", "v1"), ("pinned", "2" * 40))

    def test_branch_moves(self):
        with self._run("%s\trefs/heads/master\n" % ("3" * 40)):
            self.assertEqual(cc.git_pin("u", "master")[0], "moving")

    def test_commit_and_missing(self):
        self.assertEqual(cc.git_pin("u", SHA.upper()), ("commit", SHA))
        with self._run(""):
            with self.assertRaises(RuntimeError):
                cc.git_pin("u", "v9")
        with self._run("", rc=128):
            with self.assertRaisesRegex(RuntimeError, "nope"):
                cc.git_pin("u", "v9")


class _Command(_Repos):
    """Runs doChecksums with the network mocked."""

    def _args(self, **kw):
        base = dict(configDir=self.stack, recipeDirs=[self.lcg], defaultsProfiles="all",
                    pkgname=[], write=False, workDir=os.path.join(self.tmp, "sw"),
                    architecture="ARCH", remoteStore="", fresh=False, jobs=2)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _run(self, **kw):
        pins = {"v2": ("pinned", "2" * 40), "v3": ("pinned", "3" * 40), "master": ("moving", "4" * 40),
                "v1": ("pinned", "1" * 40)}
        buf = StringIO()
        with patch.object(cc, "hash_source", side_effect=lambda u, *_: "sha256:" + ("a" if "1.1" in u else "b") * 64), \
             patch.object(cc, "git_pin", side_effect=lambda s, t: pins[t]), \
             redirect_stdout(buf):
            rc = cc.doChecksums(self._args(**kw), parser=None)
        return rc, buf.getvalue()


class TestDoChecksums(_Command):

    def test_report_then_write_then_verify(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("moving", out)
        self.assertFalse(os.path.exists(os.path.join(self.stack, "checksums")))
        rc, out = self._run(write=True)
        self.assertEqual(rc, 0)
        git = parse_checksum_file(os.path.join(self.stack, "checksums", "gitpkg.checksum"))
        self.assertEqual(git["commits"], {"v2": "2" * 40, "v3": "3" * 40})   # no branch pin
        tar = parse_checksum_file(os.path.join(self.stack, "checksums", "tarpkg.checksum"))
        self.assertEqual(tar["sources"], {URL + "tarpkg-1.1.tar.gz": "sha256:" + "a" * 64,
                                          URL + "osx-1.1.tar.gz": "sha256:" + "a" * 64})
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("new ", out)
        self.assertIn("4 ok, 1 moving", out)

    def test_mismatch_is_reported_and_kept(self):
        _w(os.path.join(self.stack, "checksums", "tarpkg.checksum"),
           "sources:\n  %star%s: sha256:%s\n" % (URL, "pkg-1.1.tar.gz", "f" * 64))
        rc, out = self._run(write=True)
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH", out)
        tar = parse_checksum_file(os.path.join(self.stack, "checksums", "tarpkg.checksum"))
        self.assertEqual(tar["sources"][URL + "tarpkg-1.1.tar.gz"], "sha256:" + "f" * 64)

    def test_inline_checksum_checked(self):
        _w(os.path.join(self.lcg, "inl.sh"), """\
            package: inl
            version: "1"
            sources:
              - "%sinl-1.tar.gz,sha256:%s"
            ---
            """ % (URL, "e" * 64))
        rc, out = self._run(configDir=self.lcg, recipeDirs=[], defaultsProfiles=None, pkgname=["inl"])
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH inl", out)

    def test_failure_exits_nonzero(self):
        buf = StringIO()
        with patch.object(cc, "hash_source", side_effect=OSError("404")), \
             patch.object(cc, "git_pin", return_value=("pinned", SHA)), redirect_stdout(buf):
            rc = cc.doChecksums(self._args(pkgname=["tarpkg"]), parser=None)
        self.assertEqual(rc, 1)
        self.assertIn("failed   tarpkg", buf.getvalue())


class TestHashSource(unittest.TestCase):

    def test_uses_the_download_cache(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "x-1.tar.gz")
        with open(src, "wb") as fh:
            fh.write(b"abc")
        got = cc.hash_source("file://" + src, os.path.join(tmp, "sw"), None)
        self.assertEqual(got, "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertTrue(os.path.isdir(os.path.join(tmp, "sw", "SOURCES", "cache")))


class TestReviewFollowUps(_Repos):

    def test_profile_variables_change_a_tag(self):
        _w(os.path.join(self.lcg, "varpkg.sh"), """\
            package: varpkg
            version: v1
            tag: "rel-%(flavour)s"
            source: https://example.com/varpkg.git
            ---
            """)
        _w(os.path.join(self.stack, "defaults-release.sh"),
           "package: defaults-release\nversion: v1\nvariables:\n  flavour: std\n---\n")
        _w(os.path.join(self.stack, "defaults-alt.sh"),
           "package: defaults-alt\nversion: v1\nvariables:\n  flavour: alt\n---\n")
        todo = self.plan("alt,release")
        self.assertEqual(todo["varpkg"]["git"], [("https://example.com/varpkg.git", "rel-alt", False)])

    def test_release_tag_change_not_repeated_per_profile(self):
        _w(os.path.join(self.stack, "defaults-release.sh"),
           "package: defaults-release\nversion: v1\noverrides:\n  gitpkg:\n    tag: v9\n---\n")
        todo = self.plan("release,head")
        origins = {t: todo["GitPkg"]["origin"][t] for _, t, _ in todo["GitPkg"]["git"]}
        self.assertEqual(origins, {"v9": "release", "master": "head"})

    def test_package_field_must_match_file_name(self):
        _w(os.path.join(self.lcg, "evil.sh"), "package: ../evil\nversion: v1\nsources:\n  - %sx.tgz\n---\n" % URL)
        self.assertNotIn("../evil", cc.plan(self.lcg, [], None, [], "ARCH"))

    def test_option_like_git_source_refused(self):
        with self.assertRaises(RuntimeError):
            cc.git_pin("--upload-pack=touch /tmp/x", "v1")


class TestDoChecksumsFollowUps(_Command):

    def test_other_algorithm_is_unverified(self):
        _w(os.path.join(self.stack, "checksums", "tarpkg.checksum"),
           "sources:\n  %starpkg-1.1.tar.gz: md5:%s\n" % (URL, "f" * 32))
        rc, out = self._run(pkgname=["tarpkg"])
        self.assertEqual(rc, 0)
        self.assertIn("unverified tarpkg", out)

    def test_inline_other_algorithm_still_recorded(self):
        _w(os.path.join(self.lcg, "inl.sh"), """\
            package: inl
            version: "1"
            sources:
              - "%sinl-1.tar.gz,md5:%s"
            ---
            """ % (URL, "e" * 32))
        rc, out = self._run(configDir=self.lcg, recipeDirs=[], defaultsProfiles=None, pkgname=["inl"])
        self.assertEqual(rc, 0)
        self.assertIn("new      inl", out)

    def test_broken_unrelated_recipe_is_not_an_error(self):
        _w(os.path.join(self.lcg, "broken.sh"),
           "package: broken\nversion: \"%(nosuch)s\"\nsources:\n  - " + URL + "b.tgz\n---\n")
        self.assertNotIn("broken", self.plan())
        self.assertTrue(cc.plan(self.lcg, [], None, [], "ARCH")["broken"]["errors"])

    def test_write_failure_exits_nonzero(self):
        with patch.object(cc, "update_checksum_file", side_effect=OSError("read-only")):
            rc, _ = self._run(write=True, pkgname=["tarpkg"])
        self.assertEqual(rc, 1)

    def test_fresh_uses_a_private_cache_and_no_mirror(self):
        seen = []
        buf = StringIO()
        with patch.object(cc, "hash_source", side_effect=lambda u, w, s: seen.append((w, s)) or "sha256:" + "a" * 64), \
             redirect_stdout(buf):
            cc.doChecksums(self._args(fresh=True, remoteStore="https://mirror", pkgname=["tarpkg"]), None)
        work = os.path.join(self.tmp, "sw")
        self.assertTrue(seen and all(w.startswith(work + os.sep) and w != work for w, _ in seen))
        self.assertFalse(any(type(s).__name__ == "HttpRemoteSync" for _, s in seen))
        self.assertEqual(os.listdir(work), [])            # cleaned up


class TestBuildWriteChecksums(unittest.TestCase):
    """bits build --write-checksums: the download cache, the recipe's patches/,
    merging into the existing file."""

    def setUp(self):
        from bits_helpers.download import getUrlChecksum
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "sw")
        self.pkgdir = os.path.join(self.tmp, "repo.bits")
        self.url = URL + "p-1.tar.gz"
        h = getUrlChecksum(self.url)
        _w(os.path.join(self.work, "SOURCES", "cache", h[:2], h, "p-1.tar.gz"), "abc")
        _w(os.path.join(self.pkgdir, "patches", "fix.patch"), "abc")
        _w(os.path.join(self.pkgdir, "checksums", "p.checksum"),
           "sources:\n  https://old/x.tar.gz: sha256:%s\n" % ("1" * 64))

    def _write(self, **spec):
        from unittest.mock import MagicMock
        from bits_helpers.build import _write_checksums_for_spec
        scm = MagicMock()
        scm.checkedOutCommitName.return_value = SHA
        base = {"package": "p", "version": "1", "pkgdir": self.pkgdir, "commit_hash": "v1",
                "tag": "v1", "sources": ["(osx.*)" + URL + "mac.tar.gz", "((?!osx).*)" + self.url],
                "patches": ["fix.patch"]}
        base.update(spec)
        base["scm"] = scm
        _write_checksums_for_spec(base, self.work, "slc9_x86-64")
        return parse_checksum_file(os.path.join(self.pkgdir, "checksums", "p.checksum"))

    def test_sources_patches_and_existing_entries(self):
        abc = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        got = self._write()
        self.assertEqual(got["sources"], {self.url: abc, "https://old/x.tar.gz": "sha256:" + "1" * 64})
        self.assertEqual(got["patches"], {"fix.patch": abc})
        self.assertEqual(got["commits"], {})             # no git source

    def test_tag_pinned_branch_not(self):
        got = self._write(source="https://example.com/p.git", sources=[])
        self.assertEqual(got["commits"], {"v1": SHA})
        os.remove(os.path.join(self.pkgdir, "checksums", "p.checksum"))
        got = self._write(source="https://example.com/p.git", sources=[], tag="master",
                          commit_hash="9" * 40)
        self.assertEqual(got["commits"], {})


if __name__ == "__main__":
    unittest.main()


class TestSmallerPoints(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_prefetch_resolves_arch_gates_and_only_warms_the_cache(self):
        from bits_helpers import build
        spec = {"package": "p", "version": "1", "is_devel_pkg": True,
                "sources": ["(osx.*)" + URL + "mac.tgz", "((?!osx).*)" + URL + "linux.tgz"]}
        calls = []
        with patch("bits_helpers.download.download", side_effect=lambda *a, **k: calls.append(a)), \
             patch("bits_helpers.sync.source_sync_for", side_effect=lambda s, h: h):
            build._prefetch_package(dict(spec, hash="ab12"), None, self.tmp,
                                    "slc9_x86-64-gcc14", "slc9_x86-64")
        self.assertEqual(calls, [(URL + "linux.tgz", None, self.tmp)])

    def test_prefetch_survives_a_fatal_url(self):
        from bits_helpers import build
        spec = {"package": "p", "version": "1", "is_devel_pkg": True, "hash": "ab12",
                "sources": ["weird://x/y.tgz"]}
        with patch("bits_helpers.sync.source_sync_for", side_effect=lambda s, h: h):
            build._prefetch_package(spec, None, self.tmp, "slc9_x86-64")   # no SystemExit

    def test_readdefaults_records_override_dirs(self):
        from bits_helpers.defaults import readDefaults
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        _w(os.path.join(a, "defaults-release.sh"),
           "package: defaults-release\nversion: v1\noverrides:\n  ROOT:\n    tag: v1\n---\n")
        _w(os.path.join(b, "defaults-dev.sh"),
           "package: defaults-dev\nversion: v1\noverrides:\n  ROOT:osx:\n    tag: v2\n  Boost@x:\n    version: '1'\n"
           "  ROOT:\n    env: {X: '1'}\n---\n")
        with patch.dict(os.environ, {"BITS_PATH": b}):
            meta, _ = readDefaults(a, ["release", "dev"], lambda *_: None, "slc9_x86-64")
        self.assertEqual(meta["_override_dirs"], {"root": a, "root:osx": b, "boost": b})
        self.assertEqual(meta["_version_profiles"], {"boost": "dev"})      # who pins a version

    def test_build_write_respects_a_legacy_pin(self):
        from unittest.mock import MagicMock
        from bits_helpers.build import _write_checksums_for_spec
        repo = os.path.join(self.tmp, "r")
        scm = MagicMock()
        scm.checkedOutCommitName.return_value = "b" * 40
        spec = {"package": "g", "version": "v1", "tag": "v1", "commit_hash": "v1", "pkgdir": repo,
                "source": "https://example.com/g.git", "scm": scm, "pin_commit": "a" * 40}
        with patch("bits_helpers.build.warning") as warn:
            _write_checksums_for_spec(spec, self.tmp, "slc9_x86-64")
        self.assertFalse(os.path.exists(os.path.join(repo, "checksums")))   # B never supersedes A
        warn.assert_called_once()
        _write_checksums_for_spec(dict(spec, pin_commit="b" * 40), self.tmp, "slc9_x86-64")
        got = parse_checksum_file(os.path.join(repo, "checksums", "g.checksum"))
        self.assertEqual(got["commits"], {"v1": "b" * 40})                  # migrated

    def test_build_write_goes_to_the_profile_repo_and_skips_known(self):
        from unittest.mock import MagicMock
        from bits_helpers.build import _write_checksums_for_spec
        from bits_helpers.download import getUrlChecksum
        recipes, stack, work = (os.path.join(self.tmp, x) for x in ("lcg", "stack", "sw"))
        for name in ("p-1.tgz", "p-2.tgz"):
            h = getUrlChecksum(URL + name)
            _w(os.path.join(work, "SOURCES", "cache", h[:2], h, name), "abc")
        _w(os.path.join(recipes, "patches", "fix.patch"), "abc")
        abc = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        spec = {"package": "p", "version": "2", "tag": "2", "commit_hash": "2",
                "pkgdir": recipes, "checksums_dir": stack,
                "sources": [URL + "p-1.tgz", URL + "p-2.tgz"], "patches": ["fix.patch"],
                "source_checksums": {URL + "p-1.tgz": abc}, "scm": MagicMock()}
        _write_checksums_for_spec(spec, work, "slc9_x86-64")
        got = parse_checksum_file(os.path.join(stack, "checksums", "p.checksum"))
        self.assertEqual(got["sources"], {URL + "p-2.tgz": abc})       # p-1 already known
        self.assertEqual(got["patches"], {"fix.patch": abc})
        self.assertFalse(os.path.exists(os.path.join(recipes, "checksums")))


class TestLegacyTagChecked(_Command):

    def test_legacy_tag_mismatch_reported(self):
        _w(os.path.join(self.lcg, "checksums", "gitpkg.checksum"), "tag: %s\n" % ("9" * 40))
        rc, out = self._run(configDir=self.lcg, recipeDirs=[], defaultsProfiles=None, pkgname=["gitpkg"])
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH GitPkg  tag v1", out)

    def test_matching_legacy_tag_gets_a_commits_entry(self):
        _w(os.path.join(self.lcg, "checksums", "gitpkg.checksum"), "tag: %s\n" % ("1" * 40))
        rc, out = self._run(configDir=self.lcg, recipeDirs=[], defaultsProfiles=None,
                            pkgname=["gitpkg"], write=True)
        self.assertEqual(rc, 0)
        got = parse_checksum_file(os.path.join(self.lcg, "checksums", "gitpkg.checksum"))
        self.assertEqual(got["commits"], {"v1": "1" * 40})
