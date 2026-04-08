"""
Source and patch checksum verification.

Checksums are embedded directly in the ``sources:`` and ``patches:`` recipe
entries using a comma-separator syntax::

    sources:
      - https://example.com/libfoo-1.2.tar.gz,sha256:e3b0c44298fc1c149afb...
      - https://example.com/libbar-3.1.tar.xz          # no checksum — optional

    patches:
      - fix-endian.patch,sha256:a665a45920422f9d417e...
      - add-missing-header.patch                        # no checksum — optional

The part after the last comma is treated as a checksum only when it matches
``<algorithm>:<hexdigest>``.  If it does not match, the whole string is treated
as the URL or filename unchanged, so existing recipes require no modification.

Supported algorithms: ``sha256`` (recommended), ``sha512``, ``sha1``, ``md5``.

Enforcement
-----------
Three levels of enforcement exist, controlled by CLI flags and/or a per-recipe
field:

``off`` (default)
    No verification is performed even when a checksum is declared.

``warn``
    When a checksum is declared it is verified; a mismatch emits a warning but
    does not stop the build.  Missing declarations are silently ignored.
    Activated by ``--check-checksums``.

``enforce``
    When a checksum is declared it is verified; a mismatch is a fatal error.
    Packages without *any* declared checksum are also a fatal error.
    Activated by ``--enforce-checksums`` (CLI) or ``enforce_checksums: true``
    in the recipe (per-package opt-in).

``print``
    Checksums are computed and printed in ready-to-paste YAML format; no
    verification is performed.  Activated by ``--print-checksums``.

The effective mode for a given package is the *strictest* of the recipe field
and the CLI flag, in the order ``off < warn < enforce``.  ``print`` is
independent and takes precedence over all other modes.
"""

import hashlib
import re

from bits_helpers.log import debug, warning, dieOnError  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

SUPPORTED_ALGORITHMS = frozenset({"sha256", "sha512", "sha1", "md5"})

# Matches "sha256:abcdef1234..." (hex digits only, case-insensitive)
_CHECKSUM_RE = re.compile(
    r'^(sha256|sha512|sha1|md5):([0-9a-fA-F]+)$',
    re.IGNORECASE,
)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_entry(raw: str):
    """Split ``'url_or_file[,algo:digest]'`` into ``(url_or_file, checksum_or_None)``.

    The checksum is detected by matching ``algo:hexdigest`` after the **last**
    comma.  If the part after the last comma does not look like a checksum the
    entire string is returned as-is and ``None`` is returned for the checksum.

    This rule makes the syntax safe for URLs that contain commas in their query
    strings: only a trailing ``algo:hexdigest`` token is stripped.

    Examples::

        parse_entry("https://example.com/foo.tar.gz,sha256:abc123")
        # → ("https://example.com/foo.tar.gz", "sha256:abc123")

        parse_entry("https://example.com/foo.tar.gz")
        # → ("https://example.com/foo.tar.gz", None)

        parse_entry("https://example.com/q?a=1,2")
        # → ("https://example.com/q?a=1,2", None)   (no algo: prefix → not a checksum)
    """
    raw = raw.strip()
    comma = raw.rfind(",")
    if comma >= 0:
        suffix = raw[comma + 1:].strip()
        if _CHECKSUM_RE.match(suffix):
            return raw[:comma].strip(), suffix
    return raw, None


def parse_checksum(value: str):
    """Parse ``'algo:hexdigest'`` → ``('algo', 'hexdigest')``.

    Raises ``ValueError`` when the format is not recognised.
    """
    m = _CHECKSUM_RE.match(value.strip())
    if not m:
        raise ValueError(
            "Cannot parse checksum %r; expected <algo>:<hexdigest>, "
            "e.g. sha256:e3b0c44298fc1c149afb..." % value
        )
    return m.group(1).lower(), m.group(2).lower()


# ── Hashing ───────────────────────────────────────────────────────────────────

def checksum_file(path: str, algorithm: str = "sha256") -> str:
    """Stream-hash *path* and return ``'algo:hexdigest'``.

    Uses fixed-size reads so that large tarballs are never loaded fully into
    memory.

    Raises ``ValueError`` for unsupported algorithms.
    """
    algo = algorithm.lower()
    if algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(
            "Unsupported checksum algorithm %r.  "
            "Supported: %s" % (algorithm, ", ".join(sorted(SUPPORTED_ALGORITHMS)))
        )
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "%s:%s" % (algo, h.hexdigest())


def verify_file(path: str, expected: str) -> bool:
    """Return ``True`` when *path* matches the *expected* checksum string."""
    algo, expected_digest = parse_checksum(expected)
    actual = checksum_file(path, algo)
    _, actual_digest = parse_checksum(actual)
    return actual_digest == expected_digest.lower()


# ── Enforcement ───────────────────────────────────────────────────────────────

def enforcement_mode(spec: dict, args) -> str:
    """Return the effective enforcement mode for *spec* given CLI *args*.

    Returns one of ``"off"``, ``"warn"``, ``"enforce"``, ``"print"``.
    """
    if getattr(args, "printChecksums", False):
        return "print"
    if getattr(args, "enforceChecksums", False):
        return "enforce"
    if getattr(args, "checkChecksums", False):
        return "warn"
    if spec.get("enforce_checksums"):
        return "enforce"
    return "off"


def check_file(path: str, filename: str, checksum_or_none, mode: str) -> None:
    """Verify *path* against *checksum_or_none* according to *mode*.

    Parameters
    ----------
    path:
        Absolute path to the file that has just been downloaded or copied.
    filename:
        Bare filename shown in log messages (no directory component).
    checksum_or_none:
        Expected checksum string (``'algo:hexdigest'``) or ``None`` when the
        recipe entry carried no checksum declaration.
    mode:
        One of ``"off"``, ``"warn"``, ``"enforce"``, ``"print"``.
    """
    if mode == "print":
        computed = checksum_file(path)
        print("  %s: %s" % (filename, computed))
        return

    if mode == "off":
        return

    if checksum_or_none is None:
        if mode == "enforce":
            dieOnError(True,
                "No checksum declared for %r. "
                "Add a checksum suffix to the recipe entry, e.g.:\n"
                "  - <url>,%s\n"
                "Or run with --check-checksums to generate checksums."
                % (filename, checksum_file(path)))
        # warn mode: silently ignore missing declarations
        return

    if verify_file(path, checksum_or_none):
        debug("Checksum OK: %s (%s)", filename, checksum_or_none.split(":")[0])
        return

    algo = checksum_or_none.split(":")[0]
    computed = checksum_file(path, algo)
    msg = (
        "Checksum MISMATCH for %r:\n"
        "  Expected: %s\n"
        "  Got:      %s"
        % (filename, checksum_or_none, computed)
    )
    if mode == "enforce":
        dieOnError(True, msg)
    else:
        warning(msg)
