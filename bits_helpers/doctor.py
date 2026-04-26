#!/usr/bin/env python3
"""bits doctor — system requirement checks and runner environment validation.

In its default (recipe-check) mode ``bits doctor`` examines a package's
dependency tree and reports which packages can be satisfied by the system and
which will be built by bits.

With ``--runner`` it additionally validates the full build-runner environment:
compiler, git, Docker daemon, podman/sandbox, QEMU binfmt handlers, CVMFS
mounts, disk space, and remote store reachability.

With ``--check-store`` it resolves the dependency tree, computes the expected
tarball hash for each package bits would build, and probes the remote store to
report which packages are pre-built and which will need compilation.

Exit codes (recipe-check mode)
-------------------------------
0  All system requirements satisfied; build can proceed.
1  One or more required system packages are missing or the compiler/git is absent.
2  No valid defaults combination was found for the requested packages.
3  No valid defaults at all for the given package set.

Exit codes (--runner mode)
--------------------------
0  All checks PASS or WARN.
1  One or more checks FAIL.

Exit codes (--check-store mode)
--------------------------------
0  Always (the report is informational; missing tarballs are expected).
"""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from os.path import abspath, exists, expanduser
from typing import List, Tuple

from bits_helpers.cmd import DockerRunner, getstatusoutput
from bits_helpers.log import banner, debug, error, info, logger, success, warning
from bits_helpers.utilities import (
    getPackageList, parseDefaults, readDefaults, validateDefaults,
    effective_arch, ver_rev,
)

# ── Status constants ───────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

_COLOUR = {
    PASS: "\033[32m",   # green
    FAIL: "\033[31m",   # red
    WARN: "\033[33m",   # yellow
    SKIP: "\033[90m",   # dark grey
}
_RESET = "\033[0m"

CheckResult = Tuple[str, str, str]  # (name, status, detail)


def _colour(status: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return _COLOUR.get(status, "") + text + _RESET


# ── bits.rc helper ─────────────────────────────────────────────────────────────

def _bits_rc_value(key: str) -> str:
    """Return *key* from the first bits.rc / .bitsrc / ~/.bitsrc found, or ''."""
    import configparser
    cfg = configparser.ConfigParser()
    for path in ["bits.rc", ".bitsrc", expanduser("~/.bitsrc")]:
        if exists(path):
            cfg.read(path)
            break
    return cfg.get("bits", key, fallback="").strip()


# ── Existing helpers (unchanged) ───────────────────────────────────────────────

def prunePaths(workDir) -> None:
    for x in ["PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"]:
        if x not in os.environ:
            continue
        workDirEscaped = re.escape("%s" % workDir) + "[^:]*:?"
        os.environ[x] = re.sub(workDirEscaped, "", os.environ[x])


def checkPreferSystem(spec, cmd, homebrew_replacement, getstatusoutput_docker):
    if cmd == "false":
        debug("Package %s can only be managed via bits.", spec["package"])
        return (1, "")
    cmd = homebrew_replacement + cmd
    with tempfile.TemporaryDirectory(prefix="bits_prefer_check_%s_" % spec["package"]) as temp_dir:
        err, out = getstatusoutput_docker(cmd, cwd=temp_dir)
    if not err:
        success("Package %s will be picked up from the system.", spec["package"])
        for x in out.split("\n"):
            debug("%s: %s", spec["package"], x)
        return (err, "")
    warning("Package %s cannot be picked up from the system and will be built by bits.\n"
            "This is due to the fact the following script fails:\n\n%s\n\n"
            "with the following output:\n\n%s\n",
            spec["package"], cmd,
            "\n".join("%s: %s" % (spec["package"], x) for x in out.split("\n")))
    return (err, "")


def checkRequirements(spec, cmd, homebrew_replacement, getstatusoutput_docker):
    if cmd == "false":
        debug("Package %s is not a system requirement.", spec["package"])
        return (0, "")
    cmd = homebrew_replacement + cmd
    with tempfile.TemporaryDirectory(prefix="bits_prefer_check_%s_" % spec["package"]) as temp_dir:
        err, out = getstatusoutput_docker(cmd, cwd=temp_dir)
    if not err:
        success("Required package %s will be picked up from the system.", spec["package"])
        debug("%s", cmd)
        for x in out.split("\n"):
            debug("%s: %s", spec["package"], x)
        return (0, "")
    error("Package %s is a system requirement and cannot be found.\n"
          "This is due to the fact that the following script fails:\n\n%s\n"
          "with the following output:\n\n%s\n%s\n",
          spec["package"], cmd,
          "\n".join("%s: %s" % (spec["package"], x) for x in out.split("\n")),
          spec.get("system_requirement_missing"))
    return (err, "")


def systemInfo() -> None:
    _, out = getstatusoutput("env")
    debug("Environment:\n%s", out)
    _, out = getstatusoutput("uname -a")
    debug("uname -a: %s", out)
    _, out = getstatusoutput("mount")
    debug("Mounts:\n%s", out)
    _, out = getstatusoutput("df")
    debug("Disk free:\n%s", out)
    for f in ["/etc/lsb-release", "/etc/redhat-release", "/etc/os-release"]:
        err, out = getstatusoutput("cat " + f)
        if not err:
            debug("%s:\n%s", f, out)


# ── Runner environment checks ──────────────────────────────────────────────────

def _check_host_tool(tool: str) -> Tuple[str, str]:
    """PASS if *tool* is on PATH, FAIL otherwise."""
    err, out = getstatusoutput(["which", tool])
    if not err:
        return PASS, out.strip()
    return FAIL, "%s not found on PATH" % tool


def _check_compiler() -> Tuple[str, str]:
    """PASS if a C++ compiler is available."""
    for cxx in ("c++", "g++", "clang++"):
        err, out = getstatusoutput(["which", cxx])
        if not err:
            return PASS, out.strip()
    return FAIL, "no C++ compiler (c++, g++, clang++) found on PATH"


def _check_docker_daemon() -> Tuple[str, str]:
    """PASS if the Docker daemon is running and reachable."""
    err, out = getstatusoutput("docker info --format '{{.ServerVersion}}'")
    if not err and out.strip():
        return PASS, "Docker daemon running (server version %s)" % out.strip()
    if err:
        return FAIL, "docker info failed — daemon not running or not installed"
    return FAIL, "docker info returned unexpected output: %s" % out[:120]


def _check_podman() -> Tuple[str, str]:
    """WARN if podman is missing (sandbox degrades to off); PASS if working."""
    err_which, _ = getstatusoutput(["which", "podman"])
    if err_which:
        return WARN, "podman not found; --sandbox=auto will degrade to 'off'"
    # Confirm user namespaces work by running a trivial container.
    err_run, out_run = getstatusoutput("podman run --rm --quiet busybox true")
    if not err_run:
        return PASS, "podman functional; sandbox mode available"
    # Distinguish 'image not found' (pull needed) from permission problems.
    if "permission" in out_run.lower() or "unshare" in out_run.lower():
        return FAIL, "podman found but user namespaces appear disabled: %s" % out_run[:200]
    # Image not cached — that is fine; sandbox can still work.
    return WARN, "podman found but test container failed (image may need pulling): %s" % out_run[:120]


def _check_qemu_binfmt(arch: str) -> Tuple[str, str]:
    """Check whether a QEMU binfmt handler is registered for *arch*.

    *arch* is a bits architecture string such as ``slc9_aarch64``.  We derive
    the QEMU interpreter name from it (e.g. ``qemu-aarch64``) and look for a
    matching entry under ``/proc/sys/fs/binfmt_misc/``.
    """
    _BITS_TO_QEMU = {
        "aarch64": "qemu-aarch64",
        "arm64":   "qemu-aarch64",
        "ppc64le": "qemu-ppc64le",
        "s390x":   "qemu-s390x",
        "riscv64": "qemu-riscv64",
    }
    # Derive the QEMU interpreter name from the bits arch string.
    qemu_name = None
    for key, val in _BITS_TO_QEMU.items():
        if key in arch:
            qemu_name = val
            break

    if qemu_name is None:
        return SKIP, "no QEMU handler needed for %s (native or unsupported)" % arch

    binfmt_dir = "/proc/sys/fs/binfmt_misc"
    if not os.path.isdir(binfmt_dir):
        return SKIP, "/proc/sys/fs/binfmt_misc not present (not Linux or not mounted)"

    handler_path = os.path.join(binfmt_dir, qemu_name)
    if os.path.exists(handler_path):
        try:
            with open(handler_path) as fh:
                content = fh.read()
            if "enabled" in content:
                return PASS, "%s binfmt handler registered and enabled" % qemu_name
            return WARN, "%s binfmt handler registered but disabled" % qemu_name
        except OSError:
            pass
    return FAIL, (
        "%s binfmt handler not registered — cross-compilation for %s will not work.\n"
        "  Register with: docker run --rm --privileged multiarch/qemu-user-static --reset -p yes" % (qemu_name, arch)
    )


def _check_cvmfs_repo(repo_path: str) -> Tuple[str, str]:
    """PASS if *repo_path* exists and contains at least one entry."""
    if not os.path.isdir(repo_path):
        return FAIL, "directory does not exist: %s" % repo_path
    try:
        entries = os.listdir(repo_path)
    except PermissionError as exc:
        return FAIL, "cannot list %s: %s" % (repo_path, exc)
    if entries:
        return PASS, "%s accessible (%d entries)" % (repo_path, len(entries))
    return WARN, "%s is mounted but appears empty" % repo_path


def _check_disk_space(work_dir: str, min_free_gib: float = 10.0) -> Tuple[str, str]:
    """WARN if free space in *work_dir* is below *min_free_gib* GiB."""
    try:
        target = work_dir if os.path.isdir(work_dir) else os.path.dirname(os.path.abspath(work_dir))
        usage = shutil.disk_usage(target)
        free_gib  = usage.free  / (1024 ** 3)
        total_gib = usage.total / (1024 ** 3)
        detail = "%.1f GiB free of %.1f GiB on %s" % (free_gib, total_gib, target)
        if free_gib >= min_free_gib:
            return PASS, detail
        return WARN, "low disk space — %s (minimum recommended: %.0f GiB)" % (detail, min_free_gib)
    except Exception as exc:
        return WARN, "cannot determine disk usage for %s: %s" % (work_dir, exc)


def _check_store(url: str, insecure: bool = False) -> Tuple[str, str]:
    """Check remote-store reachability.

    * ``https://`` → HTTP HEAD request.
    * ``s3://``    → check ``~/.s3cfg`` presence.
    * ``b3://``    → check ``AWS_ACCESS_KEY_ID`` env var.
    * ``rsync://`` → SKIP (network check not practical without credentials).
    * empty        → SKIP.
    """
    if not url:
        return SKIP, "no remote store configured"

    if url.startswith("https://") or url.startswith("http://"):
        import urllib.request
        import urllib.error
        ctx = None
        if insecure:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                return PASS, "HTTP %d  %s" % (resp.status, url)
        except urllib.error.HTTPError as exc:
            # 403/404 on HEAD is common for object-store buckets — they are
            # reachable but the root URL may require auth.  Treat as WARN.
            if exc.code in (403, 404):
                return WARN, "HTTP %d (store reachable but root listing denied): %s" % (exc.code, url)
            return FAIL, "HTTP %d: %s" % (exc.code, url)
        except Exception as exc:
            return FAIL, "cannot reach store %s: %s" % (url, exc)

    if url.startswith("s3://"):
        s3cfg = expanduser("~/.s3cfg")
        if os.path.isfile(s3cfg):
            return PASS, "~/.s3cfg present for s3:// store %s" % url
        return WARN, "s3:// store configured but ~/.s3cfg not found: %s" % url

    if url.startswith("b3://"):
        key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        if key:
            return PASS, "AWS_ACCESS_KEY_ID set for b3:// store %s" % url
        return WARN, "b3:// store configured but AWS_ACCESS_KEY_ID not set: %s" % url

    if url.startswith("rsync://"):
        return SKIP, "rsync:// store connectivity check not supported; assumed reachable: %s" % url

    return SKIP, "unrecognised store scheme; skipping connectivity check: %s" % url


# ── Store tarball probe (--check-store mode) ───────────────────────────────────

def _probe_tarball_in_store(spec: dict, arch: str, store_url: str,
                             insecure: bool = False) -> Tuple[str, str]:
    """Probe whether a pre-built tarball for *spec* is available in the remote store.

    Supports:
    * ``https://`` / ``http://`` — HTTP HEAD request for the tarball path.
    * Local directory path starting with ``/``  — ``os.path.isfile`` check.
    * Everything else (rsync://, s3://, b3://) — SKIP (credentials needed).

    Returns ``(status, detail)``.
    """
    remote_hashes = spec.get("remote_hashes") or []
    if not remote_hashes:
        return WARN, "hash not computed for %s (commit ref unknown; re-run with --fetch-repos)" % spec["package"]

    pkg_arch = effective_arch(spec, arch)
    pkg      = spec["package"]
    vr       = ver_rev(spec)
    tarball  = "%s-%s.%s.tar.gz" % (pkg, vr, pkg_arch)

    for h in remote_hashes:
        store_path = "TARS/%s/store/%s/%s/%s" % (pkg_arch, h[:2], h, tarball)

        if store_url.startswith("http"):
            import urllib.request
            import urllib.error
            import ssl
            url = "%s/%s" % (store_url.rstrip("/"), store_path)
            ctx = None
            if insecure:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    if resp.status < 400:
                        return PASS, "available: %s  (hash %s)" % (tarball, h[:16])
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    continue   # not found at this hash — try next
                return WARN, "HTTP %d probing store for %s: %s" % (exc.code, pkg, url[:80])
            except Exception as exc:
                return WARN, "store probe failed for %s: %s" % (pkg, exc)

        elif store_url.startswith("/"):
            full_path = os.path.join(store_url, store_path)
            if os.path.isfile(full_path):
                return PASS, "available: %s  (hash %s)" % (tarball, h[:16])

        else:
            return SKIP, "store scheme not probeable without credentials — %s" % store_url[:60]

    return FAIL, "not in store — will build from source: %s" % tarball


def _run_check_store_checks(args, specs: dict, own: set,
                             always_built: set) -> List[CheckResult]:
    """Probe the remote store for each package bits would build.

    Populates ``commit_hash`` (best-effort: tag string for tagged releases,
    "0" for branch builds without ``--fetch-repos``) then calls ``storeHashes``
    in topological order before probing each target package.

    Returns ``[(name, status, detail), ...]``.
    """
    # Lazy import — bits_helpers.build is heavy (jinja2, analytics, slow init).
    from bits_helpers.build import storeHashes as _storeHashes

    store_url = (getattr(args, "remoteStore", "") or "").rstrip("/")
    arch      = getattr(args, "architecture", "")
    insecure  = getattr(args, "insecure", False)

    if not store_url:
        return [("remote store", SKIP,
                 "no --remote-store configured; cannot check store availability")]

    # Populate commit_hash for each spec that lacks one.
    # For tagged releases this is exact; for branch builds it is approximate
    # (use --fetch-repos in 'bits status --check-store' for accurate hashes).
    hash_approx = False
    for pkg, spec in specs.items():
        if "commit_hash" not in spec:
            tag = spec.get("tag", "")
            if tag:
                spec["commit_hash"] = tag
            else:
                spec["commit_hash"] = "0"
                hash_approx = True

    # Compute all hashes in topological order (specs is insertion-ordered,
    # dependencies before dependents, as returned by getPackageList).
    for pkg in list(specs.keys()):
        try:
            _storeHashes(pkg, specs, considerRelocation=False)
        except Exception:
            pass  # leave spec without remote_hashes; probe will WARN

    targets: List[CheckResult] = []
    for pkg in specs:  # topological order — preserves readability in output
        if pkg not in own and pkg not in always_built:
            continue
        spec   = specs[pkg]
        status, detail = _probe_tarball_in_store(spec, arch, store_url, insecure)
        targets.append((pkg, status, detail))

    if not targets:
        return [("(nothing to build)", SKIP,
                 "all packages satisfied from the system — nothing to look up in the store")]

    if hash_approx:
        targets.insert(0, (
            "(note)", WARN,
            "Some commit hashes are approximate (branch builds without "
            "--fetch-repos). Re-run 'bits status --fetch-repos --check-store' "
            "for exact results.",
        ))

    return targets


# ── check-store output emitters ────────────────────────────────────────────────

def _emit_check_store_text(checks: List[CheckResult], arch: str,
                            store_url: str) -> None:
    from bits_helpers.log import banner as _banner
    _banner("bits doctor --check-store  —  architecture: %s", arch)
    print("  Store: %s\n" % store_url)
    print("  %-36s %-6s  %s" % ("package", "status", "detail"))
    print("  " + "-" * 78)
    for name, status, detail in checks:
        first_line, *rest = detail.split("\n")
        label = _colour(status, "%-6s" % status)
        print("  %-36s %s  %s" % (name[:36], label, first_line))
        for extra in rest:
            if extra.strip():
                print("  %-36s         %s" % ("", extra))
    print()
    n_pass = sum(1 for _, s, _ in checks if s == PASS)
    n_fail = sum(1 for _, s, _ in checks if s == FAIL)
    n_skip = sum(1 for _, s, _ in checks if s == SKIP)
    n_shown = len(checks) - n_skip
    print("  %d of %d package(s) available in store; %d will build from source." % (
        n_pass, n_shown, n_fail))


def _emit_check_store_json(checks: List[CheckResult], arch: str,
                            store_url: str) -> None:
    report = {
        "mode":         "check-store",
        "architecture": arch,
        "store":        store_url,
        "packages": [
            {"package": name, "status": status, "detail": detail}
            for name, status, detail in checks
            if name != "(note)"
        ],
        "summary": {
            PASS: sum(1 for n, s, _ in checks if s == PASS and n != "(note)"),
            FAIL: sum(1 for n, s, _ in checks if s == FAIL and n != "(note)"),
            WARN: sum(1 for n, s, _ in checks if s == WARN and n != "(note)"),
            SKIP: sum(1 for n, s, _ in checks if s == SKIP and n != "(note)"),
        },
        "notes": [d for n, _, d in checks if n == "(note)"],
    }
    print(json.dumps(report, indent=2))


# ── Runner check orchestration ─────────────────────────────────────────────────


def _check_prepub_health(url: str, insecure: bool = False) -> Tuple[str, str]:
    """Probe ``GET <url>/api/v1/health`` and verify the response contains ``"status":"ok"``.

    A PASS means cvmfs-prepub is reachable and healthy.
    A WARN means the endpoint was reachable but returned an unexpected status.
    A FAIL means the endpoint was not reachable (connection refused, DNS, etc.).
    """
    import json
    import urllib.error
    import urllib.request

    health_url = url.rstrip("/") + "/api/v1/health"
    ctx = None
    if insecure:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

    try:
        req  = urllib.request.Request(health_url, method="GET")
        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        body = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            return WARN, "health endpoint reachable but returned non-JSON: %s" % body[:120]
        status = data.get("status", "")
        if status == "ok":
            version = data.get("version", "")
            return PASS, "healthy%s  (%s)" % (("  v" + version) if version else "", health_url)
        return WARN, "health endpoint returned status=%r (expected 'ok')  (%s)" % (status, health_url)
    except urllib.error.URLError as exc:
        return FAIL, "cannot reach %s: %s" % (health_url, exc.reason)
    except Exception as exc:
        return FAIL, "error checking %s: %s" % (health_url, exc)


def _run_runner_checks(args) -> List[CheckResult]:
    """Run all environment checks and return a list of (name, status, detail)."""
    checks: List[CheckResult] = []

    # ── Basic tools ─────────────────────────────────────────────────────────
    git_status, git_detail = _check_host_tool("git")
    checks.append(("git", git_status, git_detail))

    cxx_status, cxx_detail = _check_compiler()
    checks.append(("C++ compiler", cxx_status, cxx_detail))

    # ── Docker daemon ────────────────────────────────────────────────────────
    if getattr(args, "docker", False) or getattr(args, "runner", False):
        docker_status, docker_detail = _check_docker_daemon()
        checks.append(("docker daemon", docker_status, docker_detail))

        # ── QEMU binfmt for cross-compilation ────────────────────────────
        arch = getattr(args, "architecture", "")
        if arch:
            qemu_status, qemu_detail = _check_qemu_binfmt(arch)
            checks.append(("QEMU binfmt (%s)" % arch, qemu_status, qemu_detail))

    # ── Podman / sandbox ─────────────────────────────────────────────────────
    podman_status, podman_detail = _check_podman()
    checks.append(("podman (sandbox)", podman_status, podman_detail))

    # ── CVMFS repos ──────────────────────────────────────────────────────────
    cvmfs_repos = list(getattr(args, "cvmfsRepos", None) or [])
    if not cvmfs_repos:
        # Fall back to bits.rc cvmfs_repos (comma-separated paths)
        rc_repos = _bits_rc_value("cvmfs_repos")
        if rc_repos:
            cvmfs_repos = [r.strip() for r in rc_repos.split(",") if r.strip()]
    for repo_path in cvmfs_repos:
        cvmfs_status, cvmfs_detail = _check_cvmfs_repo(repo_path)
        checks.append(("CVMFS %s" % repo_path, cvmfs_status, cvmfs_detail))

    # ── Disk space ───────────────────────────────────────────────────────────
    min_disk = float(getattr(args, "minDisk", None) or 10.0)
    work_dir = getattr(args, "workDir", "sw") or "sw"
    disk_status, disk_detail = _check_disk_space(work_dir, min_disk)
    checks.append(("disk space (%s)" % work_dir, disk_status, disk_detail))

    # ── Remote store ─────────────────────────────────────────────────────────
    store_url = (getattr(args, "remoteStore", "") or "").rstrip("/")
    insecure   = getattr(args, "insecure", False)
    store_status, store_detail = _check_store(store_url, insecure)
    checks.append(("remote store", store_status, store_detail))

    # ── cvmfs-prepub service (optional) ──────────────────────────────────────
    prepub_url = (getattr(args, "prepubUrl", "") or "").rstrip("/")
    if prepub_url:
        prepub_status, prepub_detail = _check_prepub_health(prepub_url, insecure)
        checks.append(("cvmfs-prepub service", prepub_status, prepub_detail))

    return checks


# ── Output emitters ────────────────────────────────────────────────────────────

def _emit_runner_text(checks: List[CheckResult], arch: str) -> None:
    from bits_helpers.log import banner as _banner
    _banner("bits doctor --runner  —  architecture: %s", arch)
    print()
    print("  %-32s %-6s  %s" % ("check", "status", "detail"))
    print("  " + "-" * 76)
    for name, status, detail in checks:
        first_line, *rest = detail.split("\n")
        label = _colour(status, "%-6s" % status)
        print("  %-32s %s  %s" % (name[:32], label, first_line))
        for extra in rest:
            if extra.strip():
                print("  %-32s         %s" % ("", extra))
    print()
    summary = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for _, st, _ in checks:
        summary[st] = summary.get(st, 0) + 1
    total = sum(summary.values())
    print("  Summary: %s PASS  %s FAIL  %s WARN  %s SKIP  (of %d total)" % (
        _colour(PASS, str(summary[PASS])),
        _colour(FAIL, str(summary[FAIL])),
        _colour(WARN, str(summary[WARN])),
        _colour(SKIP, str(summary[SKIP])),
        total,
    ))


def _emit_runner_json(checks: List[CheckResult], arch: str, exit_code: int) -> None:
    report = {
        "mode":         "runner",
        "architecture": arch,
        "checks": [
            {"name": name, "status": status, "detail": detail}
            for name, status, detail in checks
        ],
        "summary": {
            PASS: sum(1 for _, s, _ in checks if s == PASS),
            FAIL: sum(1 for _, s, _ in checks if s == FAIL),
            WARN: sum(1 for _, s, _ in checks if s == WARN),
            SKIP: sum(1 for _, s, _ in checks if s == SKIP),
        },
        "exit_code": exit_code,
    }
    print(json.dumps(report, indent=2))


# ── Main entry point ───────────────────────────────────────────────────────────

def doDoctor(args, parser):
    # ── --runner mode: full environment checklist ────────────────────────────
    if getattr(args, "runner", False):
        arch = getattr(args, "architecture", "")
        checks = _run_runner_checks(args)
        n_fail = sum(1 for _, s, _ in checks if s == FAIL)
        exit_code = 1 if n_fail else 0
        if getattr(args, "json_output", False):
            _emit_runner_json(checks, arch, exit_code)
        else:
            _emit_runner_text(checks, arch)
        sys.exit(exit_code)

    # ── Standard recipe-check mode ───────────────────────────────────────────
    if not exists(args.configDir):
        parser.error("Wrong path to alidist specified: %s" % args.configDir)

    prunePaths(abspath(args.workDir))

    if exists(expanduser("~/.rootlogon.C")):
        warning("You have a ~/.rootlogon.C — this might interfere with your "
                "environment in hidden ways.\nPlease review it and make sure "
                "you are not force-loading any library.")

    # ── Prerequisite URL: read from bits.rc so each community can customise ──
    _prereq_url  = _bits_rc_value("prerequisites_url") or \
                   "https://alice-doc.github.io/alice-analysis-tutorial/building/"
    _prereq_hint = "Please consult the prerequisites guide:\n  %s" % _prereq_url

    _config_dir_abs = os.path.abspath(args.configDir)
    _docker_volumes = ["%s:/alidist.bits:ro" % _config_dir_abs] if args.docker else []
    extra_env = {"BITS_CONFIG_DIR": "/alidist.bits" if args.docker else _config_dir_abs}
    extra_env.update(dict([e.partition("=")[::2] for e in args.environment]))

    exitcode = 0

    # ── Single DockerRunner keeps the container alive for all checks ─────────
    with DockerRunner(args.dockerImage, args.docker_extra_args,
                      extra_env=extra_env, extra_volumes=_docker_volumes) as getstatusoutput_docker:

        # Compiler check (inside the runner so Docker-based checks work too)
        err_cxx, out_cxx = getstatusoutput_docker("type c++")
        if err_cxx:
            exitcode = 1
            warning("Unable to find system compiler.\n%s\n%s", out_cxx, _prereq_hint)

        # git check (always on the host)
        err_git, out_git = getstatusoutput("type git")
        if err_git:
            exitcode = 1
            error("Unable to find git.\n%s\n%s", out_git, _prereq_hint)

        # Homebrew shim (macOS)
        homebrew_replacement = ""
        err_brew, _ = getstatusoutput("which brew")
        if err_brew:
            homebrew_replacement = "brew() { true; }; "

        logger.setLevel(logging.BANNER)
        if args.debug:
            logger.setLevel(logging.DEBUG)

        packages = []
        for p in args.packages:
            recipe_path = "%s/%s.sh" % (args.configDir, p.lower())
            if not exists(recipe_path):
                error("Cannot find recipe %s for package %s.", recipe_path, p)
                exitcode = 1
                continue
            packages.append(p)
        systemInfo()

        specs = {}
        defaultsReader = lambda: readDefaults(args.configDir, args.defaults, parser.error, args.architecture)
        (err_def, overrides, taps, _defaultsMeta) = parseDefaults(args.disable, defaultsReader, info)
        if err_def:
            error("%s", err_def)
            sys.exit(1)

        def performValidateDefaults(spec):
            (ok, msg, valid) = validateDefaults(spec, args.defaults)
            if not ok:
                error("%s", msg)
            return (ok, msg, valid)

        fromSystem, own, failed, validDefaults = \
            getPackageList(packages                = packages,
                           specs                   = specs,
                           configDir               = args.configDir,
                           preferSystem            = args.preferSystem,
                           noSystem                = args.noSystem,
                           architecture            = args.architecture,
                           disable                 = args.disable,
                           defaults                = args.defaults,
                           performPreferCheck      = lambda pkg, cmd: checkPreferSystem(pkg, cmd, homebrew_replacement, getstatusoutput_docker),
                           performRequirementCheck = lambda pkg, cmd: checkRequirements(pkg, cmd, homebrew_replacement, getstatusoutput_docker),
                           performValidateDefaults = performValidateDefaults,
                           overrides               = overrides,
                           taps                    = taps,
                           log                     = info)

    alwaysBuilt = {x for x in specs} - fromSystem - own - failed

    # ── --check-store mode: probe remote store for each package bits would build ─
    if getattr(args, "checkStore", False):
        store_url = (getattr(args, "remoteStore", "") or "").rstrip("/")
        store_checks = _run_check_store_checks(args, specs, own, alwaysBuilt)
        if getattr(args, "json_output", False):
            _emit_check_store_json(store_checks, args.architecture, store_url)
        else:
            _emit_check_store_text(store_checks, args.architecture, store_url)
        sys.exit(0)

    # ── Standard recipe-check output ────────────────────────────────────────────
    if alwaysBuilt:
        banner("The following packages will be built by bits because\n"
               " usage of a system version of it is not allowed or supported, by policy:\n\n- %s",
               " \n- ".join(alwaysBuilt))
    if fromSystem:
        banner("The following packages will be picked up from the system:\n\n- %s\n\n"
               "If this is not what you want, you have to uninstall / unload them.",
               "\n- ".join(fromSystem))
    if own:
        banner("The following packages will be built by bits because they couldn't be picked up from the system:\n\n"
               "- %s\n\n"
               "This is not a real issue, but it might take longer the first time you invoke bits.\n"
               "Look at the error messages above to get hints on what packages you need to install separately.",
               "\n- ".join(own))
    if failed:
        banner("The following packages are system dependencies and could not be found:\n\n- %s\n\n"
               "Look at the error messages above to get hints on what packages you need to install separately.",
               "\n- ".join(failed))
        exitcode = 1
    if validDefaults and any(d not in validDefaults for d in args.defaults):
        banner("The list of packages cannot be built with the defaults you have specified.\n"
               "List of valid defaults:\n\n- %s\n\n"
               "Use the `--defaults' switch to specify one of them.",
               "\n- ".join(validDefaults))
        exitcode = 2
    if validDefaults is None:
        banner("No valid defaults combination was found for the given list of packages, check your recipes!")
        exitcode = 3
    if exitcode:
        error("There were errors: build cannot be performed if they are not resolved. Check the messages above.")
    sys.exit(exitcode)
