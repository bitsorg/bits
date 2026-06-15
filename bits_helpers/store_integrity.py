# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local integrity ledger for build-product tarballs.

Threat model
------------
A malicious actor with write access to the remote store backend (S3, rsync
server, HTTP proxy, etc.) could silently replace a legitimate build product
with a trojanised tarball.  Because the build system unpacks and runs the
tarball's content directly, such a replacement would result in arbitrary code
execution on every developer and CI machine that subsequently builds against
that package.

Mitigation
----------
Immediately after each successful upload, the SHA-256 digest of the local
tarball is written to a **local-only ledger** that lives entirely within the
work directory::

    $WORK_DIR/
      STORE_CHECKSUMS/
        TARS/
          {architecture}/
            store/
              {hash[:2]}/
                {hash}/
                  {tarball}.sha256   ← one file per tarball

The path mirrors the remote store structure so that the ledger entry for a
tarball is trivially derivable from its spec, without any database or index.

When the tarball is later recalled from the remote store, its SHA-256 is
recomputed and compared against the ledger.  Three outcomes are possible:

**Match**
    The file is intact.  The build continues normally.

**Missing ledger entry**
    The tarball was uploaded before ledger recording was deployed, or the work
    directory was wiped.  A warning is emitted and the current digest is
    recorded so that subsequent recalls are verified.

**Mismatch**
    The tarball has been altered since it was uploaded.  This is a fatal error:
    the build is aborted with a clear message indicating potential tampering.

The ledger lives on the *local* filesystem and is **never** uploaded to the
remote store, so it cannot be forged through the same vector that it protects
against.  Operators who share a work directory via NFS or a distributed FS
benefit from the same protection as long as the shared volume is not itself
compromised.
"""

import os

from bits_helpers.checksum import checksum_file
from bits_helpers.log import debug, warning, error
from bits_helpers.utilities import resolve_store_path, effective_arch, ver_rev

# Sub-directory inside $WORK_DIR that holds all ledger files.
# Kept separate from TARS/ so that it is clearly local-only and is not
# accidentally swept into an rsync upload of the TARS/ tree.
LEDGER_SUBDIR = "STORE_CHECKSUMS"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _tarball_name(spec: dict, arch: str) -> str:
    """Return the tarball filename for *spec* on *arch*."""
    return "{}-{}.{}.tar.gz".format(spec["package"], ver_rev(spec), arch)


def _ledger_path(work_dir: str, arch: str, pkg_hash: str, tarball: str) -> str:
    """Return the absolute path to the ledger file for *tarball*.

    Example::

        /path/to/sw/STORE_CHECKSUMS/TARS/slc7_x86-64/store/ab/abcd1234.../
            MyPkg-1.0-1.slc7_x86-64.tar.gz.sha256
    """
    store_rel = resolve_store_path(arch, pkg_hash)
    return os.path.join(work_dir, LEDGER_SUBDIR, store_rel, tarball + ".sha256")


def _write_ledger(ledger: str, digest: str) -> None:
    """Atomically write *digest* to *ledger*, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    # Write to a sibling temp file then rename for atomicity, so that a
    # concurrent reader never sees a half-written digest.
    tmp = ledger + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(digest + "\n")
    os.replace(tmp, ledger)


# ── Public API ────────────────────────────────────────────────────────────────

def record_tarball_checksum(spec: dict, work_dir: str, build_arch: str) -> None:
    """Compute and record the SHA-256 digest of the local tarball after upload.

    Call this immediately after a successful
    ``syncHelper.upload_symlinks_and_tarball(spec)`` so that the digest is
    captured before the tarball could be modified externally.

    The function is a no-op when:

    * the local tarball file does not exist (e.g. the upload backend keeps
      only the remote copy and did not write a local file); or
    * a ledger entry already exists with exactly the same digest (idempotent
      on repeated uploads of the same hash).

    A pre-existing ledger entry with a *different* digest is treated as a
    warning — it could mean a hash collision (essentially impossible with
    SHA-256) or a bug in the build hash computation.  The new digest wins.
    """
    arch = effective_arch(spec, build_arch)
    tarball = _tarball_name(spec, arch)
    store_rel = resolve_store_path(arch, spec["hash"])
    local_tar = os.path.join(work_dir, store_rel, tarball)
    ledger = _ledger_path(work_dir, arch, spec["hash"], tarball)

    if not os.path.isfile(local_tar):
        debug(
            "store_integrity: local tarball not present after upload, "
            "skipping ledger record: %s", local_tar,
        )
        return

    digest = checksum_file(local_tar)  # sha256:<hexdigest>

    # If a ledger entry already exists, check for consistency.
    if os.path.isfile(ledger):
        with open(ledger) as fh:
            existing = fh.read().strip()
        if existing == digest:
            debug("store_integrity: ledger already current for %s", tarball)
            return
        warning(
            "store_integrity: overwriting ledger entry for %s\n"
            "  Old: %s\n  New: %s\n"
            "  This is unexpected — verify that the build hash is stable.",
            tarball, existing, digest,
        )

    _write_ledger(ledger, digest)
    debug("store_integrity: recorded %s  %s", digest, tarball)


def verify_tarball_checksum(
    spec: dict,
    work_dir: str,
    build_arch: str,
    local_tar: str,
) -> None:
    """Verify *local_tar* against the ledger after recall from the remote store.

    Call this after ``syncHelper.fetch_tarball(spec)`` and after confirming
    that the tarball file exists locally.

    Three outcomes:

    * **Match** — debug log; build proceeds normally.
    * **No ledger entry** — the tarball predates the integrity feature or the
      work directory was rebuilt from scratch.  A warning is emitted, the
      digest is recorded for next time, and the build proceeds.  Operators who
      want zero tolerance for unverified tarballs can set
      ``BITS_STRICT_STORE_INTEGRITY=1`` in the environment to make this a
      fatal error instead.
    * **Mismatch** — always fatal: the tarball has been altered since upload.
    """
    if not os.path.isfile(local_tar):
        debug("store_integrity: nothing to verify — tarball absent: %s", local_tar)
        return

    arch = effective_arch(spec, build_arch)
    tarball = os.path.basename(local_tar)
    ledger = _ledger_path(work_dir, arch, spec["hash"], tarball)

    actual = checksum_file(local_tar)

    if not os.path.isfile(ledger):
        strict = os.environ.get("BITS_STRICT_STORE_INTEGRITY", "").strip() == "1"
        msg = (
            "store_integrity: no local checksum ledger for %s\n"
            "  The tarball may have been uploaded before integrity recording "
            "was enabled, or the work directory was rebuilt.\n"
            "  Current digest: %s\n"
            "  Recording digest now for future verification." % (tarball, actual)
        )
        if strict:
            error("%s", msg)
            import sys; sys.exit(1)
        warning("%s", msg)
        _write_ledger(ledger, actual)
        return

    with open(ledger) as fh:
        expected = fh.read().strip()
    if actual == expected:
        debug("store_integrity: %s integrity OK (%s)", tarball, actual)
        return

    # Mismatch — always fatal regardless of strict mode.
    error(
        "INTEGRITY FAILURE: tarball %s does not match its local ledger!\n"
        "  Expected (ledger): %s\n"
        "  Actual  (on-disk): %s\n"
        "\n"
        "  This may indicate that the tarball was silently replaced in the\n"
        "  remote store backend.  Do NOT use this tarball.\n"
        "\n"
        "  To investigate:\n"
        "    1. Delete the local copy:  rm -rf %s\n"
        "    2. Re-fetch from a trusted source and compare.\n"
        "    3. Delete the ledger entry to reset: rm %s",
        tarball, expected, actual, os.path.dirname(local_tar), ledger,
    )
    import sys; sys.exit(1)
