"""
Build provenance helpers: a deterministic ``build_id`` (a per-release coherence
token) and an ``abi_tag`` (the ABI-relevant build configuration).

See docs/adr/0001-cvmfs-relaxed-reuse.md. These are *additive* metadata: they
are recorded in a package's ``.meta.json`` but never enter the package hash and
never change build behaviour, so the simple ``bits build`` / aliBuild case is
unaffected (ADR backward-compatibility constraint).

Everything here is deliberately defensive — a minimal build with sparse specs or
a near-empty environment must still produce a value, never raise.
"""

import hashlib
import os


def _label(args) -> str:
    """A human-readable release label for the build_id (e.g. 'release_gcc15')."""
    defaults = getattr(args, "defaults", None) or []
    try:
        label = "_".join(str(d) for d in defaults)
    except TypeError:
        label = ""
    return label or "local"


def compute_abi_tag(args) -> str:
    """Return a readable ABI tag for the current build configuration.

    The qualified architecture string already encodes OS/glibc, compiler and
    build type (e.g. ``ubuntu2510_x86-64-gcc15-dbg``); we append the C++ standard
    and libstdc++ ABI selector when the environment exposes them. Readable on
    purpose (not a hash) so it can be eyeballed in ``.meta.json``.
    """
    arch = str(getattr(args, "architecture", "") or "")
    parts = [arch] if arch else []
    cxxstd = os.environ.get("CXXSTD") or os.environ.get("BITS_CXXSTD")
    if cxxstd:
        parts.append("c++%s" % cxxstd)
    cxx11abi = os.environ.get("_GLIBCXX_USE_CXX11_ABI")
    if cxx11abi:
        parts.append("cxx11abi%s" % cxx11abi)
    return "+".join(parts)


def _member_tuple(spec) -> tuple:
    """(name, version, revision, hash) for one spec, defensively."""
    if not isinstance(spec, dict):
        return ("", "", "", "")
    return (
        str(spec.get("package", "")),
        str(spec.get("version", "")),
        str(spec.get("revision", "")),
        str(spec.get("hash", "")),
    )


def compute_build_id(specs, args) -> str:
    """Return a deterministic build_id for the set of packages in *specs*.

    ``<label>-<digest>`` where the digest is a hash over the sorted
    ``(name, version, revision, hash)`` of every spec that carries a content
    hash. Deterministic and host-independent: the same package set produces the
    same id on any machine, so independent builds/imports of one release agree.
    Specs without a ``hash`` (system packages, placeholders) are excluded.
    """
    try:
        members = sorted(
            _member_tuple(s) for s in specs.values()
            if isinstance(s, dict) and s.get("hash")
        )
    except (AttributeError, TypeError):
        members = []
    digest = hashlib.sha256(repr(members).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (_label(args), digest)


def recipe_tools_ref(specs) -> str:
    """A reference to the bits-recipe-tools used (version+short hash), or ''.

    Part of the strict-reproducibility record: editing bits-recipe-tools
    re-hashes the whole stack, so pinning what produced a build_id matters.
    """
    if not isinstance(specs, dict):
        return ""
    rt = specs.get("bits-recipe-tools")
    if not isinstance(rt, dict):
        return ""
    ver = str(rt.get("version", ""))
    h = str(rt.get("hash", ""))[:8]
    return ("%s-%s" % (ver, h)).strip("-")
