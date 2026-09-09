# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Typed view of the resolved build knobs read across doBuild.

Five build knobs are each read at several points in doBuild via
``getattr(args, NAME, HARD_DEFAULT)``. Because every one is an argparse dest that
doBuild resolves once, unconditionally, before those reads, the hard default can
never actually fire — the differing per-site defaults are dead defensives.
BuildConfig captures the resolved values once so the downstream reads become
typed ``cfg.<knob>`` access instead of repeating the getattr idiom and its
post-read ``or DEFAULT`` guards.

Only the five reconciled knobs live here. ``develPrefix`` (per-site semantics,
a runtime-derived effective default) and ``initdotshFromModules`` (the alidist
hash guardrail, resolved in two stages) are deliberately excluded.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BuildConfig:
  """The five resolved build knobs, read once off a resolved ``args``."""

  oversubscribe: float           # CPU oversubscription factor, >= 1.0
  unleash_final: bool            # let the final package use the full -j
  critical_path_schedule: bool   # order --builders jobs by critical-path weight
  require_signed_reuse: bool     # gate tarball reuse on a verified signed manifest
  reuse_policy: str              # "strict" | "relaxed"

  @classmethod
  def from_args(cls, args):
    """Read the five knobs off *args* using each read site's effective
    expression — the same hard default and the same post-read guard — so
    ``cfg.<knob>`` is a drop-in for the ``getattr(...)`` the site used. Safe to
    call before resolution or on a non-build args: an absent knob yields exactly
    the value the site's getattr default would have.
    """
    return cls(
        oversubscribe=(getattr(args, "oversubscribe", 1.0) or 1.0),
        unleash_final=getattr(args, "unleashFinal", True),
        critical_path_schedule=getattr(args, "criticalPathSchedule", True),
        require_signed_reuse=getattr(args, "requireSignedReuse", False),
        reuse_policy=(getattr(args, "reusePolicy", "strict") or "strict"),
    )
