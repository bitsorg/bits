# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Typed view of the resolved build configuration read across doBuild.

Each build knob here was read at one or more points in doBuild via
``getattr(args, NAME, HARD_DEFAULT)``. Their values are settled by the end of
doBuild's resolution phase, so BuildConfig snapshots them once (built right after
that phase) and the downstream reads become typed ``cfg.<knob>`` access instead
of repeating the getattr idiom and its post-read ``or DEFAULT`` guards.

Only knobs that are settled by the snapshot point live here. Values mutated later
in the run (``manifest``, ``resources``, ``resourceMonitoring``), the
loop-computed effective ``develPrefix``, and ``initdotshFromModules`` (the alidist
hash guardrail, resolved in two stages) are deliberately excluded.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BuildConfig:
  """The resolved build knobs, read once off a resolved ``args``."""

  oversubscribe: float           # CPU oversubscription factor, >= 1.0
  unleash_final: bool            # let the final package use the full -j
  critical_path_schedule: bool   # order --builders jobs by critical-path weight
  require_signed_reuse: bool     # gate tarball reuse on a verified signed manifest
  reuse_policy: str              # "strict" | "relaxed"

  # Parallelism / resources (settled by the end of the resolution phase).
  mem_per_job_default: int       # per-job memory assumption (MiB), 0 = unset
  parallel_downloads: int        # raw --parallel-downloads (site clamps to >= 1)
  parallel_sources: int          # concurrent source fetches
  prefetch_workers: int          # tarball prefetch workers, -1 = auto

  # Reuse / store consumers (resolved during import; read later in the loop).
  reuse_overlay: Optional[str]   # host path of the imported reuse overlay
  reuse_cvmfs_base: Optional[str]  # CVMFS Packages base for the reuse overlay
  reuse_beacon: Optional[str]    # reuse-beacon URL (falls back to env at the site)
  store_integrity: bool          # verify store-integrity ledger on cached reuse

  # Build-host monitor (each raw value; the site still ORs in system opts / env).
  monitor: Optional[str]         # --monitor mode, None = off
  monitor_url: Optional[str]     # metrics push URL
  monitor_instance: Optional[str]
  monitor_interval: Optional[float]
  monitor_disk_interval: Optional[float]

  # Misc flags read via the getattr(…, default) idiom.
  brew: bool                     # install Homebrew deps on demand
  auto_patch: bool               # auto-apply recipe patches
  auto_resources: bool           # measurement-driven --builders
  provider_policy: dict          # provider prepend/append overrides
  build_local: object            # None | str | comma-list (site normalises)
  cvmfs_prefix: Optional[str]    # CVMFS prefix override

  @classmethod
  def from_args(cls, args):
    """Read the knobs off *args* using each read site's effective expression —
    the same hard default and the same post-read guard — so ``cfg.<knob>`` is a
    drop-in for the ``getattr(...)`` the site used. Safe to call before
    resolution or on a non-build args: an absent knob yields exactly the value
    the site's getattr default would have.
    """
    return cls(
        oversubscribe=(getattr(args, "oversubscribe", 1.0) or 1.0),
        unleash_final=getattr(args, "unleashFinal", True),
        critical_path_schedule=getattr(args, "criticalPathSchedule", True),
        require_signed_reuse=getattr(args, "requireSignedReuse", False),
        reuse_policy=(getattr(args, "reusePolicy", "strict") or "strict"),
        mem_per_job_default=getattr(args, "memPerJobDefault", 0),
        parallel_downloads=getattr(args, "parallelDownloads", 2),
        parallel_sources=getattr(args, "parallelSources", 1),
        prefetch_workers=getattr(args, "prefetchWorkers", -1),
        reuse_overlay=getattr(args, "reuseOverlay", None),
        reuse_cvmfs_base=getattr(args, "reuseCvmfsBase", None),
        reuse_beacon=getattr(args, "reuseBeacon", None),
        store_integrity=getattr(args, "storeIntegrity", False),
        monitor=getattr(args, "monitor", None),
        monitor_url=getattr(args, "monitorUrl", None),
        monitor_instance=getattr(args, "monitorInstance", None),
        monitor_interval=getattr(args, "monitorInterval", None),
        monitor_disk_interval=getattr(args, "monitorDiskInterval", None),
        brew=getattr(args, "brew", False),
        auto_patch=getattr(args, "autoPatch", True),
        auto_resources=getattr(args, "autoResources", False),
        provider_policy=getattr(args, "provider_policy", {}),
        build_local=getattr(args, "buildLocal", None),
        cvmfs_prefix=getattr(args, "cvmfsPrefix", None),
    )
