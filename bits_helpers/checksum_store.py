# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""External checksum store for bits packages.

Each recipe repository can carry an optional ``checksums/`` subdirectory.
A file named ``checksums/<pkgname>.checksum`` (case-insensitive package name)
supplies checksums for that package's sources and patches, and optionally pins
the expected git commit SHA for the ``source:`` + ``tag:`` checkout.

File format (YAML)
------------------

::

    # checksums/mylib.checksum
    # Re-generate with:  bits build --write-checksums mylib

    tag: abc123def456abc123def456abc123def456abc1   # pinned commit SHA

    sources:
      https://example.com/mylib-1.0.tar.gz: sha256:e3b0c44298fc1c149afb...
      https://example.com/extra.tar.bz2:    sha512:cf83e1357eefb8bdf154...

    patches:
      fix-endian.patch:          sha256:a665a45920422f9d417e4867efdc4fb8...
      add-missing-header.patch:  sha256:d41d8cd98f00b204e9800998ecf8427e...

All sections are optional.  The ``tag`` value is a bare commit SHA (no
``algo:`` prefix) because git always uses SHA-1 or SHA-256 for commit
identities.

Merge semantics
---------------

The external file *wins* over any inline checksum carried in the recipe's
``sources:`` or ``patches:`` entries.  If a URL or filename appears in the
external file, that checksum is used regardless of any comma-suffix in the
recipe.  If a URL / filename is **not** in the external file, the inline
comma-suffix (if present) is used as the fallback.

This makes the checksum file the single authoritative security artefact,
while keeping inline entries useful during development or for simple cases.
"""

import os
import re

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from bits_helpers.log import debug, warning

# Commit SHA: 40 hex chars (SHA-1) or 64 hex chars (SHA-256)
_COMMIT_RE = re.compile(r'^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$')

# ── Discovery ─────────────────────────────────────────────────────────────────

def find_checksum_file(pkgdir: str, pkgname: str):
    """Return the path to ``<pkgdir>/checksums/<pkgname>.checksum``, or ``None``.

    The lookup is case-insensitive (package name is lowercased before joining).
    """
    path = os.path.join(pkgdir, "checksums", pkgname.lower() + ".checksum")
    return path if os.path.isfile(path) else None


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_checksum_file(path: str) -> dict:
    """Parse a ``.checksum`` file and return a normalised dict::

        {
            "tag":     "<commit-sha>" | None,
            "sources": {"<url>": "algo:hex", ...},
            "patches": {"<filename>": "algo:hex", ...},
        }

    Unknown keys are silently ignored so that future extensions are backward
    compatible.  Raises ``ValueError`` on YAML parse errors or invalid values.
    """
    if yaml is None:
        raise ImportError(
            "PyYAML is required to parse checksum files: pip install pyyaml"
        )

    with open(path, encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError("YAML parse error in %s: %s" % (path, exc)) from exc

    if not isinstance(data, dict):
        raise ValueError("Checksum file must be a YAML mapping: %s" % path)

    result = {"tag": None, "sources": {}, "patches": {}}

    # --- tag (commit pin) ----------------------------------------------------
    raw_tag = data.get("tag")
    if raw_tag is not None:
        raw_tag = str(raw_tag).strip()
        if not _COMMIT_RE.match(raw_tag):
            raise ValueError(
                "Invalid commit SHA in %s — expected 40 or 64 hex chars, "
                "got: %r" % (path, raw_tag)
            )
        result["tag"] = raw_tag.lower()

    # --- sources -------------------------------------------------------------
    raw_sources = data.get("sources") or {}
    if not isinstance(raw_sources, dict):
        raise ValueError("'sources' in %s must be a YAML mapping" % path)
    for url, cksum in raw_sources.items():
        result["sources"][str(url).strip()] = str(cksum).strip()

    # --- patches -------------------------------------------------------------
    raw_patches = data.get("patches") or {}
    if not isinstance(raw_patches, dict):
        raise ValueError("'patches' in %s must be a YAML mapping" % path)
    for fname, cksum in raw_patches.items():
        result["patches"][str(fname).strip()] = str(cksum).strip()

    debug("Loaded checksum store from %s: tag=%s, %d sources, %d patches",
          path, result["tag"], len(result["sources"]), len(result["patches"]))
    return result


def load_for_spec(spec: dict) -> dict:
    """Convenience wrapper: discover and parse the checksum file for *spec*.

    Returns an empty store dict (tag=None, sources={}, patches={}) if no file
    is found, so callers never have to handle ``None``.
    """
    pkgdir = spec.get("pkgdir", "")
    pkgname = spec.get("package", "")
    path = find_checksum_file(pkgdir, pkgname)
    if path is None:
        return {"tag": None, "sources": {}, "patches": {}}
    try:
        return parse_checksum_file(path)
    except (ValueError, IOError, OSError) as exc:
        warning("Could not load checksum file %s: %s", path, exc)
        return {"tag": None, "sources": {}, "patches": {}}


def merge_into_spec(spec: dict, store: dict) -> None:
    """Inject checksum store data into *spec* in-place.

    Sets:
    - ``spec["source_checksums"]``  — ``{url: "algo:hex", ...}``
    - ``spec["patch_checksums"]``   — ``{filename: "algo:hex", ...}``
    - ``spec["pin_commit"]``        — commit SHA string or ``None``

    These keys are consumed by ``workarea.checkout_sources``.
    """
    spec["source_checksums"] = dict(store.get("sources") or {})
    spec["patch_checksums"] = dict(store.get("patches") or {})
    spec["pin_commit"] = store.get("tag")


# ── Writing ───────────────────────────────────────────────────────────────────

def format_checksum_file(pkgname: str, store: dict) -> str:
    """Render *store* as a YAML ``.checksum`` file string.

    This is called by ``bits build --write-checksums`` to persist computed
    checksums back to the recipe repository.
    """
    lines = [
        "# checksums/%s.checksum" % pkgname.lower(),
        "# Re-generate with:  bits build --write-checksums %s" % pkgname,
        "",
    ]

    if store.get("tag"):
        lines += ["tag: %s" % store["tag"], ""]

    if store.get("sources"):
        lines.append("sources:")
        for url, cksum in sorted(store["sources"].items()):
            lines.append("  %s: %s" % (url, cksum))
        lines.append("")

    if store.get("patches"):
        lines.append("patches:")
        for fname, cksum in sorted(store["patches"].items()):
            lines.append("  %s: %s" % (fname, cksum))
        lines.append("")

    return "\n".join(lines)


def write_checksum_file(pkgdir: str, pkgname: str, store: dict) -> str:
    """Write *store* to ``<pkgdir>/checksums/<pkgname>.checksum``.

    Creates the ``checksums/`` directory if it does not exist.
    Returns the path of the written file.
    """
    checksums_dir = os.path.join(pkgdir, "checksums")
    os.makedirs(checksums_dir, exist_ok=True)
    path = os.path.join(checksums_dir, pkgname.lower() + ".checksum")
    content = format_checksum_file(pkgname, store)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
