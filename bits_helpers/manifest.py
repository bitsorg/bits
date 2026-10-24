# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build manifest — captures all inputs and outputs of a bits build run.

Purpose
-------
A build manifest records every parameter, provider, package, and checksum
involved in a build so that the exact same build can be reliably reproduced
later from the manifest alone::

    bits build --from-manifest bits-manifest-20260411T143000Z.json

The manifest is written **incrementally**: after each package completes (or
is confirmed already up-to-date), so a partial build still yields a useful
record of what was completed before the failure.

Location
--------
Manifests are written to a dedicated subdirectory of the work directory::

    $WORK_DIR/
      MANIFESTS/
        bits-manifest-<ISO-timestamp>.json   ← one per build run
        bits-manifest-latest.json            ← symlink to the most recent

The ``bits-manifest-latest.json`` symlink is updated atomically after each
incremental write.

Schema (version 4)
------------------

Version 4 adds ``requires`` / ``build_requires`` (the package's direct runtime
and build-only dependencies, by name) and ``source`` / ``tag`` (git origin) to
each ``PackageEntry``, so the manifest carries what an SBOM needs: the
dependency graph and where git-sourced code came from.  The top-level
``system_packages`` lists the dependencies taken from the system.

Version 3 added ``patches`` (recipe patch filenames + their recorded checksums)
and ``variables`` (the package's resolved recipe variables) to each
``PackageEntry``, so a replay/audit can see every patch and template value that
shaped a build — not just its source checksums.  Consumers should treat unknown
fields as additive and key off ``schema_version``.

::

    {
      "schema_version":    int,         # this implementation writes 4
      "bits_version":      str,         # bits package version (or "unknown")
      "bits_dist_hash":    str,         # BITS_DIST_HASH env var
      "build_epoch":       int,         # bits_helpers/build_epoch.py at build time
      "created_at":        ISO-8601,
      "updated_at":        ISO-8601,
      "status":            "in_progress" | "complete" | "failed",
      "failed_package":    str,         # only present when status == "failed"
      "failure_reason":    str,         # only present when status == "failed"
      "requested_packages": [str],      # packages passed on the command line
      "architecture":      str,
      "defaults":          [str],
      "config_dir":        str,         # absolute path to the .bits checkout
      "config_commit":     str,         # BITS_DIST_HASH of the config repo
      "system_packages":   [str],       # taken from the system, not built (v4+)
      "system_package_info": {name: {"version": str, "references": [str]}},
                                        # their versions, when their checks said (v4+)
      "cvmfs_templates":   {str: str},  # this build's CVMFS layout (optional);
                                        # bits cvmfs publish uses it for all packages
      "providers":         [ProviderEntry],
      "packages":          [PackageEntry]
    }

    ProviderEntry::
    {
      "name":         str,              # provider package name
      "checkout_dir": str,              # absolute path of the local clone
      "commit":       str,              # full git commit hash
      "remote_url":   str | null        # 'origin' remote URL (or null)
    }

    PackageEntry::
    {
      "package":                str,
      "version":                str,
      "revision":               str,
      "pkg_family":             str,              # aliBuild family subdir, e.g. "Pythia"; empty if none
      "effective_architecture": str,              # "share" for noarch packages; build arch otherwise
      "hash":                   str,              # content-addressable build hash
      "commit_hash":            str,              # source commit hash (or "0")
      "outcome":                "already_installed" | "from_store" | "built_from_source",
      "rebuilt_because":        str,              # built_from_source only, when known:
                                                  # why no tarball was reused
      "tarball":                str | null,       # tarball filename
      "tarball_sha256":         str | null,       # sha256:<hex> of the tarball, if present
      "source_checksums":       [SourceEntry],    # per-source archive integrity anchors
      "patches":                [PatchEntry],     # recipe patches + their checksums (v3+)
      "variables":              {str: str},       # resolved recipe variables (v3+)
      "requires":               [str],            # direct runtime (+ untracked) deps (v4+)
      "build_requires":         [str],            # direct build-only dependencies (v4+)
      "source":                 str,              # git repository URL, "" if none (v4+)
      "tag":                    str,              # git tag/branch built, "" if none (v4+)
      "references":             [str],            # CPEs / purls naming it for scanners; only when any
      "built_by":               str | None,       # user@host that compiled this hash; null unless built_from_source
      "completed_at":           ISO-8601
    }

    When ``pkg_family`` is non-empty the on-disk install path includes the
    family as an extra path component::

        $WORK_DIR/<arch>/<pkg_family>/<package>/<version>-<revision>/

    rather than the default::

        $WORK_DIR/<arch>/<package>/<version>-<revision>/

    The publish pipeline uses this field to reconstruct ``SOURCE_DIR``
    correctly without having to scan the filesystem.

    SourceEntry::
    {
      "url":      str,          # source URL or local patch filename
      "checksum": str | null    # declared checksum (algo:hex), or null if none
    }

    ``source_checksums`` contains one entry per item in the recipe's ``sources:``
    list.  For git-sourced packages the list is empty (the ``commit_hash`` field
    already pins the exact revision).  When a checksum was declared in the recipe
    the field is populated immediately — no extra hashing is required at manifest
    write time, and the value matches exactly what the checksum system already
    verified during the build.

    PatchEntry::
    {
      "name":     str,          # patch filename from the recipe's `patches:` list
      "checksum": str | null    # recorded digest (algo:hex) from patch_checksums,
                                #   or null if none was declared/recorded
    }

    ``patches`` lists the recipe's patches with the checksum the checksum system
    recorded for each (``spec['patch_checksums']``); names without a recorded
    digest get ``null``.  ``variables`` is the package's resolved recipe-variable
    map (``%(...)s`` values already expanded), captured for audit/debugging — it
    does not drive replay (replay is pinned by ``requested_packages`` +
    ``config_commit`` + per-package ``hash``).

Replay
------
When ``bits build --from-manifest FILE`` is invoked, bits reads the manifest
and re-runs the build with the same ``requested_packages``, ``architecture``,
``defaults``, and ``config_commit`` pinned.  Each package entry's ``hash``
and ``tarball_sha256`` are used to verify the recalled tarballs, providing
end-to-end integrity even for a replay.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone

try:
    from bits_helpers import __version__
except ImportError:
    __version__ = None

from bits_helpers.build_epoch import BUILD_EPOCH
from bits_helpers.log import debug, warning


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _builder_id() -> str:
    """Identity of the host that built a package, as ``user@shorthost``.

    Captured at build time so the "who built this hash" provenance is recorded
    at the source, rather than inferred from whoever later publishes it.
    """
    import getpass
    import socket
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return "%s@%s" % (user, socket.gethostname().split(".")[0])


def _git_remote_url(directory: str):
    """Return the ``origin`` remote URL for *directory*, or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if result.returncode == 0:
            url = result.stdout.decode(errors="replace").strip()
            return url or None
        return None
    except Exception:
        return None


def _tarball_sha256(tarball_path: str, packed_here: bool = True):
    """Return the SHA-256 digest of *tarball_path* (``sha256:<hex>``), or ``None``.

    A tarball this build packed may be read from its checksum sidecar; one taken
    from a store is always hashed, its own bytes being what matters."""
    if not tarball_path or not os.path.isfile(tarball_path):
        return None
    try:
        from bits_helpers.checksum import checksum_file, tarball_checksum
        if not packed_here:
            return checksum_file(tarball_path)
        return tarball_checksum(tarball_path)
    except Exception as exc:
        warning("manifest: could not checksum %s: %s", tarball_path, exc)
        return None


def _source_entries(spec: dict) -> list:
    """Parse ``spec['sources']`` and return ``[{url, checksum, store_path}]``.

    Uses ``parse_entry()`` from the checksum module to separate the URL from any
    declared checksum suffix (``algo:hexdigest``).  The returned list is empty for
    git-sourced packages — their integrity is already captured by ``commit_hash``.

    ``store_path`` is the archive's location in the bits source store
    (``SOURCES/cache/<h2>/<url_md5>/<file>``), computed here where bits already owns
    the addressing scheme — so consumers (e.g. the per-release SOURCES file) read a
    ready-made path instead of re-deriving it. Omitted if the helpers are absent.

    No file I/O is performed: the checksum value is whatever was declared in the
    recipe, exactly as the checksum system already verified (or would verify) at
    download time.
    """
    try:
        from bits_helpers.checksum import parse_entry
    except ImportError:
        return []
    try:
        from os.path import basename
        from bits_helpers.download import getUrlChecksum
        from bits_helpers.sync import _source_remote_path
    except Exception:  # noqa: BLE001
        getUrlChecksum = _source_remote_path = None
    sources = spec.get("sources") or []
    result = []
    for raw in sources:
        if not isinstance(raw, str):
            continue
        url, checksum = parse_entry(raw)
        entry = {"url": url, "checksum": checksum}
        if url and getUrlChecksum and _source_remote_path:
            try:
                entry["store_path"] = _source_remote_path(getUrlChecksum(url), basename(url))
            except Exception:  # noqa: BLE001
                pass
        result.append(entry)
    return result


def _no_userinfo(url: str) -> str:
    """*url* without user:password@ (a token in a clone URL must not be recorded)."""
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#]*@", r"\1", url)   # up to the last @


def _dep_names(names) -> list:
    """Dependency names in recipe order, once each, without defaults-* pseudo
    packages (not components of the build)."""
    out = []
    for n in names or []:
        if n and not n.startswith("defaults-") and n not in out:
            out.append(n)
    return out


_PYPI_NAME = re.compile(r"""^\s*(?:export\s+)?PYPI_NAME=["']?([A-Za-z0-9._-]+)["']?\s*(?:#.*)?$""", re.M)
_PIP_RECIPE = re.compile(r"^[^#\n]*bits-include\s+PythonPipRecipe\b", re.M)
_CPE_FIELDS = re.compile(r"(?<!\\):")


def _cpe(ref, fill):
    """*ref* (``cpe:2.3:…``, short forms padded with ``*``) with its %(…)s filled
    in, escaped as CPE 2.3 requires; None unless it has the 13 fields. A version
    loses a leading ``v`` (v6.40.02): NVD versions never carry one."""
    for k, v in fill.items():
        if k in ("%(version)s", "%(tag)s"):
            v = re.sub(r"^v(?=\d)", "", v)
        ref = ref.replace(k, re.sub(r"([^A-Za-z0-9._-])", r"\\\1", v))
    n = len(_CPE_FIELDS.split(ref))
    return ref + ":*" * (13 - n) if 5 <= n <= 13 else None


def _references(spec: dict) -> list:
    """The recipe's `references:` (``cpe:2.3:…`` and ``pkg:…`` strings, with
    %(version)s, %(name)s and %(tag)s filled in). A PythonPipRecipe that names
    no purl gets ``pkg:pypi/<PYPI_NAME or package>@<version>``."""
    from urllib.parse import quote
    name, version = str(spec.get("package") or ""), str(spec.get("version") or "")
    fill = {"%(version)s": version, "%(name)s": name, "%(package)s": name,
            "%(tag)s": str(spec.get("tag") or "")}
    raw = spec.get("references") or []
    out = []
    for r in [raw] if isinstance(raw, str) else raw:
        if not isinstance(r, str) or not r.strip():
            continue
        r = r.strip()
        if r.startswith("cpe:2.3:"):
            ref = _cpe(r, fill)
        elif r.startswith("pkg:"):
            ref = r
            for k, v in fill.items():
                ref = ref.replace(k, quote(v, safe=".-_~"))
        else:
            ref = None
        if ref is None:
            warning("%s: ignoring reference %r: not a cpe:2.3: CPE nor a pkg: purl", name, r)
        elif ref not in out:
            out.append(ref)
    body = str(spec.get("recipe") or "")
    if version and not any(r.startswith("pkg:") for r in out) and _PIP_RECIPE.search(body):
        m = _PYPI_NAME.search(body)
        pypi = (m.group(1) if m else name).lower().replace("_", "-")
        out.append("pkg:pypi/%s@%s" % (quote(pypi, safe=".-"), quote(version, safe=".-_~")))
    return out


def _patch_entries(spec: dict) -> list:
    """Return ``[{name, checksum}]`` for each patch the recipe applied.

    ``name`` is the filename from the recipe's ``patches:`` list; ``checksum`` is
    the digest recorded for it in ``spec['patch_checksums']`` (``algo:hex``) or
    ``None`` when none was declared.  No file I/O: the checksum is exactly what
    the checksum system already recorded for the build.  Empty list when the
    package applies no patches.
    """
    patches = spec.get("patches") or []
    checks = spec.get("patch_checksums") or {}
    result = []
    for name in patches:
        if not isinstance(name, str):
            continue
        result.append({"name": name, "checksum": checks.get(name)})
    return result


# ── BuildManifest ─────────────────────────────────────────────────────────────

class BuildManifest:
    """Incremental build manifest written to ``$WORK_DIR/bits-manifest-*.json``.

    Typical lifecycle::

        manifest = BuildManifest(work_dir, requested_packages, ...)
        manifest.add_providers(provider_dirs)          # after provider load
        # main build loop:
        manifest.add_package(spec, "already_installed")
        manifest.add_package(spec, "from_store", tarball_path)
        manifest.add_package(spec, "built_from_source", tarball_path)
        # end of build:
        manifest.complete()   # or manifest.fail(package_name, reason)
    """

    SCHEMA_VERSION = 4
    _LATEST_SYMLINK = "bits-manifest-latest.json"

    def __init__(
        self,
        work_dir: str,
        requested_packages: list,
        architecture: str,
        defaults: list,
        config_dir: str,
        config_commit: str,
        target: str = "",
        cvmfs_templates: dict = None,
    ):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._work_dir = work_dir
        # All manifest files live under $WORK_DIR/MANIFESTS/ so they don't
        # clutter the top-level work directory.
        self._manifest_dir = os.path.join(work_dir, "MANIFESTS")
        os.makedirs(self._manifest_dir, exist_ok=True)
        # Sanitise the target name so it is always safe as a filename component
        # (package names are typically alphanumeric + hyphens, but guard anyway).
        _safe_target = re.sub(r"[^A-Za-z0-9_.+-]", "_", target) if target else ""
        _name = (
            "bits-manifest-{}-{}.json".format(_safe_target, timestamp)
            if _safe_target
            else "bits-manifest-{}.json".format(timestamp)
        )
        self._path = os.path.join(self._manifest_dir, _name)
        # Disk-write coordination: _save serialises a snapshot under _lock,
        # _write_pending performs the I/O under _io_lock with a monotonic
        # sequence so racing writers converge on the newest snapshot.
        self._seq = 0
        self._written_seq = 0
        self._pending = None
        self._io_lock = threading.Lock()
        # Serialises concurrent add_package()/_save() calls from --builders
        # worker threads so they neither corrupt _data nor race os.replace().
        self._lock = threading.Lock()
        self._sys_edges = {}      # package -> (runtime, build) system deps (v4)
        self._data = {
            "schema_version":     self.SCHEMA_VERSION,
            "bits_version":       __version__ or "unknown",
            "build_epoch":        BUILD_EPOCH,
            "bits_dist_hash":     os.environ.get("BITS_DIST_HASH", ""),
            "created_at":         _now_iso(),
            "updated_at":         _now_iso(),
            "status":             "in_progress",
            "requested_packages": list(requested_packages),
            "architecture":       architecture,
            "defaults":           list(defaults),
            "config_dir":         os.path.abspath(config_dir),
            "config_commit":      config_commit,
            "providers":          [],
            "packages":           [],
        }
        if cvmfs_templates:
            self._data["cvmfs_templates"] = dict(cvmfs_templates)
        self._save()
        self._write_pending()
        debug("manifest: initialised at %s", self._path)

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        """Absolute path of the manifest JSON file."""
        return self._path

    @property
    def manifest_dir(self) -> str:
        """Directory that holds all manifest files for this work directory."""
        return self._manifest_dir

    # ── Provider recording ────────────────────────────────────────────────────

    def add_providers(self, provider_dirs: dict) -> None:
        """Record all provider entries from a ``{checkout_dir: (name, commit)}`` dict.

        This is the dict returned by both ``load_always_on_providers()`` and
        ``fetch_repo_providers_iteratively()``.  Call once after merging both.
        """
        with self._lock:
            for checkout_dir, (name, commit) in provider_dirs.items():
                abs_dir = os.path.abspath(checkout_dir)
                entry = {
                    "name":         name,
                    "checkout_dir": abs_dir,
                    "commit":       commit,
                    "remote_url":   _git_remote_url(abs_dir),
                }
                self._data["providers"].append(entry)
                debug("manifest: recorded provider %s @ %s", name, commit[:10])
            if provider_dirs:
                self._data["updated_at"] = _now_iso()
                self._save()
        self._write_pending()

    # ── Package recording ─────────────────────────────────────────────────────

    def set_system_packages(self, names, edges=None, info=None) -> None:
        """Record the packages taken from the system (not built) and, per built
        package, its (runtime, build) dependencies among them: the build drops
        those from the specs' requires, so the SBOM graph needs them here.
        *info* gives the versions (and CPEs/purls) known for them."""
        with self._lock:
            self._data["system_packages"] = sorted(names or ())
            known = {n: i for n, i in (info or {}).items() if i.get("version")}
            if known:
                self._data["system_package_info"] = known
            self._sys_edges = dict(edges or {})

    def add_package(
        self,
        spec: dict,
        outcome: str,
        tarball_path: str = None,
        effective_architecture: str = "",
    ) -> None:
        """Record a completed package in the manifest.

        Parameters
        ----------
        spec:
            The spec dict for the package (as used throughout build.py).
        outcome:
            One of ``"already_installed"``, ``"from_store"``,
            ``"built_from_source"``.
        tarball_path:
            Absolute path to the local tarball file, if one exists.  Used to
            compute ``tarball_sha256``.
        effective_architecture:
            The architecture string actually used in paths and tarball names
            for this package.  ``"share"`` for packages that declare
            ``architecture: share``; the real build arch otherwise.  Used by
            the publish pipeline to locate the tarball and choose the correct
            CVMFS path template.
        """
        from bits_helpers.sync import redistributable_forms
        _forms = redistributable_forms(spec.get("redistributable"))
        _refs = _references(spec)
        entry = {
            "package":                spec.get("package", ""),
            "version":                spec.get("version", ""),
            "revision":               spec.get("revision", ""),
            # pkg_family is used by the publish pipeline to reconstruct the
            # on-disk install path when aliBuild inserts a family subdirectory:
            #   $WORK_DIR/<arch>/<pkg_family>/<package>/<version>-<revision>/
            # Empty string means no family subdir (standard layout).
            "pkg_family":             spec.get("pkg_family", ""),
            # effective_architecture is "share" for noarch packages (those
            # that declare `architecture: share` in their recipe), and the
            # real build architecture for all other packages.  The publish
            # pipeline uses this to locate the tarball under TARS/<eff_arch>/
            # and to select the appropriate CVMFS path template.
            "effective_architecture": effective_architecture,
            # Repository packages (provides_repository: true, e.g. lcg.bits) only
            # trigger recipe-repo loading and carry no publishable artifacts. The
            # publish pipeline skips them so they never reach the store or CVMFS.
            "provides_repository":    bool(spec.get("provides_repository", False)),
            # Publish policy (hash-excluded metadata). A package whose binaries
            # are not redistributable (redistributable: sources|none — e.g. the
            # Oracle client, qgraf) is still built and usable locally, but is
            # never uploaded to the store nor published to CVMFS; one whose
            # sources are not redistributable (binaries|none) never has its
            # source archives mirrored to the store.
            # Which forms of this package may be redistributed — recorded as
            # the CANONICAL enum value (all | binaries | sources | none),
            # normalised by the same parser the upload gates use
            # (bits_helpers.sync.redistributable_forms) so the manifest can
            # never disagree with what the build actually did. Legacy recipe
            # booleans normalise to all/none.
            "redistributable":        ("all" if _forms == {"binaries", "sources"}
                                       else "binaries" if _forms == {"binaries"}
                                       else "sources" if _forms == {"sources"}
                                       else "none"),
            # SPDX license id (hash-excluded metadata). Carried so the publish step
            # can aggregate a per-release NOTICE / attribution file.
            "license":                (spec.get("license") or ""),
            # The recipe's `view:` rules (hash-excluded presentation), applied
            # when a release's merged view is built. Only recorded when set.
            **({"view": spec["view"]} if spec.get("view") is not None else {}),
            "hash":                   spec.get("hash", ""),
            "commit_hash":            spec.get("commit_hash", ""),
            "outcome":                outcome,
            "tarball":                os.path.basename(tarball_path) if tarball_path else None,
            # Prefer the checksum of the object actually in the remote store
            # (recorded on the spec by the upload path). The store object is
            # authoritative: a .tar.gz is not byte-reproducible, so when the
            # upload found an object already at the designated path it kept it,
            # and this build's locally-packed bytes may differ. Recording the
            # store's sha256 keeps every manifest consistent with the one stable
            # object that `bits sign` validates.
            "tarball_sha256":         spec.get("store_tarball_sha256")
                                      or _tarball_sha256(tarball_path,
                                                         packed_here=outcome != "from_store"),
            "source_checksums":       _source_entries(spec),
            # v3: patch provenance (names + recorded checksums) and the resolved
            # recipe variables that shaped this build.
            "patches":                _patch_entries(spec),
            "variables":              dict(spec.get("variables") or {}),
            # v4: direct dependency edges (names), for the SBOM dependency graph.
            "requires":               _dep_names(
                list(spec.get("runtime_requires") or []) + list(spec.get("untracked_requires") or [])
                + list(self._sys_edges.get(spec.get("package"), ((), ()))[0])),
            "build_requires":         _dep_names(
                list(spec.get("build_requires") or [])
                + list(self._sys_edges.get(spec.get("package"), ((), ()))[1])),
            **({"references": _refs} if _refs else {}),
            "source":                 _no_userinfo(spec.get("source") or ""),
            "tag":                    str(spec.get("tag") or ""),
            # built_by identifies the host that actually compiled this hash.
            # Only meaningful when the package was built here; for from_store /
            # already_installed outcomes the real builder lives in another
            # build's manifest, so leave it null rather than claim it.
            "built_by":               _builder_id() if outcome == "built_from_source" else None,
            "completed_at":           _now_iso(),
            # Why a package was built here rather than reused (additive).
            **({"rebuilt_because": spec["reuse_note"]}
               if outcome == "built_from_source" and spec.get("reuse_note") else {}),
        }
        with self._lock:
            self._data["packages"].append(entry)
            self._data["updated_at"] = _now_iso()
            self._save()
        self._write_pending()          # disk I/O outside the data lock
        debug("manifest: %s recorded as %s", spec.get("package", "?"), outcome)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def packages(self) -> list:
        """The package entries recorded so far (copies)."""
        with self._lock:
            return [dict(e) for e in self._data["packages"]]

    def complete(self) -> None:
        """Mark the manifest as successfully completed and write a final save."""
        with self._lock:
            self._data["status"] = "complete"
            self._data["updated_at"] = _now_iso()
            self._save()
        self._write_pending()
        debug("manifest: complete — %s", self._path)

    def fail(self, package_name: str = "", reason: str = "") -> None:
        """Mark the manifest as failed (e.g. build script exited non-zero).

        The manifest still contains all packages recorded up to this point,
        so partial builds are preserved for inspection.
        """
        with self._lock:
            self._data["status"] = "failed"
            self._data["updated_at"] = _now_iso()
            if package_name:
                self._data["failed_package"] = package_name
            if reason:
                self._data["failure_reason"] = reason
            self._save()
        self._write_pending()
        debug("manifest: failed at package %s", package_name or "(unknown)")

    # ── Serialisation ─────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str) -> dict:
        """Load and return the manifest at *path* as a plain ``dict``.

        This is a lightweight helper for the ``--from-manifest`` replay path.
        It does not return a ``BuildManifest`` instance (which would try to
        write a *new* manifest file).
        """
        with open(path) as fh:
            return json.load(fh)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomically write the JSON manifest and update the ``latest`` symlink.

        MUST be called with ``self._lock`` held (every caller mutates
        ``_data`` under it). The expensive part — serialising the whole
        manifest — happens here under the lock so the snapshot is consistent,
        but the DISK I/O is handed to :meth:`_write` which runs under a
        separate I/O lock: with ``--builders`` every package completion saves,
        and holding the data lock across ``os.replace`` serialised all
        builders on disk latency. A monotonic sequence number makes the
        file-on-disk converge on the NEWEST snapshot even when two writers
        race (the loser's older payload is discarded, never written over a
        newer one).

        Uses a unique temp file (not a fixed ``<path>.tmp``) so that concurrent
        --builders workers cannot race on a shared temp name -- previously two
        simultaneous saves could leave one ``os.replace`` with a missing temp
        (FileNotFoundError), failing an otherwise-successful package.
        """
        self._seq += 1
        self._pending = (self._seq, json.dumps(self._data, indent=2) + "\n")

    def _write_pending(self) -> None:
        """Write the newest serialised snapshot to disk (outside _lock)."""
        with self._lock:
            pending = self._pending
        if pending is None:
            return
        seq, payload = pending
        with self._io_lock:
            if seq <= self._written_seq:
                return                      # a newer snapshot already landed
            fd, tmp = tempfile.mkstemp(dir=self._manifest_dir,
                                       prefix=".manifest-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
                self._written_seq = seq
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

            # Update the ``bits-manifest-latest.json`` symlink atomically.
            # The symlink lives alongside the timestamped files inside MANIFESTS/.
            latest = os.path.join(self._manifest_dir, self._LATEST_SYMLINK)
            tmp_link = latest + ".tmp"
            try:
                if os.path.lexists(tmp_link):
                    os.unlink(tmp_link)
                os.symlink(os.path.basename(self._path), tmp_link)
                os.replace(tmp_link, latest)
            except OSError as exc:
                warning("manifest: could not update latest symlink: %s", exc)
