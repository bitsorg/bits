# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits verify — check that a live deployment matches a build manifest.

Usage::

    bits verify --from-manifest FILE [--cvmfs-root PATH] [--work-dir DIR] [--json]

For each package entry in the manifest the command:

1. Locates the content-addressed tarball under the configured search roots
   (``--cvmfs-root`` and/or ``--work-dir``).
2. Computes its SHA-256 and compares it to the ``tarball_sha256`` recorded in
   the manifest.
3. Packages whose ``outcome`` was ``already_installed`` and that carry no
   tarball are reported as SKIP — no output tarball is expected for them.

For each provider entry the command checks that the local checkout's current
HEAD commit matches the commit recorded in the manifest.  Provider checks are
silently skipped when the checkout directory does not exist locally (the
command is typically run on a worker node or analysis machine that does not
have the recipe repositories checked out).

Exit codes
----------
0  All verifiable entries match — the deployment is consistent with the manifest.
1  One or more entries are FAIL (hash mismatch or provider commit mismatch).
2  One or more entries are MISS (tarball not found; consistency unknown).
3  The manifest file cannot be read or is malformed.
"""

import json
import os
import subprocess
import sys
from typing import List, Optional, Tuple

from bits_helpers.checksum import checksum_file
from bits_helpers.log import banner, error


# ── Constants ─────────────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
MISS = "MISS"
SKIP = "SKIP"

_COLOUR = {
    PASS: "\033[32m",   # green
    FAIL: "\033[31m",   # red
    MISS: "\033[33m",   # yellow
    SKIP: "\033[90m",   # dark grey
}
_RESET = "\033[0m"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _store_rel(pkg_hash: str, tarball: str, arch: str) -> str:
    """Return the store-relative path for a content-addressed tarball."""
    return os.path.join("TARS", arch, "store", pkg_hash[:2], pkg_hash, tarball)


def _find_tarball(tarball: str, pkg_hash: str, arch: str,
                  search_roots: List[str]) -> Optional[str]:
    """Return the absolute path of *tarball* if found in any search root, else None."""
    rel = _store_rel(pkg_hash, tarball, arch)
    for root in search_roots:
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def _git_head(directory: str) -> Optional[str]:
    """Return the full HEAD commit SHA of *directory*, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.decode(errors="replace").strip() or None
    except Exception:
        pass
    return None


def _colour(status: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return _COLOUR.get(status, "") + text + _RESET


# ── Per-entry verification ────────────────────────────────────────────────────

def verify_package(entry: dict, arch: str,
                   search_roots: List[str]) -> Tuple[str, str]:
    """Return ``(status, detail)`` for a single PackageEntry dict.

    Parameters
    ----------
    entry:
        A dict corresponding to one ``packages[]`` element from the manifest.
    arch:
        The ``architecture`` field from the manifest top level.
    search_roots:
        Ordered list of filesystem roots to search for the tarball store.
    """
    tarball = entry.get("tarball")
    expected_sha = entry.get("tarball_sha256")
    pkg_hash = entry.get("hash", "")
    outcome = entry.get("outcome", "")

    if not tarball or not expected_sha:
        if outcome == "already_installed":
            return SKIP, "already_installed — no output tarball expected"
        return SKIP, "no tarball recorded in manifest"

    path = _find_tarball(tarball, pkg_hash, arch, search_roots)
    if path is None:
        rel = _store_rel(pkg_hash, tarball, arch)
        searched = "; ".join(os.path.join(r, rel) for r in search_roots)
        return MISS, "tarball not found\n        searched: %s" % searched

    try:
        actual_sha = checksum_file(path)
    except Exception as exc:
        return FAIL, "cannot read tarball: %s" % exc

    if actual_sha == expected_sha:
        return PASS, "sha256 OK"
    return FAIL, (
        "sha256 mismatch\n"
        "        expected: %s\n"
        "        actual:   %s" % (expected_sha, actual_sha)
    )


def verify_provider(entry: dict) -> Tuple[str, str]:
    """Return ``(status, detail)`` for a single ProviderEntry dict."""
    checkout = entry.get("checkout_dir", "")
    expected_commit = entry.get("commit", "")

    if not checkout or not os.path.isdir(checkout):
        return SKIP, "checkout not present locally"

    actual_commit = _git_head(checkout)
    if actual_commit is None:
        return MISS, "cannot read HEAD from %s" % checkout

    if actual_commit == expected_commit:
        return PASS, "commit OK  (%s)" % actual_commit[:12]

    return FAIL, (
        "commit mismatch\n"
        "        manifest: %s\n"
        "        actual:   %s" % (expected_commit[:12], actual_commit[:12])
    )


# ── Output formatting ─────────────────────────────────────────────────────────

def _print_pkg_row(status: str, name: str, ver_rev: str, detail: str) -> None:
    label = _colour(status, "%-4s" % status)
    print("    %s  %-30s %-18s %s" % (label, name[:30], ver_rev[:18], detail))


def _print_prov_row(status: str, name: str, detail: str) -> None:
    label = _colour(status, "%-4s" % status)
    print("    %s  %-30s %s" % (label, name[:30], detail))


# ── Main entry point ──────────────────────────────────────────────────────────

def doVerify(args, parser) -> None:  # noqa: N802
    """Verify a live deployment against a build manifest."""
    from bits_helpers.utilities import detectArch

    manifest_path = args.fromManifest
    if not os.path.isfile(manifest_path):
        error("Manifest not found: %s", manifest_path)
        sys.exit(3)

    try:
        with open(manifest_path) as fh:
            manifest = json.load(fh)
    except (ValueError, OSError) as exc:
        error("Cannot read manifest %s: %s", manifest_path, exc)
        sys.exit(3)

    arch = manifest.get("architecture", "")
    packages = manifest.get("packages", [])
    providers = manifest.get("providers", [])

    # Build ordered search-root list: CVMFS mount first, then local work dir.
    search_roots: List[str] = []
    cvmfs_root = getattr(args, "cvmfsRoot", None)
    if cvmfs_root:
        search_roots.append(cvmfs_root)
    work_dir = getattr(args, "workDir", None) or "sw"
    search_roots.append(work_dir)

    skip_providers = getattr(args, "noProviders", False)
    use_json = getattr(args, "json_output", False)

    counts = {PASS: 0, FAIL: 0, MISS: 0, SKIP: 0}

    # ── Architecture check ────────────────────────────────────────────────────
    host_arch = detectArch()
    arch_match = (arch == host_arch)
    arch_status = PASS if arch_match else FAIL
    if not arch_match:
        counts[FAIL] += 1

    # ── Collect results ───────────────────────────────────────────────────────
    pkg_results = []
    for entry in packages:
        status, detail = verify_package(entry, arch, search_roots)
        counts[status] += 1
        pkg_results.append((entry, status, detail))

    prov_results = []
    if not skip_providers:
        for entry in providers:
            status, detail = verify_provider(entry)
            counts[status] += 1
            prov_results.append((entry, status, detail))

    exit_code = 1 if counts[FAIL] else (2 if counts[MISS] else 0)

    # ── Emit output ───────────────────────────────────────────────────────────
    if use_json:
        _emit_json(manifest, arch, host_arch, arch_status,
                   pkg_results, prov_results, counts, exit_code)
    else:
        _emit_text(manifest_path, manifest, arch, host_arch, arch_status,
                   pkg_results, prov_results, counts, skip_providers)

    sys.exit(exit_code)


def _emit_text(manifest_path, manifest, arch, host_arch, arch_status,
               pkg_results, prov_results, counts, skip_providers) -> None:
    banner("bits verify  —  %s", os.path.basename(manifest_path))
    print("  File:       %s" % manifest_path)
    print("  Schema:     v%s" % manifest.get("schema_version", "?"))
    print("  Created:    %s" % manifest.get("created_at", "?"))
    print("  Build:      %s" % manifest.get("status", "?"))
    print()

    # Architecture line
    if arch_status == PASS:
        print("  Architecture: %s  %s" % (_colour(PASS, PASS), arch))
    else:
        print("  Architecture: %s  manifest=%s  host=%s" % (
            _colour(FAIL, FAIL), arch, host_arch))
    print()

    # Packages
    print("  Packages (%d):" % len(pkg_results))
    print("    %-4s  %-30s %-18s %s" % ("", "package", "version-revision", "detail"))
    print("    " + "-" * 74)
    for entry, status, detail in pkg_results:
        name = entry.get("package", "?")
        ver  = "%s-%s" % (entry.get("version", "?"), entry.get("revision", "?"))
        _print_pkg_row(status, name, ver, detail)
    print()

    # Providers
    if not skip_providers and prov_results:
        print("  Providers (%d):" % len(prov_results))
        print("    %-4s  %-30s %s" % ("", "name", "detail"))
        print("    " + "-" * 74)
        for entry, status, detail in prov_results:
            name = entry.get("name", "?")
            _print_prov_row(status, name, detail)
        print()

    # Summary
    total = sum(counts.values())
    print("  Summary: %s PASS  %s FAIL  %s MISS  %s SKIP  (of %d total)" % (
        _colour(PASS, str(counts[PASS])),
        _colour(FAIL, str(counts[FAIL])),
        _colour(MISS, str(counts[MISS])),
        _colour(SKIP, str(counts[SKIP])),
        total,
    ))


def _emit_json(manifest, arch, host_arch, arch_status,
               pkg_results, prov_results, counts, exit_code) -> None:
    report = {
        "manifest_created_at": manifest.get("created_at", ""),
        "manifest_status":     manifest.get("status", ""),
        "schema_version":      manifest.get("schema_version"),
        "architecture": {
            "manifest": arch,
            "host":     host_arch,
            "status":   arch_status,
        },
        "packages": [
            {
                "package":  e.get("package"),
                "version":  e.get("version"),
                "revision": e.get("revision"),
                "status":   st,
                "detail":   dt,
            }
            for e, st, dt in pkg_results
        ],
        "providers": [
            {
                "name":   e.get("name"),
                "status": st,
                "detail": dt,
            }
            for e, st, dt in prov_results
        ],
        "summary":   dict(counts),
        "exit_code": exit_code,
    }
    print(json.dumps(report, indent=2))
