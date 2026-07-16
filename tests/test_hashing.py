# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import codecs
import os.path
import re
import unittest

from collections import OrderedDict

from bits_helpers.build import storeHashes

LOGFILE = "build.log"
SPEC_RE = re.compile(r"spec = (OrderedDict\(\[\('package', '([^']+)'.*\)\]\))")
HASH_RE = re.compile(r"Hashes for recipe (.*) are "
                     r"(([0-9a-f]{40})(?:, [0-9a-f]{40})*) \(remote\)[,;] "
                     r"(([0-9a-f]{40})(?:, [0-9a-f]{40})*) \(local\)")


class KnownGoodHashesTestCase(unittest.TestCase):
    """Make sure storeHashes produces the same hashes as in a build log.

    It is assumed that the hashes in the build log are correct, i.e. the ones
    we want to get for the matching spec in the log.

    It is possible to provide old-style logs (mentioning one local and remote
    hash only) or new-style logs (mentioning all alternative remote and local
    hashes). If providing old-style logs, only the hashing for the primary
    hashes is checked.
    """

    @unittest.skipIf(not os.path.exists(LOGFILE),
                     "Need a reference build log at path " + LOGFILE)
    def test_hashes_match_build_log(self) -> None:
        checked = set()
        specs = {}
        with codecs.open(LOGFILE, encoding="utf-8") as logf:
            for line in logf:
                match = re.search(SPEC_RE, line)
                if match:
                    spec_expr, package = match.groups()
                    specs[package] = eval(spec_expr, {"OrderedDict": OrderedDict})
                    specs[package]["is_devel_pkg"] = False
                    continue
                match = re.search(HASH_RE, line)
                if not match:
                    continue
                package, alt_remote, remote, alt_local, local = match.groups()
                if package in checked:
                    # Once a package is built, it will have a second "spec ="
                    # and "Hashes for recipe" line in the log. In that case, we
                    # don't want to check the hashes are correct, as
                    # storeHashes doesn't do anything in that case (the spec
                    # from the log will already have hashes stored).
                    continue
                storeHashes(package, specs, considerRelocation=False)
                spec = specs[package]
                self.assertEqual(spec["remote_revision_hash"], remote)
                self.assertEqual(spec["local_revision_hash"], local)
                # For logs produced by old hash implementations (which didn't
                # consider spec["scm_refs"]), alt_{remote,local} will only
                # contain the primary hash anyway, so this works nicely.
                self.assertEqual(spec["remote_hashes"], alt_remote.split(", "))
                self.assertEqual(spec["local_hashes"], alt_local.split(", "))
                checked.add(package)
                continue


if __name__ == '__main__':
    unittest.main()


class NormalizeRecipeForHashTestCase(unittest.TestCase):
    """normalize_recipe_for_hash strips comments/blank lines for hashing only."""

    def _n(self, s):
        from bits_helpers.build import normalize_recipe_for_hash
        return normalize_recipe_for_hash(s)

    def test_drops_full_line_comments_and_blanks(self):
        self.assertEqual(self._n("# a\nmake\n\n  # b\n  ./x\n"), "make\n  ./x")

    def test_keeps_inline_hash_and_code(self):
        # A '#' after code is not a full-line comment; the line is kept verbatim.
        self.assertEqual(self._n('echo "a # b"\n'), 'echo "a # b"')

    def test_comment_only_edits_produce_identical_normalization(self):
        a = "# explain the thing in one way\nConfigure() { cmake .; }\n"
        b = "# totally different wording here\n\nConfigure() { cmake .; }\n"
        self.assertEqual(self._n(a), self._n(b))

    def test_preserves_hash_lines_inside_heredoc(self):
        r = ("cat > f <<'EOF'\n"
             "#%Module1.0\n"
             "# not a comment, this is data\n"
             "EOF\n"
             "# this one is a real comment\n"
             "make\n")
        out = self._n(r)
        self.assertIn("#%Module1.0", out)
        self.assertIn("# not a comment, this is data", out)
        self.assertNotIn("# this one is a real comment", out)

    def test_handles_dash_heredoc_terminator(self):
        r = "cat <<-END\n#data\n\tEND\n#comment\ndone\n"
        out = self._n(r)
        self.assertIn("#data", out)
        self.assertNotIn("#comment", out)
        self.assertIn("done", out)

    def test_non_string_passthrough(self):
        self.assertEqual(self._n(None), None)


class NormalizeRecipeMetadataExclusionTestCase(unittest.TestCase):
    """normalize_recipe_for_hash drops metadata/publish-policy keys from the YAML
    header so editing license/description/url/etc does not change the build hash."""

    HEADER = ("package: Foo\n"
              "version: \"1.0\"\n"
              "description: A physics tool\n"
              "license: MIT\n"
              "requires:\n"
              "  - CMake\n"
              "  - boost\n"
              "---\n"
              "#!/bin/bash\n"
              "make install\n")

    def _n(self, s):
        from bits_helpers.build import normalize_recipe_for_hash
        return normalize_recipe_for_hash(s)

    def test_metadata_keys_removed_from_header(self):
        out = self._n(self.HEADER)
        self.assertNotIn("license:", out)
        self.assertNotIn("description:", out)
        # functional fields survive
        self.assertIn("version:", out)
        self.assertIn("- boost", out)
        self.assertIn("make install", out)      # body untouched

    def test_license_edit_is_hash_invariant(self):
        a = self.HEADER
        b = self.HEADER.replace("license: MIT", "license: GPL-3.0-only")
        self.assertEqual(self._n(a), self._n(b))

    def test_description_edit_is_hash_invariant(self):
        a = self.HEADER
        b = self.HEADER.replace("description: A physics tool",
                                "description: Now with a much longer blurb")
        self.assertEqual(self._n(a), self._n(b))

    def test_adding_url_acknowledgment_source_redistributable_is_invariant(self):
        base = self.HEADER
        extra = self.HEADER.replace(
            "license: MIT\n",
            "license: MIT\n"
            "url: https://example.org/foo\n"
            "homepage: https://example.org\n"
            "acknowledgment: Thanks to the Foo Collaboration.\n"
            "source_url: b3://bucket/SOURCES/foo/1.0/foo-1.0.tar.gz\n"
            "redistributable: false\n")
        self.assertEqual(self._n(base), self._n(extra))

    def test_multiline_block_value_dropped_entirely(self):
        base = self.HEADER.replace("description: A physics tool\n", "")
        block = self.HEADER.replace(
            "description: A physics tool\n",
            "description: |\n"
            "  first line\n"
            "  second line\n")
        self.assertEqual(self._n(block), self._n(base))
        self.assertNotIn("second line", self._n(block))

    def test_functional_change_still_changes_hash_input(self):
        for mut in ("version: \"1.0\"", "- boost", "make install"):
            with self.subTest(mut=mut):
                b = self.HEADER.replace(mut, mut + "X")
                self.assertNotEqual(self._n(self.HEADER), self._n(b))

    def test_body_is_not_scanned_for_metadata_keys(self):
        # a "license:"/"description:" line in the shell body is data, kept verbatim
        r = ("package: Foo\n"
             "license: MIT\n"
             "---\n"
             "echo 'license: printed at runtime'\n"
             "cat > NOTICE <<'EOF'\n"
             "description: shown to users\n"
             "EOF\n")
        out = self._n(r)
        self.assertNotIn("license: MIT", out)                 # header key dropped
        self.assertIn("echo 'license: printed at runtime'", out)   # body kept
        self.assertIn("description: shown to users", out)          # heredoc data kept

    def test_similar_key_names_are_not_dropped(self):
        # exact-match only: "licenses"/"description_of" are not metadata keys
        r = "package: Foo\nlicenses: many\ndescription_of: x\n---\nmake\n"
        out = self._n(r)
        self.assertIn("licenses: many", out)
        self.assertIn("description_of: x", out)


class SourceKeysExcludedFromTextHashTestCase(unittest.TestCase):
    """source:/sources:/tag: are dropped from the recipe TEXT hash (storeHashes
    already folds the resolved source identity in from the spec), so declaring a
    git alternative on a tarball recipe is hash-neutral."""

    def _n(self, s):
        from bits_helpers.build import normalize_recipe_for_hash
        return normalize_recipe_for_hash(s)

    def test_source_sources_tag_stripped_from_header(self):
        r = ("package: foo\nversion: \"1.2.3\"\n"
             "source: https://git/foo.git\ntag: \"v%(version)s\"\n"
             "sources:\n  - https://a/foo-1.2.3.tar.gz\n"
             "requires:\n  - CMake\n---\nmake\n")
        out = self._n(r)
        for k in ("source:", "tag:", "sources:", "- https://a"):
            self.assertFalse(any(l.strip().startswith(k) for l in out.splitlines()), k)
        self.assertIn("requires:", out)      # functional keys kept
        self.assertIn("- CMake", out)
        self.assertIn("make", out)           # body kept

    def test_body_source_line_not_stripped(self):
        r = "package: foo\n---\necho 'source: not a header'\n"
        self.assertIn("echo 'source: not a header'", self._n(r))

    def _h(self, **over):
        from bits_helpers.build import storeHashes
        s = {"package": "foo", "version": "1.2.3", "commit_hash": "1.2.3", "tag": "1.2.3",
             "scm_refs": {}, "requires": [], "build_requires": [], "runtime_requires": [],
             "is_devel_pkg": False, "pkg_family": "",
             "sources": ["https://a/foo-1.2.3.tar.gz"],
             "recipe": "package: foo\nversion: \"1.2.3\"\n---\nmake\n"}
        s.update(over)
        specs = {"foo": s}
        storeHashes("foo", specs, considerRelocation=False)
        return specs["foo"]["remote_revision_hash"]

    def test_declaring_git_source_is_hash_neutral_in_tar_mode(self):
        # identical spec sources; only the recipe TEXT gains a git source/tag line
        plain = self._h()
        withgit = self._h(recipe="package: foo\nversion: \"1.2.3\"\n"
                                 "source: https://git/foo.git\ntag: \"v%(version)s\"\n---\nmake\n")
        self.assertEqual(plain, withgit)

    def test_real_source_changes_still_change_hash(self):
        self.assertNotEqual(self._h(sources=["https://a/X.tar.gz"]),
                            self._h(sources=["https://a/Y.tar.gz"]))
        g1 = self._h(source="g", commit_hash="aaa", tag="v1")
        g2 = self._h(source="g", commit_hash="bbb", tag="v1")
        self.assertNotEqual(g1, g2)
