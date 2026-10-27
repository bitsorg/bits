# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from bits_helpers.reuse_symlinks import serialize_relocation_table


RELOCATE = Path(__file__).parents[1] / "bits_helpers/relocate-references.sh"


def relocate_references(root, replacements):
    subprocess.run(["bash", str(RELOCATE), str(root),
                    serialize_relocation_table(replacements)], check=True)


class ReferenceRelocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()   # macOS: /var is /private/var
        self.stage = self.root / "stage"
        self.stage.mkdir()

    def test_all_dependency_paths_in_consumer_configs(self):
        text = ('-I/ref/sw/arch/external/hdf5/1/include '
                '-L/ref/sw/arch/external/zlib/1/lib '
                '-L/ref/sw/arch/external/openmpi/1/lib\n')
        for name in ("yoda.pc", "libYODA.la", "yoda-config", "YODAConfig.cmake"):
            (self.stage / name).write_text(text)
        relocate_references(self.stage, {"/ref/sw": "/local/sw"})
        for path in self.stage.iterdir():
            self.assertEqual(path.read_text(), text.replace("/ref/sw", "/local/sw"))
        relocate_references(self.stage, {"/ref/sw": "/local/sw"})

    def test_mapping_file_avoids_passing_table_as_argument(self):
        config = self.stage / "yoda.pc"
        config.write_text("-L/ref/sw/lib\n")
        mappings = self.root / "reuse-relocations.tsv"
        mappings.write_text(serialize_relocation_table({"/ref/sw": "/local/sw"}) + "\n")
        subprocess.run(["bash", str(RELOCATE), str(self.stage), "--file", str(mappings)],
                       check=True)
        self.assertEqual(config.read_text(), "-L/local/sw/lib\n")

    def test_no_symlinks_or_binary_files_are_followed_or_modified(self):
        original = self.root / "reference"
        original.mkdir()
        target = original / "config"
        target.write_text("/ref/sw/lib\n")
        (self.stage / "linked-file").symlink_to(target)
        (self.stage / "linked-directory").symlink_to(original)
        binary = self.stage / "library.so"
        binary.write_bytes(b"\x7fELF\0/ref/sw/lib")
        invalid = self.stage / "other-binary"
        invalid.write_bytes(b"\xff/ref/sw/lib")
        relocate_references(self.stage, {"/ref/sw": "/local/sw"})
        self.assertEqual(target.read_text(), "/ref/sw/lib\n")
        self.assertEqual(binary.read_bytes(), b"\x7fELF\0/ref/sw/lib")
        self.assertEqual(invalid.read_bytes(), b"\xff/local/sw/lib")
        result = subprocess.run(["bash", str(RELOCATE), str(self.stage / "linked-directory"),
                                 serialize_relocation_table({"/ref/sw": "/local/sw"})],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)

    def test_hardlinks_are_broken_without_changing_the_other_link(self):
        original = self.root / "source-config"
        original.write_text("/ref/sw/lib\n")
        original.chmod(0o555)
        local = self.stage / "wrapper"
        os.link(original, local)
        relocate_references(self.stage, {"/ref/sw": "/local/sw"})
        self.assertEqual(original.read_text(), "/ref/sw/lib\n")
        self.assertEqual(local.read_text(), "/local/sw/lib\n")

    def test_longest_prefix_and_sequential_literal_replacement_with_spaces(self):
        path = self.stage / "a config"
        path.write_text('/ref/Packages/gcc/1/lib /ref/Packages/other/lib /ref/Packages-old/lib\n')
        relocate_references(self.stage, {
            "/ref/Packages": "/local/arch",
            "/ref/Packages/gcc/1": "/local/toolchain/external/gcc/1",
        })
        self.assertEqual(path.read_text(),
                         '/local/toolchain/external/gcc/1/lib '
                         '/local/arch/other/lib '
                         '/local/arch-old/lib\n')

    def test_bsd_grep_and_xargs_options_are_not_used(self):
        # BSD grep reads -Z as "decompress", BSD xargs may lack -r: shims refuse both.
        shims = self.root / "bin"
        shims.mkdir()
        for tool, refused in (("grep", "Z"), ("xargs", "r")):
            real = shutil.which(tool)
            (shims / tool).write_text(
                '#!/bin/bash\nfor a in "$@"; do case "$a" in --*) ;; -*%s*) exit 9 ;; esac; done\n'
                'exec %s "$@"\n' % (refused, real))
            (shims / tool).chmod(0o755)
        (self.stage / "yoda.pc").write_text("-L/ref/sw/lib\n")
        env = dict(os.environ, PATH="%s:%s" % (shims, os.environ["PATH"]))
        subprocess.run(["bash", str(RELOCATE), str(self.stage),
                        serialize_relocation_table({"/ref/sw": "/local/sw"})], check=True, env=env)
        self.assertEqual((self.stage / "yoda.pc").read_text(), "-L/local/sw/lib\n")

    @unittest.skipUnless(shutil.which("perl") and shutil.which("grep"), "CMS reference commands required")
    def test_matches_cms_relocate_reference_shell_macro(self):
        fixtures = {
            "paths": b'/ref/sw/lib /ref/sw-other /ref/sw/BUILD/x\n',
            "latin": b'\xff /ref/sw/lib\n',
            "binary": b'ELF\0/ref/sw/lib',
        }
        oracle = self.root / "cms"
        oracle.mkdir()
        outside = self.root / "outside"
        outside.write_bytes(b'/ref/sw/lib\n')
        for directory in (oracle, self.stage):
            for name, content in fixtures.items():
                (directory / name).write_bytes(content)
            (directory / "link").symlink_to(outside)
        # SPEC_REFERENCE_REPO from cms-sw/pkgtools V00-34-XX, expanded.
        subprocess.run(["bash", "-e", "-c",
                        'find "$1" -type f | xargs --no-run-if-empty grep -Il "/ref/sw" '
                        '| xargs -n1 --no-run-if-empty perl -p -i -e '
                        "'s|\\Q/ref/sw\\E|/local/sw|g;'", "cms", str(oracle)], check=True,
                       env=dict(os.environ, LC_ALL="C"))
        relocate_references(self.stage, {"/ref/sw": "/local/sw"})
        for name in fixtures:
            self.assertEqual((self.stage / name).read_bytes(), (oracle / name).read_bytes())
        self.assertTrue((self.stage / "link").is_symlink())
        self.assertEqual(outside.read_bytes(), b'/ref/sw/lib\n')

    def test_template_rewrites_before_relocation_list_and_later_move(self):
        checkout = Path(__file__).parents[1]
        template = (checkout / "bits_helpers/build_template.sh").read_text()
        # Execute the actual post-install block, including relativization,
        # relocation-list collection and view generation, without compiling.
        block = template.split("# Make pkg-config and CMake package files relocation-independent:", 1)[1]
        block = "# Make pkg-config and CMake package files relocation-independent:" + block
        block = block.split("# Archive creation", 1)[0]
        for enabled in (False, True):
            with self.subTest(symlinks=enabled):
                work = self.root / ("on" if enabled else "off")
                pkgpath = "arch/external/yoda/1-hash"
                install = work / "INSTALLROOT/hash" / pkgpath
                (install / "etc/profile.d").mkdir(parents=True)
                (install / "lib/pkgconfig").mkdir(parents=True)
                flags = install / "lib/pkgconfig/yoda.pc"
                flags.write_text('Libs: -L/ref/sw/arch/external/hdf5/1/lib '
                                 '-L/ref/sw/arch/external/zlib/1/lib\n')
                mappings_file = work / "reuse-relocations.tsv"
                if enabled:
                    mappings_file.write_text(serialize_relocation_table(
                        {"/ref/sw": str(work)}) + "\n")
                env = dict(os.environ, WORK_DIR=str(work), INSTALLROOT=str(install),
                           PKGHASH="hash", PKGPATH=pkgpath, PKGNAME="yoda",
                           BITS_SCRIPT_DIR=str(checkout),
                           BITS_REUSE_RELOCATIONS_FILE=str(mappings_file) if enabled else "")
                subprocess.run(["bash", "-e", "-c", block], env=env, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                manifest = (install / "etc/profile.d/.bits-relocate").read_text()
                self.assertEqual("lib/pkgconfig/yoda.pc" in manifest, enabled)
                self.assertEqual("/ref/sw" in flags.read_text(), not enabled)
                if not enabled:
                    continue
                (install / "etc/profile.d/.bits-pkginfo").write_text(
                    'PKG_DIR="%s"\nPP="%s"\nPH=hash\n' % (work, pkgpath))
                shutil.copy(checkout / "bits_helpers/relocate-me.sh", install)
                moved = self.root / "downloaded" / pkgpath
                moved.parent.mkdir(parents=True)
                shutil.move(str(install), moved)
                env["WORK_DIR"] = str(self.root / "downloaded")
                subprocess.run(["bash", str(moved / "relocate-me.sh")], check=True, env=env)
                self.assertEqual((moved / "lib/pkgconfig/yoda.pc").read_text(),
                                 'Libs: -L%s/arch/external/hdf5/1/lib -L%s/arch/external/zlib/1/lib\n'
                                 % (self.root / "downloaded", self.root / "downloaded"))
