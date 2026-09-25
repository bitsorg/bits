"""relativize-configs.sh: rewrite absolute install prefixes baked into .pc / .cmake
files to self-relative anchors (.pc -> ${pcfiledir}, .cmake -> ${CMAKE_CURRENT_LIST_DIR}),
so a package resolves correctly wherever it is laid down, including reuse that skips
relocate-me.sh. The helper is the extracted, portable (BSD+GNU sed) form of the block
that used to live inline in build_template.sh.
"""
import os
import subprocess
import tempfile

import bits_helpers

HELPER = os.path.join(os.path.dirname(bits_helpers.__file__), "relativize-configs.sh")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _run(root):
    subprocess.run(["bash", HELPER, root], check=True)


def test_pc_and_cmake_become_self_relative():
    with tempfile.TemporaryDirectory() as root:
        pc = os.path.join(root, "lib", "pkgconfig", "foo.pc")
        cm = os.path.join(root, "lib", "cmake", "Foo", "FooConfig.cmake")
        _write(pc, "prefix=%s\nincludedir=%s/include\nCflags: -I${includedir}\n" % (root, root))
        _write(cm, 'set(FOO_DIR "%s/include")\n' % root)
        _run(root)
        pc_txt = open(pc).read()
        cm_txt = open(cm).read()
        # from lib/pkgconfig, the package root is two levels up
        assert "prefix=${pcfiledir}/../..\n" in pc_txt
        assert "includedir=${pcfiledir}/../../include\n" in pc_txt
        # from lib/cmake/Foo, three levels up
        assert "${CMAKE_CURRENT_LIST_DIR}/../../../include" in cm_txt
        # no absolute prefix left anywhere
        assert root not in pc_txt
        assert root not in cm_txt


def test_pc_at_root_uses_bare_anchor():
    with tempfile.TemporaryDirectory() as root:
        pc = os.path.join(root, "foo.pc")
        _write(pc, "prefix=%s\n" % root)
        _run(root)
        assert open(pc).read() == "prefix=${pcfiledir}\n"


def test_relocatable_file_is_untouched():
    with tempfile.TemporaryDirectory() as root:
        pc = os.path.join(root, "lib", "pkgconfig", "clean.pc")
        original = "prefix=${pcfiledir}/../..\nCflags: -I${prefix}/include\n"
        _write(pc, original)
        _run(root)
        assert open(pc).read() == original


def test_idempotent_and_no_backup_left():
    with tempfile.TemporaryDirectory() as root:
        pc = os.path.join(root, "lib", "pkgconfig", "foo.pc")
        _write(pc, "prefix=%s\n" % root)
        _run(root)
        first = open(pc).read()
        _run(root)
        assert open(pc).read() == first
        # the sed -i.suffix backup must be cleaned up
        leftovers = [f for _, _, files in os.walk(root) for f in files if f.endswith(".bits-reloc")]
        assert leftovers == []


SELF_PREFIX = 'prefix=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)'


def test_config_script_prefix_becomes_self_relative():
    with tempfile.TemporaryDirectory() as root:
        cfg = os.path.join(root, "bin", "foo-config")
        _write(cfg, "#!/bin/sh\nprefix=%s\nexec_prefix=${prefix}\n"
                    "includedir=${prefix}/include\nlibdir=${exec_prefix}/lib\n"
                    "echo -I${includedir} -L${libdir} -lfoo\n" % root)
        os.chmod(cfg, 0o755)
        _run(root)
        txt = open(cfg).read()
        # the one absolute line is recomputed from $0; everything else derived
        # from ${prefix} is left as-is
        assert SELF_PREFIX + "\n" in txt
        assert "exec_prefix=${prefix}\n" in txt
        assert "includedir=${prefix}/include\n" in txt
        assert root not in txt
        # +x bit preserved (a *-config must stay runnable)
        assert os.access(cfg, os.X_OK)


def test_config_script_quoted_prefix_and_literal_libdir():
    with tempfile.TemporaryDirectory() as root:
        cfg = os.path.join(root, "bin", "bar-config")
        # quoted prefix assignment + a literal own-root libdir (multiarch idiom)
        _write(cfg, '#!/bin/sh\nprefix="%s"\nlibdir=%s/lib64\n'
                    'echo -L${libdir}\n' % (root, root))
        os.chmod(cfg, 0o755)
        _run(root)
        txt = open(cfg).read()
        assert SELF_PREFIX + "\n" in txt          # quoted form recognised
        assert "libdir=${prefix}/lib64\n" in txt  # literal own-root repointed
        assert root not in txt


def test_config_script_foreign_dep_path_untouched():
    # over-rewrite negative control: a DIFFERENT package's absolute path
    # (a different INSTALLROOT hash) must survive — we only rewrite our own root.
    with tempfile.TemporaryDirectory() as root:
        foreign = "/some/other/INSTALLROOT/%s/x86-64/dep/1.0" % ("d" * 40)
        cfg = os.path.join(root, "bin", "baz-config")
        _write(cfg, "#!/bin/sh\nprefix=%s\necho -L${prefix}/lib -L%s/lib\n"
                    % (root, foreign))
        os.chmod(cfg, 0o755)
        _run(root)
        txt = open(cfg).read()
        assert SELF_PREFIX + "\n" in txt
        assert foreign in txt          # dependency path left alone
        assert root not in txt


def test_config_script_resolves_after_move():
    # prove the endpoint: relativise, move the tree, the script emits the NEW path
    import shutil
    with tempfile.TemporaryDirectory() as parent:
        root = os.path.join(parent, "orig")
        os.makedirs(os.path.join(root, "bin"))
        cfg = os.path.join(root, "bin", "foo-config")
        _write(cfg, "#!/bin/sh\nprefix=%s\nincludedir=${prefix}/include\n"
                    "echo -I${includedir}\n" % root)
        os.chmod(cfg, 0o755)
        _run(root)
        moved = os.path.join(parent, "moved")
        shutil.move(root, moved)
        out = subprocess.run([os.path.join(moved, "bin", "foo-config")],
                             capture_output=True, text=True, check=True).stdout
        assert ("-I%s/include" % moved) in out


def test_config_script_idempotent():
    with tempfile.TemporaryDirectory() as root:
        cfg = os.path.join(root, "bin", "foo-config")
        _write(cfg, "#!/bin/sh\nprefix=%s\n" % root)
        os.chmod(cfg, 0o755)
        _run(root)
        first = open(cfg).read()
        _run(root)
        assert open(cfg).read() == first
        leftovers = [f for _, _, files in os.walk(root) for f in files if f.endswith(".bits-reloc")]
        assert leftovers == []


def test_config_script_without_prefix_line_left_untouched():
    # a *-config that bakes the root but defines no prefix= assignment must be
    # left byte-identical, never rewritten to an undefined ${prefix}.
    with tempfile.TemporaryDirectory() as root:
        cfg = os.path.join(root, "bin", "noprefix-config")
        original = "#!/bin/sh\necho -I%s/include -L%s/lib\n" % (root, root)
        _write(cfg, original)
        os.chmod(cfg, 0o755)
        _run(root)
        assert open(cfg).read() == original   # untouched, still absolute


def test_relocate_list_built_after_hooks_and_relativize():
    # .bits-relocate must be written after the POST_INSTALL hooks and the
    # relativize pass, or it lists configs already made relative and misses
    # files the hooks wrote.
    tmpl = os.path.join(os.path.dirname(bits_helpers.__file__), "build_template.sh")
    src = open(tmpl).read()
    writer = src.index('> "$INSTALLROOT/etc/profile.d/.bits-relocate"')
    assert src.count("etc/profile.d/.bits-relocate\"") == 1
    assert writer > src.index('run_hooks "POST_INSTALL"')
    assert writer > src.index("bits_helpers/relativize-configs.sh\" \"$INSTALLROOT\"")
