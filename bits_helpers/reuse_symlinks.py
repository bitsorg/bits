# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local views of reused packages: linked payload, private initialization files."""

import hashlib
import json
import os
from pathlib import Path
import re
from shlex import quote
from shlex import split
import shutil
import stat
import subprocess
import tempfile

from bits_helpers.arch import effective_arch
from bits_helpers.log import debug
from bits_helpers.utilities import pkg_to_shell_id
from bits_helpers.utilities import is_virtual_package


def find_symlink_package(spec, deployment_root, architecture, policy="strict"):
    """Find a package in ROOT/<effective-arch>/[family/]package/version[-revision].

    Only inspect the requested architecture and version. Strict selection also
    checks .build-hash against the computed remote identity. An explicit
    force_revision (including an empty one) pins the directory label; an
    automatically injected hash label still permits other revisions of the same
    version. Prefer the expected label, then use deterministic directory order.
    """
    if policy not in ("strict", "relaxed"):
        raise ValueError("Unknown symlink policy: %s" % policy)
    package, version = spec["package"], spec["version"]
    arch, family = effective_arch(spec, architecture), spec.get("pkg_family", "")
    for segment in (package, version, arch, family):
        if segment and (segment in (".", "..") or "/" in segment or "\\" in segment):
            raise ValueError("Invalid symlink package path segment: %r" % segment)
    base = Path(deployment_root).absolute() / arch
    parents = [base / family / package] if family else []
    parents.append(base / package)
    # Deployment roots represent published packages. Match their remote identity
    # only; the local hash has an extra local marker and is not a valid deployed
    # artifact identity for strict reuse.
    expected_hash = spec.get("remote_revision_hash")
    pinned = "force_revision" in spec and not spec.get("_revision_policy_hash_injected")
    revision = spec.get("force_revision", spec.get("revision", ""))
    expected = version + ("-" + str(revision) if revision else "")
    for parent in parents:
        if not parent.is_dir():
            continue
        for path in sorted(parent.iterdir(), key=lambda p: (p.name != expected, p.name)):
            if pinned and path.name != expected:
                continue
            if path.name != version and not path.name.startswith(version + "-"):
                continue
            if not path.is_dir():
                continue
            if not (path / "etc/profile.d/init.sh").is_file():
                debug("Symlink candidate %s skipped: missing etc/profile.d/init.sh", path)
                continue
            try:
                source_hash = (path / ".build-hash").read_text().strip()
            except (OSError, UnicodeError):
                source_hash = ""
            if policy == "strict" and (not expected_hash or source_hash != expected_hash):
                debug("Symlink candidate %s skipped: .build-hash=%s, expected %s", path,
                      source_hash or "<missing>", expected_hash or "<missing remote hash>")
                continue
            return {"source": str(path), "install_base": str(base),
                    "revision": path.name[len(version) + 1:] if path.name != version else "",
                    "hash": source_hash or spec.get("remote_revision_hash", "")}
    return None


def select_symlink_package(spec, cfg, architecture):
    """The same eligibility and lookup for builds and read-only plans."""
    root = getattr(cfg, "symlink_packages_from", None)
    if not root:
        return None
    build_local = cfg.build_local or []
    if isinstance(build_local, str):
        build_local = build_local.split(",")
    if (spec.get("is_devel_pkg") or is_virtual_package(spec)
            or spec["package"].startswith("defaults-") or spec["package"] in build_local):
        return None
    return find_symlink_package(spec, root, architecture, cfg.symlink_policy)


def matching_local_install(spec, match, work_dir, architecture):
    """Return an existing complete installation with the selected build hash.

    A regular installation already in the work area needs no symlink stage.
    Existing symlinks still go through staging so their init.sh becomes private.
    """
    if not match or not match.get("hash"):
        return None
    arch = effective_arch(spec, architecture)
    family = spec.get("pkg_family", "")
    version = spec["version"]
    revision = match["revision"]
    label = version + ("-" + revision if revision else "")
    local = Path(work_dir) / arch
    if family:
        local /= family
    local = local / spec["package"] / label
    # A staged view is a real directory with a copied .build-hash, but its
    # private init.sh may need regeneration after bits changes its wrapper.
    if (local / ".bits-reuse-symlinks.json").exists():
        return None
    if local.is_symlink() or not (local / "etc/profile.d/init.sh").is_file():
        return None
    try:
        if (local / ".build-hash").read_text().strip() == match["hash"]:
            return str(local)
    except (OSError, UnicodeError):
        pass
    return None


def _run_checked(command):
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise ValueError("Reference package setup failed: %s" % command[0]) from exc


def serialize_relocation_table(replacements):
    """Serialize mappings for relocate-references.sh without shell evaluation."""
    rows = []
    ordered = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    for source, destination in ordered:
        if any(c in source or c in destination for c in ("\t", "\n")):
            raise ValueError("--symlink-packages-from cannot relocate paths containing tabs or newlines")
        rows.append(source + "\t" + destination)
    return "\n".join(rows)


def _relocate_references(root, replacements):
    script = Path(__file__).with_name("relocate-references.sh")
    _run_checked(["bash", str(script), str(root), serialize_relocation_table(replacements)])


def _incompatible_entry(existing, staged, source, relative=Path()):
    """First incompatible path, or None when an existing stage can be adopted.

    Reuse the caller's already-created source symlinks, including a whole-package
    symlink, by replacing only the local directory entry with our prepared tree.
    Any local regular files must match the staged copies; foreign links/data fail
    closed. Symlink targets are compared after resolving, never followed for edit.
    """
    if existing.is_symlink():
        try:
            actual = existing.resolve(strict=True)
        except OSError:
            return existing
        try:
            original = (source / relative).resolve(strict=True)
        except OSError:
            return existing
        if actual != staged.resolve() and actual != original:
            return existing
        if not ((staged.is_dir() and actual.is_dir()) or (staged.is_file() and actual.is_file())):
            return existing
        return None
    if staged.is_symlink():
        return existing
    if existing.is_dir():
        if not staged.is_dir():
            return existing
        for item in existing.iterdir():
            target = staged / item.name
            if not (target.exists() or target.is_symlink()):
                return item
            conflict = _incompatible_entry(item, target, source, relative / item.name)
            if conflict is not None:
                return conflict
        return None
    if not existing.is_file() or not staged.is_file():
        return existing
    try:
        return None if existing.read_bytes() == staged.read_bytes() else existing
    except OSError:
        return existing


def _install_stage(staged, destination, source):
    """Install a prepared package, preserving a rollback copy if it exists."""
    if not os.path.lexists(destination):
        staged.rename(destination)
        return None
    conflict = _incompatible_entry(destination, staged, source)
    if conflict is not None:
        raise ValueError("--symlink-packages-from found incompatible existing content at %s "
                         "(first conflict: %s)" % (destination, conflict))
    backup = Path(tempfile.mkdtemp(prefix=".reuse-old-", dir=str(destination.parent)))
    old = backup / "package"
    destination.rename(old)
    try:
        staged.rename(destination)
    except BaseException:
        try:
            old.rename(destination)
        except BaseException:
            # Keep the old tree in its backup location if rollback itself fails.
            raise
        shutil.rmtree(backup)
        raise
    return backup


def stage_reused_package(spec, install_base, work_dir, architecture,
                         *, source_path, runtime_work_dir=None):
    """Stage a selected package under work_dir without writing to its source.

    Directories containing private files are real directories; the other entries
    are symlinks. No relocation program is run on a linked tree, since it could
    edit the deployment. Publication of consumers still requires their dependency
    closure at the destination. Config files inside linked lib/bin remain as-is.
    """
    base = Path(install_base).absolute()
    work = Path(work_dir).absolute()
    runtime = Path(runtime_work_dir or work).absolute()
    arch = effective_arch(spec, architecture)
    package, verrev = spec["package"], Path(source_path).name
    family = spec.get("pkg_family", "")
    for segment in (package, verrev, arch, family):
        if segment and (segment in (".", "..") or "/" in segment):
            raise ValueError("Invalid reused package path segment: %r" % segment)
    suffix = Path(family) / package / verrev if family else Path(package) / verrev
    source = Path(source_path).absolute()
    if not (source / "etc/profile.d/init.sh").is_file():
        raise ValueError("--symlink-packages-from needs the deployed init.sh for %s under %s"
                         % (package + "/" + verrev, base))
    destination = work / arch / suffix
    local_root = runtime / arch / suffix
    parent = destination.parent
    while parent != work:
        if parent.is_symlink():
            raise ValueError("--symlink-packages-from refuses a linked destination parent: %s" % parent)
        parent = parent.parent
    if destination.absolute() == source.absolute():
        raise ValueError("--symlink-packages-from source and destination are the same: %s" % source)

    original = (source / "etc/profile.d/init.sh").read_text()
    # Longest first: the flat deployment and the local family layout may differ.
    replacements = {str(source): str(local_root),
                    str(base): str(runtime / arch)}
    replacements[str(base.parent)] = str(runtime)
    # Older init.sh files default WORK_DIR to their original build workdir.
    # Read the literal PKG_DIR without executing the deployment's shell code.
    pkginfo = source / "etc/profile.d/.bits-pkginfo"
    if pkginfo.is_file():
        for line in pkginfo.read_text().splitlines():
            if line.startswith("PKG_DIR="):
                value = split(line[len("PKG_DIR="):])
                if len(value) == 1 and os.path.isabs(value[0]) and value[0] != "/":
                    replacements.setdefault(value[0], str(runtime))
    # Consumers can inherit reference paths from linked .pc/.la files or
    # compiler wrappers. Carry these mappings to their post-install rewrite.
    # Include the physical root in case a tool resolves our source symlink.
    replacements.setdefault(str(source.resolve()), str(local_root))
    spec["reuse_reference_paths"] = dict(replacements)
    pattern = re.compile("|".join(
        re.escape(p) for p in sorted(replacements, key=len, reverse=True)))
    relocated = pattern.sub(lambda m: replacements[m.group()], original)
    # A shared or build-type-neutral package can still depend on packages under
    # the consumer's architecture. Its own init.sh uses a literal effective arch
    # for its ROOT, but its dependencies use BITS_ARCH_PREFIX. Keep that prefix
    # at the consumer architecture while sourcing, then restore the caller's.
    saved = "_bits_reuse_arch_" + pkg_to_shell_id(package)
    def wrapped_init(prefix):
        return ('if [ -z "${WORK_DIR:-}" ]; then WORK_DIR=%s; fi\n'
                '%s="${BITS_ARCH_PREFIX-}"; %s_set="${BITS_ARCH_PREFIX+x}"\n'
                'BITS_ARCH_PREFIX=%s\n' % (quote(str(runtime)), saved, saved, quote(prefix))
                + relocated.rstrip() + "\n"
                + ('if [ -n "${%s_set}" ]; then BITS_ARCH_PREFIX="${%s}"; '
                   'else unset BITS_ARCH_PREFIX; fi\nunset %s %s_set\n'
                   % (saved, saved, saved, saved)))

    init = wrapped_init(architecture)

    identity = {"source": str(source.resolve()), "layout_version": 2,
                "init_sha256": hashlib.sha256(init.encode()).hexdigest()}
    marker = ".bits-reuse-symlinks.json"
    # Keep an already completed stage as-is. Earlier bits versions used the
    # package's effective arch ("share") in this wrapper; refresh only that
    # exact generated script, leaving modified private files untouched.
    if os.path.lexists(destination):
        if not destination.is_symlink() and (destination / marker).is_file():
            previous = json.loads((destination / marker).read_text())
            local_init = destination / "etc/profile.d/init.sh"
            private = all((not (destination / "etc").is_symlink(),
                           not local_init.parent.is_symlink(),
                           not local_init.is_symlink(), local_init.is_file()))
            if private:
                current = local_init.read_text()
                if previous == identity and current == init:
                    return str(destination)
                if arch != architecture:
                    legacy_init = wrapped_init(arch)
                    legacy_identity = dict(identity, init_sha256=hashlib.sha256(
                        legacy_init.encode()).hexdigest())
                    if previous == legacy_identity and current == legacy_init:
                        local_init.write_text(init)
                        (destination / marker).write_text(json.dumps(identity, sort_keys=True) + "\n")
                        return str(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".reuse-", dir=str(destination.parent)))
    try:
        scram_project = (source / ".SCRAM").is_dir()
        for entry in source.iterdir():
            target = staged / entry.name
            if entry.name == "etc" and (entry / "profile.d").exists():
                target.mkdir()
                for child in entry.iterdir():
                    copy = child.name == "scram.d" or (
                        child.name == "profile.d" and (child / "init.sh").exists())
                    if copy:
                        shutil.copytree(child, target / child.name, symlinks=True)
                    else:
                        (target / child.name).symlink_to(child)
            elif any((entry.name == "tools" and (entry / "selected").is_dir(),
                      scram_project and entry.name in (".SCRAM", "config"))):
                shutil.copytree(entry, target, symlinks=True)
            elif entry.name != marker:
                target.symlink_to(entry)

        # CMS makes copied regular files writable. Do not traverse any links.
        writable = []
        for directory, dirs, files in os.walk(staged, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
            for name in files:
                path = Path(directory) / name
                if stat.S_ISREG(path.lstat().st_mode):
                    writable.append(str(path))
        for start in range(0, len(writable), 128):
            _run_checked(["chmod", "+w"] + writable[start:start + 128])
        # Preserve bits' WORK_DIR/BITS_ARCH_PREFIX setup in its local init.sh.
        # Relocate other copied profile/SCRAM/tool files with the same rule.

        def write_init(root):
            path = root / "etc/profile.d/init.sh"
            if path.is_symlink():
                path.unlink()
            path.write_text(init)

        if scram_project:
            for name in (".SCRAM", "config"):
                _run_checked(["chmod", "-R", "+w", str(staged / name)])
            # Run at the final package location, as REFERENCE_PACKAGE_POST does.
            backup = _install_stage(staged, destination, source)
            refroot = base.parent if base.name == arch else base
            try:
                _run_checked([str(destination / "config/SCRAM/projectAreaRename.pl"),
                              str(refroot), str(runtime), architecture, str(destination)])
                # CMS performs the text substitution after projectAreaRename.
                _relocate_references(destination, replacements)
                write_init(destination)
                (destination / marker).write_text(json.dumps(identity, sort_keys=True) + "\n")
            except BaseException:
                if destination.exists() and not destination.is_symlink():
                    shutil.rmtree(destination)
                if backup is not None:
                    (backup / "package").rename(destination)
                    shutil.rmtree(backup)
                raise
            if backup is not None:
                shutil.rmtree(backup)
            return str(destination)
        _relocate_references(staged, replacements)
        write_init(staged)
        (staged / marker).write_text(json.dumps(identity, sort_keys=True) + "\n")
        backup = _install_stage(staged, destination, source)
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return str(destination)
