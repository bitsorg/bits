# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
from bits_helpers.utilities import normalise_multiple_options, readDefaults
from bits_helpers.arch import (detectArch, arch_distro_token, arch_machine_token,
                               normalise_arch_key, detectArchComponents,
                               apply_arch_template)
from bits_helpers.workarea import cleanup_git_log
import multiprocessing

import re
import os
import platform
import shlex

import subprocess as commands
from os.path import abspath, dirname, basename
import sys

# Default workdir: fall back on "sw" if env is not set or empty
DEFAULT_WORK_DIR = os.environ.get("BITS_WORK_DIR") or os.environ.get("ALICE_WORK_DIR") or "sw"


def _add_s3_connection_opts(group):
  """Add the S3 connection options to an argparse group (build and doctor).

  These configure the connection for b3:// remote/write stores, so tarballs can
  be archived to and reused from a non-CERN bucket (AWS S3, MinIO, Ceph RGW, …).
  Each option overrides the matching environment variable, which in turn may be
  set as a GitLab CI/CD variable or in the gitlab-runner `environment`; if
  neither is given the aliBuild-compatible defaults apply (CERN S3, credentials
  from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY). Precedence: flag > env var >
  default. All default to None so that, unless used, behaviour is unchanged.
  """
  group.add_argument("--s3-endpoint", dest="s3Endpoint", metavar="URL", default=None,
                     help=("S3 endpoint URL for b3:// stores (e.g. "
                           "https://s3.amazonaws.com or http://minio.local:9000). "
                           "Overrides $S3_ENDPOINT_URL / $AWS_ENDPOINT_URL_S3. "
                           "Default: https://s3.cern.ch. Set this to use a non-CERN bucket."))
  group.add_argument("--s3-access-key", dest="s3AccessKey", metavar="KEY", default=None,
                     help=("S3 access key id. Overrides $AWS_ACCESS_KEY_ID. Prefer the "
                           "env var (a CI/CD variable or gitlab-runner `environment` entry) "
                           "so the secret is not visible in the process list."))
  group.add_argument("--s3-secret-key", dest="s3SecretKey", metavar="KEY", default=None,
                     help=("S3 secret access key. Overrides $AWS_SECRET_ACCESS_KEY. "
                           "Prefer the env var over this flag for the same reason."))
  group.add_argument("--s3-region", dest="s3Region", metavar="REGION", default=None,
                     help=("S3 region name (required by some non-CERN providers). "
                           "Overrides $AWS_DEFAULT_REGION."))
  group.add_argument("--s3-addressing-style", dest="s3AddressingStyle",
                     choices=["auto", "path", "virtual"], default=None,
                     help=("S3 addressing style for b3:// stores. Self-hosted S3 such "
                           "as MinIO usually needs 'path'. Overrides $S3_ADDRESSING_STYLE; "
                           "default lets boto3 choose."))


def _host_online_cpus():
  """Return the kernel's online-CPU range string for use with --cpuset-cpus.

  Reads /sys/devices/system/cpu/online (e.g. "0-7" or "0-3,5-7"), which
  reflects the actual hardware CPU set regardless of any cgroup CPU quota
  that may be in effect for the calling process.  Falls back to
  ``os.cpu_count()`` on platforms where sysfs is unavailable (macOS, WSL1).

  This value is injected as ``--cpuset-cpus`` into every Docker build
  container so that ``make -j`` and similar tools always see the
  full host core count rather than a potentially narrower cgroup quota
  inherited from the GitLab runner process.

  Callers can suppress the automatic injection by including their own
  ``--cpuset-cpus`` flag in ``--docker-extra-args``.
  """
  try:
    with open("/sys/devices/system/cpu/online") as f:
      return f.read().strip()
  except OSError:
    return "0-%d" % ((os.cpu_count() or 1) - 1)


def _docker_memory_args():
  """Return the ``--memory``/``--memory-swap`` flags for the build container.

  Hard memory cap so that no single build can OOM the HOST: the kernel then
  OOM-kills inside the container's cgroup only, and host services (runner,
  sshd, monitoring) survive. The cap is host total minus a reserve of
  max(4 GiB, 10%). ``--memory-swap`` is set to the same value so a capped
  build cannot thrash host swap instead. Inside the container,
  available_memory_mib() reads the cgroup limit, so $JOBS self-throttles to
  fit rather than hitting the cap (see memory.py).

  Returns [] (no cap) when: not Linux (docker on macOS runs in a VM that
  already bounds memory), BITS_DOCKER_MEMORY=off/0/false/no, the host total
  cannot be read, or the host is smaller than the reserve.
  BITS_DOCKER_MEMORY=<size> (docker syntax, e.g. 48g) overrides the computed
  cap. Callers skip the injection entirely when the user already passed any
  ``--memory*`` in --docker-extra-args.
  """
  if platform.system() != "Linux":
    return []
  bdm = os.environ.get("BITS_DOCKER_MEMORY", "").strip()
  if bdm.lower() in ("off", "0", "false", "no"):
    return []
  mem_flag = bdm
  if not mem_flag:
    try:
      with open("/proc/meminfo") as fh:
        total_kib = next((int(l.split()[1]) for l in fh
                          if l.startswith("MemTotal:")), 0)
      total_mib = total_kib // 1024
      reserve = max(4096, total_mib // 10)
      if total_mib > reserve:
        mem_flag = "%dm" % (total_mib - reserve)
    except Exception:  # pylint: disable=broad-except
      pass  # detection failed → no cap (previous behaviour)
  if not mem_flag:
    return []
  return ["--memory=" + mem_flag, "--memory-swap=" + mem_flag]

# cd to this directory before start
DEFAULT_CHDIR = os.environ.get("BITS_CHDIR") or "."

# Default S3 content store for the store-operating actions (certify, compliance,
# gc, store-stats, publish). Precedence: CLI --remote-store (or a .bitsuse-recorded
# one) > $BITS_S3_STORE > this literal. Same env var the bitsStore launcher reads.
DEFAULT_S3_STORE = os.environ.get("BITS_S3_STORE") or "https://s3.cern.ch/lcgapp-bits-testing"

# Worker count assumed when --parallel/--builders is given with no number.
BUILDERS_AUTO = 4


class _WarnAliasAction(argparse.Action):
  """Store the value (or const, for a nargs=0 flag); warn when a deprecated
  spelling is used. The canonical spelling is the first option string."""
  def __call__(self, parser, namespace, values, option_string=None):
    if option_string and option_string != self.option_strings[0]:
      from bits_helpers.log import warning
      warning("%s is deprecated; use %s.", option_string, self.option_strings[0])
    setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def _parse_provider_policy(value: str) -> dict:
  """Parse a ``provider_policy`` string into a ``{provider_name: position}`` dict.

  The format is a comma-separated list of ``name:position`` pairs where
  *position* is either ``"prepend"`` or ``"append"``::

      bits-providers:prepend, myorg-recipes:append

  Provider names are lower-cased for consistent lookup.  Malformed entries
  and unrecognised position values are skipped with a warning printed to
  stderr.  Returns an empty dict for an empty or missing *value*.

  This is the sole parsing point for the ``--provider-policy`` CLI flag so
  that all inputs share identical validation logic.
  """
  from bits_helpers.log import warning as log_warning
  result = {}
  if not value:
    return result
  for token in value.split(","):
    token = token.strip()
    if not token:
      continue
    name, sep, pos = token.partition(":")
    name = name.strip().lower()
    pos  = pos.strip().lower()
    if not name or not sep:
      log_warning(
        "provider_policy: ignoring malformed entry %r — expected name:position",
        token,
      )
      continue
    if pos not in ("prepend", "append"):
      log_warning(
        "provider_policy: ignoring entry %r — position must be 'prepend' or 'append'",
        token,
      )
      continue
    result[name] = pos
  return result


# This is syntactic sugar for the --dist option (which should really be called
# --dist-tag). It can be either:
# - A tag name
# - A repository spec in the for org/repo@tag
def bits_string(s):
  repo, have_repo_spec, ver = s.partition("@")
  if not have_repo_spec:
    repo, ver = "alisw/alidist", "master"
    print(s)
  return {"repo": repo, "ver": ver}


def doParseArgs():
  detectedArch = detectArch()

  # Shared adders for the cross-cutting options, so every action gets the same
  # flag string, dest, metavar and default by construction (no per-action drift).
  # Help stays per-action (passed in). config-dir also takes a per-action default
  # because `bits init` places recipes under DEVELPREFIX, not BITS_REPO_DIR.
  def add_architecture(p, help):
    p.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH",
                   default=detectedArch, help=help)
  def add_work_dir(p, help):
    p.add_argument("-w", "--work-dir", dest="workDir", metavar="WORKDIR",
                   default=DEFAULT_WORK_DIR, help=help)
  def add_config_dir(p, help, default=None):
    p.add_argument("-c", "--config-dir", "--config", dest="configDir",
                   metavar="CONFIGDIR",
                   default=os.environ.get("BITS_REPO_DIR", ".") if default is None else default,
                   help=help)
  def add_chdir(p, help):
    p.add_argument("-C", "--chdir", dest="chdir", metavar="DIR",
                   default=DEFAULT_CHDIR, help=help)
  def add_defaults(p, help):
    p.add_argument("--defaults", dest="defaults", metavar="DEFAULT", default="release",
                   help=help)
  def add_search_path(p, help=("Comma-separated recipe sub-repos to search besides "
                               "CONFIGDIR (relative NAME -> <config-dir>/NAME.bits, "
                               "absolute used as-is). Seeds BITS_PATH; an explicit "
                               "$BITS_PATH wins.")):
    p.add_argument("--search-path", dest="searchPath", metavar="NAMES", default=None,
                   help=help)
  def add_remote_store(p, dest, help, default=DEFAULT_S3_STORE):
    # Canonical --remote-store with --store kept as a deprecated alias (warns).
    p.add_argument("--remote-store", "--store", dest=dest, metavar="URL",
                   default=default, action=_WarnAliasAction, help=help)

  parser = argparse.ArgumentParser(epilog="""\
  For help about each option, specify --help after the option itself. For
  complete documentation please refer to https://alisw.github.io/alibuild.
  """)

  parser.add_argument("-d", "--debug", dest="debug", action="store_true", help="Enable debug log output")
  parser.add_argument("-n", "--dry-run", dest="dryRun", action="store_true",
                      help="Print what would happen, without actually doing it.")

  subparsers = parser.add_subparsers(dest="action")
  subparsers.add_parser("architecture", help="display detected architecture",
                        description="Display the detected architecture.")
  build_parser = subparsers.add_parser("build", help="build a package",
                                       description="Build a package.")
  clean_parser = subparsers.add_parser("clean", help="clean up build area",
                                       description="Clean up the build area.")
  cleanup_parser = subparsers.add_parser(
      "cleanup",
      help="evict stale packages from a persistent workDir",
      description=(
          "Evict packages from the persistent build workDir whose sentinel files "
          "have not been touched within the configured age window, and/or free space "
          "when disk usage exceeds a threshold (least-recently-used first). "
          "Safe to run concurrently with active build jobs."
      ),
  )
  deps_parser = subparsers.add_parser("deps", help="generate a dependency graph for a given package",
                                      description="Generate a dependency graph for a given package.")
  doctor_parser = subparsers.add_parser("doctor", help="verify status of your system",
                                        description="Verify the status of your system.")
  brew_parser = subparsers.add_parser("brew", help="generate a Homebrew Brewfile from recipes (macOS)",
                                      description="Scan recipes for Homebrew-sourced system packages "
                                                  "(homebrew_formula:) and write a Brewfile listing the "
                                                  "formulae the stack expects. Run 'brew bundle' against "
                                                  "it to install them, or build with --brew to install on "
                                                  "demand.")
  init_parser = subparsers.add_parser("init", help="initialise local packages",
                                      description="Initialise development packages.")
  version_parser = subparsers.add_parser("version", help="display %(prog)s version",
                                         description="Display %(prog)s and architecture.")
  publish_parser = subparsers.add_parser(
      "publish",
      help="copy, relocate, and stream a built package to a CVMFS ingestion spool",
      description=(
          "Copies the immutable installation from WORKDIR, relocates it to the "
          "final CVMFS target path, and streams the result to an ingestion spool "
          "for content-addressed pre-staging before the CVMFS transaction."
      ),
  )
  certify_parser = subparsers.add_parser(
      "certify",
      help="merge build manifests into a signed common manifest (trust unit)",
      description=(
          "Merge one or more published build manifests into a single common "
          "manifest, validate every content hash against the S3 store, and sign "
          "the result with the release Ed25519 key. The signed common manifest "
          "is what clients trust for binary reuse (see docs/adr/0004)."
      ),
  )
  gc_parser = subparsers.add_parser(
      "gc",
      help="sweep unreferenced objects from the shared S3 store (reachability GC)",
      description=(
          "Reachability garbage collection (ADR-0004 §6): the roots are every "
          "content hash in the verified signed common manifest; any store object "
          "whose hash is not a root and is older than the grace period is swept. "
          "Fail-closed: refuses to run if the manifest does not verify."
      ),
  )
  store_stats_parser = subparsers.add_parser(
      "store-stats",
      help="summarise S3 binary-store usage (per-arch + per-build/signed)",
      description=(
          "Walk the S3 binary store and write a store.json the Monitoring "
          "dashboard consumes: per-architecture byte/object totals plus a "
          "per-build (manifest) breakdown with a signed flag. Runs where bits "
          "already has the S3 credentials + manifests, replacing the standalone "
          "store-stats CI collector. Optionally pushes Prometheus gauges."
      ),
  )
  compliance_parser = subparsers.add_parser(
      "compliance",
      help="audit recipe licence metadata and the binary store",
      description=(
          "Summarise licence-compliance status: scan recipes for "
          "license:/redistributable: metadata (missing licences, unverified "
          "LicenseRef-* ids, the redistributable:false CVMFS-exclusion list), "
          "probe whether the S3 store answers unauthenticated requests, and "
          "report every stored or certified package whose current recipe "
          "forbids redistribution. With PACKAGE roots (e.g. 'bits compliance "
          "externals generators'), the audit follows the same repository-"
          "discovery path as bits build (config dir, defaults profile, "
          "repository providers) and covers exactly the resolved dependency "
          "closure of those roots for the selected group; without roots it "
          "scans one recipe directory (--recipes, default CWD). Read-only. "
          "Exit 0 = clean, 1 = issues found, so it can gate CI."
      ),
  )
  status_parser = subparsers.add_parser(
      "status",
      help="show what bits build would do for each package (dry run)",
      description=(
          "Resolve the full dependency tree for the requested package(s) and "
          "report what bits build would do for each package without actually "
          "building anything.  Each package is classified as: already_installed, "
          "from_store (local tarball), from_remote_store (remote tarball, requires "
          "--check-store), local_checkout (development package, will rebuild), "
          "local_checkout_unchanged (development package, nothing changed), "
          "build_from_source (will compile), or hash_unknown (git refs not cached; "
          "re-run with --fetch-repos to resolve)."
      ),
  )
  verify_parser = subparsers.add_parser(
      "verify",
      help="verify a live deployment against a build manifest",
      description=(
          "Check that a live deployment is consistent with a bits build manifest.  "
          "For each package in the manifest, the tarball is located under "
          "--cvmfs-root and/or --work-dir, its SHA-256 is recomputed, and the "
          "result is compared to the value recorded in the manifest.  "
          "For each provider, the current HEAD commit of the local checkout is "
          "compared to the commit recorded in the manifest.  "
          "Exit 0 = clean, 1 = FAIL (mismatch), 2 = MISS (tarball not found), "
          "3 = manifest unreadable."
      ),
  )
  stats_parser = subparsers.add_parser(
      "stats",
      help="show a human-readable resource report from a monitored build",
      description=(
          "Summarise the resource usage recorded when a build ran with "
          "--resource-monitoring. Reads <work-dir>/LOGS/<arch>/bits_build_stats.json and the "
          "per-package traces under SPECS/, leads with the heaviest/slowest "
          "packages, and flags likely memory or parallelism problems."
      ),
  )
  add_work_dir(stats_parser,
               help="Build work area to read stats from (default: %(default)s).")
  stats_parser.add_argument("--package", dest="package", metavar="NAME", default=None,
                            help="Show the resource timeline detail for a single package.")
  stats_parser.add_argument("--top", dest="top", type=int, default=10, metavar="N",
                            help="Show the top N packages in the table (default: %(default)s).")
  stats_parser.add_argument("--sort", dest="sort", choices=["time", "rss", "cpu"],
                            default="time", help="Sort the table by this metric (default: %(default)s).")
  stats_parser.add_argument("--json", dest="json_output", action="store_true",
                            help="Emit machine-readable JSON instead of the text report.")

  import_parser = subparsers.add_parser(
      "import",
      help="import a foreign CVMFS deployment (e.g. LCG) into a bits reuse overlay",
      description=(
          "Harvest each deployed module's resolved environment (or read a "
          "manifest), closure-check the set, stamp it with one deterministic "
          "build_id, and generate a per-build_id overlay (build-sufficient bits "
          "modulefiles + module-side .meta.json + .cvmfscatalog) that "
          "'bits build --reuse-from <modules-path>|cvmfs' can reuse without "
          "recompiling."
      ),
  )
  add_work_dir(import_parser,
               help="Build work area (overlay defaults to <work-dir>/MODULES).")
  add_architecture(import_parser,
                   help="Architecture the deployment was built for (default: %(default)s).")
  import_parser.add_argument("--modulepath", dest="importModulepath",
                             metavar="DIR", default=None,
                             help="MODULEPATH of the foreign deployment to harvest via modulecmd.")
  import_parser.add_argument("--manifest", dest="importManifest",
                             metavar="FILE", default=None,
                             help="JSON manifest to import instead of harvesting (fallback when "
                                  "no modulefiles exist).")
  import_parser.add_argument("--trusted", dest="importTrusted", action="store_true",
                             help="Trusted mode: harvest a bits-built deployment directly, reading "
                                  "its own modulefiles (--modulepath) and re-anchoring them to "
                                  "--install-base, capturing package hashes from the install tree. "
                                  "Deterministic (no modulecmd); publishable strict reuse.")
  import_parser.add_argument("--install-base", dest="importInstallBase",
                             metavar="DIR", default=None,
                             help="With --trusted, the absolute Packages root the modulefiles' "
                                  "BASEDIR resolves to (and where each package's .meta.json lives).")
  import_parser.add_argument("--aliases", dest="importAliases",
                             metavar="FILE", default=None,
                             help="JSON name-alias map (foreign -> bits names).")
  import_parser.add_argument("--label", dest="importLabel",
                             metavar="NAME", default=None,
                             help="Human-readable build_id prefix (e.g. LCG_109). Default: import.")
  import_parser.add_argument("--out", dest="importOut",
                             metavar="DIR", default=None,
                             help="Overlay root to write into (default: <work-dir>/MODULES).")
  import_parser.add_argument("--force-overwrite", "--force", dest="importForce",
                             nargs=0, const=True, default=False, action=_WarnAliasAction,
                             help="Stamp and write even if the release is not closed (deps missing).")


  # Options for the build command
  build_parser.add_argument("pkgname", metavar="PACKAGE", nargs="+",
                            help="One of the packages in CONFIGDIR. May be specified multiple times.")

  add_defaults(build_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")

  build_parser.add_argument("--flavour", "--flavor", dest="flavours", action="append",
                            default=[], metavar="NAME[=VALUE]",
                            help=("Set a build-wide flavour variable (repeatable, comma-separated). "
                                  "NAME -> true, NAME=VALUE -> VALUE, !NAME -> false. Flavours gate "
                                  "conditional requires/sources/patches via (?NAME) and are exported "
                                  "into the build environment; they override a defaults `variables:` "
                                  "entry of the same name."))

  add_architecture(build_parser,
                   help=("Build as if on the specified architecture. When used with --docker, build "
                         "inside a Docker image for the specified architecture. Default is the current "
                         "system architecture, which is '%(default)s'."))
  build_parser.add_argument("--force-unknown-architecture", dest="forceUnknownArch", action="store_true",
                            help="Build on this system, even if it doesn't have a supported architecture.")
  build_parser.add_argument("-z", "--devel-prefix", nargs="?", dest="develPrefix", default=argparse.SUPPRESS,
                            help="Version name to use for development packages. Defaults to branch name.")
  build_parser.add_argument("-e", dest="environment", action="append", default=[],
                            help="KEY=VALUE binding to add to the build environment. May be specified multiple times.")
  build_parser.add_argument("-j", "--jobs", dest="jobs", type=int, default=multiprocessing.cpu_count(),
                            help=("The number of parallel compilation processes to run. "
                                  "Default for this system: %(default)d."))
  build_parser.add_argument("--parallel", "--builders", dest="builders", type=int,
                            nargs="?", const=BUILDERS_AUTO, default=1, metavar="N",
                            help=("Build N independent packages in parallel. Given with no "
                                  "number it uses %(const)d; omitted entirely the build is "
                                  "serial. (--builders is a kept alias.)"))
  build_parser.add_argument("--oversubscribe", dest="oversubscribe", type=float, default=None,
                            metavar="FACTOR",
                            help=("CPU oversubscription factor (>= 1.0) for the per-builder "
                                  "-j share. A deep dependency tree rarely keeps all --builders "
                                  "busy, so each package's -j = ceil(jobs * FACTOR / builders), "
                                  "still clamped to -j and to the (unscaled) memory cap. >1.0 "
                                  "fills idle cores at the cost of mild overshoot when all "
                                  "builders are busy (absorbed by the OS scheduler / nice ladder). "
                                  "When unset, falls back to `build_oversubscribe:` in the active "
                                  "defaults, then 1.0 (no oversubscription)."))
  # The final (top-level) package builds alone — everything else is one of its
  # already-finished dependencies — so dividing its -j by --builders needlessly
  # starves the single largest compile of the run (e.g. ROOT getting -j7 of 32).
  # Tri-state on a shared dest: neither flag set (None) → resolved from
  # `build_unleash_final:` in the active defaults, then on.
  build_parser.add_argument("--unleash-final", dest="unleashFinal",
                            action="store_const", const=True, default=None,
                            help=("Let the final (top-level) package use the full -j instead of the "
                                  "per-builder share, since it builds alone once its dependencies "
                                  "finish. The memory cap (mem_per_job) still applies. On by default; "
                                  "only affects --builders > 1. Falls back to `build_unleash_final:` "
                                  "in the active defaults when unset."))
  build_parser.add_argument("--no-unleash-final", dest="unleashFinal",
                            action="store_const", const=False,
                            help="Keep the final package on the per-builder -j share (disable unleashing).")
  # Critical-path scheduling for --builders: order ready jobs by the longest
  # (history-weighted) path to the final target, so the build's long pole starts
  # as early as its dependencies allow. ON by default; tri-state so the active
  # defaults can override via `build_critical_path_schedule:`.
  build_parser.add_argument("--critical-path-schedule", dest="criticalPathSchedule",
                            action="store_const", const=True, default=None,
                            help=("Order --builders jobs by their critical-path weight (longest "
                                  "history-weighted path to the final target). Weights come from a "
                                  "previous run's bits_build_stats.json; with no history this is "
                                  "graph depth. On by default; does not affect what is built or any "
                                  "hash."))
  build_parser.add_argument("--no-critical-path-schedule", dest="criticalPathSchedule",
                            action="store_const", const=False,
                            help="Disable critical-path ordering; dispatch ready jobs in registration order.")
  build_parser.add_argument("--no-auto-patch", dest="autoPatch", action="store_false", default=True,
                            help=("Do not apply recipe patches: automatically. Patch files are "
                                  "still staged in $SOURCEDIR and exported as $PATCH0..$PATCH_COUNT, "
                                  "but each recipe must apply its own patches (e.g. via the "
                                  "bits_apply_patches helper). Default: patches are auto-applied. "
                                  "A recipe can opt out individually with `auto_patch: false`."))
  # --build-nice / --no-build-nice as a pair of store_true/store_false on the
  # same dest (argparse.BooleanOptionalAction is only available on Python 3.9+).
  # OFF by default: the priority ladder (and its renice watchdog) is opt-in, so
  # the default --builders path is the plain scheduler with no command wrapping
  # or background threads. Enable with --build-nice.
  build_parser.add_argument("--build-nice", dest="buildNice", action="store_true", default=False,
                            help=("Opt in to staggering concurrent --builders jobs across OS 'nice' "
                                  "levels so CPU contention degrades gracefully: one build runs at top "
                                  "priority and the others are progressively backed off, with a watchdog "
                                  "boosting long-running stragglers. Native builds use 'nice'; "
                                  "--docker/podman builds use 'docker run --cpu-shares'. Off by default; "
                                  "only affects --builders > 1. Memory is capped separately (mem_per_job)."))
  build_parser.add_argument("--no-build-nice", dest="buildNice", action="store_false",
                            help="Explicitly disable the --build-nice priority ladder (this is the default).")
  build_parser.add_argument("--build-nice-step", dest="buildNiceStep", type=int, default=5, metavar="N",
                            help=("Nice increment between concurrent build slots when --build-nice is set "
                                  "(slot k -> nice min(k*N, 19)). N=1 gives a gentle 0,1,2,3 ladder; larger "
                                  "values separate slots more aggressively. Default: %(default)d."))
  build_parser.add_argument("--build-nice-boost-after", dest="buildNiceBoostAfter", type=int, default=600,
                            metavar="SECONDS",
                            help=("With --build-nice on native builds, a watchdog renices a build that has "
                                  "been running longer than this (and was niced down) back up to top "
                                  "priority, one straggler at a time, so a long low-priority compile does not "
                                  "drag out the end of the build. Requires privilege to raise priority "
                                  "(root / CAP_SYS_NICE); a no-op otherwise. 0 disables. Default: %(default)d."))
  build_parser.add_argument("--resource-monitoring", dest="resourceMonitoring",
                            action="store_const", const=True, default=None,
                            help=("Enable per-package resource monitoring. Defaults to ON in "
                                  "parallel mode (--builders > 1)."))
  build_parser.add_argument("--no-resource-monitoring", dest="resourceMonitoring",
                            action="store_const", const=False,
                            help="Disable per-package resource monitoring even when --builders > 1.")
  build_parser.add_argument("--resources", dest="resources", default=None,
                            help="JSON files containing resources utilization of packages.")
  build_parser.add_argument("--auto-resources", dest="autoResources", action="store_true",
                            help=("Opt in to the self-tuning resource scheduler for --builders > 1: "
                                  "auto-load the build-stats file a previous run left behind and use it "
                                  "to gate how many build jobs start concurrently, and auto-enable "
                                  "resource monitoring to refresh it. Off by default; concurrency is then "
                                  "bounded only by --builders. Explicit --resources / --resource-monitoring "
                                  "still work without this flag."))
  build_parser.add_argument("-u", "--fetch-repos", dest="fetchRepos", action="store_true",
                            help=("Fetch updates to repositories in MIRRORDIR. Required but nonexistent "
                                  "repositories are always cloned, even if this option is not given."))

  build_parser.add_argument("--no-local", dest="noDevel", metavar="PACKAGE", default=[], action="append",
                            help=("Do not pick up the following packages from a local checkout. "
                                  "You can specify this option multiple times or separate "
                                  "multiple arguments with commas."))
  build_parser.add_argument("--force-tracked", dest="forceTracked", default=False, action="store_true",
                            help=("Do not pick up any packages from a local checkout. "))
  build_parser.add_argument("--plugin", dest="plugin", default="legacy", help=("Plugin to use to do the actual build. "))
  build_parser.add_argument("--disable", dest="disable", default=[], metavar="PACKAGE", action="append",
                            help=("Do not build %(metavar)s and all its (unique) dependencies. "
                                  "You can specify this option multiple times or separate "
                                  "multiple arguments with commas."))
  build_parser.add_argument("--force-rebuild", default=[], metavar="PACKAGE", action="append",
                            help=("Always rebuild the following packages from scratch, even if "
                                  "they were built before. Specifying a package here has the "
                                  "same effect as adding 'force_rebuild: true' to its recipe "
                                  "in CONFIGDIR. You can specify this option multiple times or "
                                  "separate multiple arguments with commas."))
  build_parser.add_argument("--annotate", default=[], action="append", metavar="PACKAGE=COMMENT",
                            help=("Store COMMENT in the build metadata for PACKAGE. This option "
                                  "can be given multiple times, if you want to store comments "
                                  "in multiple packages. The comment will only be stored if "
                                  "PACKAGE is compiled or downloaded during this run; if it "
                                  "already exists, this does not happen."))
  build_parser.add_argument("--only-deps", dest="onlyDeps", default=False, action="store_true",
                            help="Only build dependencies, not the main package (e.g. for caching)")

  build_docker = build_parser.add_argument_group(title="Build inside a container", description="""\
  Builds can be done inside a Docker container, to make it easier to get a
  common, usable environment. The Docker daemon must be installed and running
  on your system. By default, images from alisw/<platform>-builder:latest will
  be used, e.g. alisw/slc8-builder:latest. They will be fetched if unavailable.
  """)
  build_docker.add_argument("--docker", dest="docker", action="store_true",
                            help="Build inside a Docker container.")
  build_docker.add_argument("--docker-image", dest="dockerImage", metavar="IMAGE", default=None,
                            help=("The Docker image to build inside of. Implies --docker. "
                                  "By default, an image is chosen based on the architecture."))
  build_docker.add_argument("--docker-extra-args", metavar="ARGLIST", default="",
                            help=("Command-line arguments to pass to 'docker run'. "
                                  "Passed through verbatim -- separate multiple arguments "
                                  "with spaces, and make sure quoting is correct! Implies --docker. "
                                  "bits always appends --network=host and, unless already present, "
                                  "--cpuset-cpus=<host-online-CPUs> so that make -j "
                                  "see the full host core count. Pass --cpuset-cpus=... explicitly "
                                  "to override the automatic value."))
  build_docker.add_argument("--container-use-workdir", dest="containerUseWorkDir", action="store_true", default=False,
                            help="Use the host work directory inside container. "
                                 "By default it uses /container/bits/sw directory inside container.")
  build_docker.add_argument("--cvmfs-prefix", dest="cvmfsPrefix", default=None, metavar="PATH",
                            help=("When set, bind-mount the host workDir at PATH inside the container "
                                  "so that packages are compiled with PATH as their install prefix. "
                                  "PATH should be the community's CVMFS releases path "
                                  "(e.g. /cvmfs/sft.cern.ch/lcg/releases). "
                                  "With this option the relocation step in 'bits publish' is not needed "
                                  "because the embedded paths are already correct for CVMFS. "
                                  "Implies --container-use-workdir behaviour for the CVMFS mount. "
                                  "Use --no-relocate on 'bits publish' to skip relocation when publishing."))
  build_docker.add_argument("--docker-platform", dest="dockerPlatform", metavar="PLATFORM", default=None,
                            help=("Docker --platform argument for cross-compilation "
                                  "(e.g. linux/arm64, linux/amd64, linux/ppc64le). "
                                  "When not set, bits derives the platform automatically from --architecture: "
                                  "if the target architecture differs from the host, the matching platform is used "
                                  "so that QEMU transparently emulates the target inside the builder container. "
                                  "Pass 'native' to suppress automatic platform injection and always use "
                                  "the daemon-default (host-native) image variant. "
                                  "Requires QEMU binfmt handlers to be registered on the Docker host; "
                                  "see the cross-compilation section in the reference manual."))
  build_docker.add_argument("-v", dest="volumes", action="append", default=[],
                            help=("Additional volume to be mounted inside the Docker container, if one is used. "
                                  "May be specified multiple times. Passed verbatim to 'docker run'."))

  build_sandbox = build_parser.add_argument_group(title="Recipe sandbox", description="""\
  Run each recipe build script inside an isolated sandbox to limit the impact
  of malicious or buggy recipes.  On macOS, the built-in sandbox-exec is used
  (no VM, no overhead).  On Linux, podman (rootless) is used only when --docker
  is active (a nested podman container inside the builder image) or when
  requested explicitly with --sandbox=podman; a plain local Linux build is not
  sandboxed and never invokes podman.
  """)
  build_sandbox.add_argument(
      "--sandbox", dest="sandbox", metavar="MODE", default="off",
      choices=["off", "auto", "podman", "sandbox-exec"],
      help=(
          "Recipe sandbox mode. "
          "'off' (default): no sandboxing. "
          "'auto': sandbox-exec on macOS, nested podman when --docker is active "
          "and podman is present in the builder image (else off), and off on a "
          "local Linux build. "
          "'podman': always use podman (requires --docker or --sandbox-image). "
          "'sandbox-exec': macOS only."
      ),
  )
  build_sandbox.add_argument(
      "--sandbox-image", dest="sandboxImage", metavar="IMAGE", default=None,
      help=(
          "Container image to use for --sandbox=podman when not using --docker. "
          "Implies --sandbox=podman. "
          "Defaults to the --docker image when --docker is set."
      ),
  )
  build_sandbox.add_argument(
      "--sandbox-network", dest="sandboxNetwork", metavar="MODE", default=None,
      choices=["on", "off"],
      help=(
          "Global default for build-time network access inside the sandbox: "
          "'on' blocks outgoing network, 'off' allows it. A recipe's own "
          "`sandbox_network:` field always overrides this. Has no effect where "
          "sandboxing is off (e.g. a plain local Linux build). When not given "
          "on the command line, the value falls back to `sandbox_network:` in "
          "the active defaults (e.g. defaults-release.sh), then to 'on'. "
          "Setting it once in defaults is the recommended way to enable network "
          "for a stack with many recipes that pip-install at build time."
      ),
  )

  build_remote = build_parser.add_argument_group(title="Re-use prebuilt tarballs", description="""\
  Reusing prebuilt tarballs saves compilation time, as common packages need not
  be rebuilt from scratch. rsync://, https://, b3:// and s3:// remote stores
  are recognised. Some of these require credentials: s3:// remotes require an
  ~/.s3cfg; b3:// remotes require AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
  environment variables. A useful remote store is
  'https://s3.cern.ch/swift/v1/alibuild-repo'. It requires no credentials and
  provides tarballs for the most common supported architectures. To archive to
  and reuse from your own bucket, use a b3://<bucket> store (append ::rw to
  --remote-store, or add --write-store) and set the connection via the --s3-*
  options / env vars below; a non-CERN endpoint (AWS, MinIO, Ceph) is supported
  with --s3-endpoint.
  """)
  build_remote.add_argument("--no-remote-store", action="store_true",
                            help="Disable the use of the remote store, even if it is enabled by default.")
  build_remote.add_argument("--remote-store", dest="remoteStore", metavar="STORE", default="",
                            help="""\
                            Where to find prebuilt tarballs to reuse. See above for available remote stores.
                            End with ::rw if you want to upload (in that case, ::rw is stripped and --write-store
                            is set to the same value). May be set to a default store on some
                            architectures; use --no-remote-store to disable it in that case.
                            """)
  build_remote.add_argument("--sign-manifest", dest="signManifest", default=None, metavar="KEY.pem",
                            help=("After the build, sign the build manifest with this Ed25519 private key "
                                  "(PEM) so consumers can verify it for trusted reuse. Public verification "
                                  "keys ship in bits/keys/."))
  build_remote.add_argument("--trust-manifest", dest="trustManifest", default=None, metavar="URL|PATH",
                            help=("Signed release manifest to trust as the authority for remote reuse. Its "
                                  "signature is verified against the shipped trust keys; a remote tarball is "
                                  "reused only if its hash is listed there and its sha256 matches."))
  build_remote.add_argument("--require-signed-reuse", dest="requireSignedReuse", action="store_true", default=None,
                            help=("Fail-closed: only reuse remote tarballs vouched for by a verified signed "
                                  "manifest (see --trust-manifest, auto-derived from the store when unset). "
                                  "This is the DEFAULT — reusing an untrusted remote store is unsafe. Local "
                                  "and CVMFS artifacts are unaffected."))
  build_remote.add_argument("--no-require-signed-reuse", dest="requireSignedReuse", action="store_false",
                            help=("Disable the signed-reuse gate and reuse any remote tarball unverified "
                                  "(insecure; for a store with no signed manifest yet, or bootstrap)."))
  build_remote.add_argument("--trust-groups", dest="trustGroups", default=None, metavar="G1,G2,…",
                            help=("Comma-separated groups to trust in the signed manifest, on top of the "
                                  "always-trusted 'common' base. When omitted, every signed entry is trusted. "
                                  "Use to reuse only your own group's app layer plus the shared base."))
  build_remote.add_argument("--reuse-beacon", dest="reuseBeacon", default=None, metavar="URL",
                            help=("Console base URL to report reused-from-store hashes to (best-effort, "
                                  "fire-and-forget; never blocks or fails the build). Falls back to "
                                  "$BITS_REUSE_BEACON. Only small references are sent, never artifact data."))
  build_remote.add_argument("--monitor", dest="monitor", action="store_true", default=None,
                            help=("Run a best-effort background thread that samples this runner host (load, "
                                  "memory, build filesystem, sw/ size) and the building packages and pushes "
                                  "them to --monitor-url. Never blocks or fails the build; off by default "
                                  "(bits-console enables it). Falls back to the system 'monitor' option."))
  build_remote.add_argument("--no-monitor", dest="monitor", action="store_false",
                            help="Disable the build-host monitor even if the system config enables it.")
  build_remote.add_argument("--monitor-url", dest="monitorUrl", default=None, metavar="URL",
                            help=("VictoriaMetrics/Prometheus base URL for --monitor (POSTs to "
                                  "<URL>/api/v1/import/prometheus). Falls back to $METRICS_URL, then the "
                                  "system 'monitor_url' option."))
  build_remote.add_argument("--monitor-interval", dest="monitorInterval", type=float, default=None, metavar="SECS",
                            help="Host load/memory sample interval for --monitor (default 15s).")
  build_remote.add_argument("--monitor-disk-interval", dest="monitorDiskInterval", type=float, default=None, metavar="SECS",
                            help=("sw/ du + filesystem sample interval for --monitor (default 60s; never "
                                  "more often than --monitor-interval)."))
  build_remote.add_argument("--monitor-instance", dest="monitorInstance", default=None, metavar="NAME",
                            help="Override the per-runner instance label (default <fqdn>-<runner-id>).")
  build_remote.add_argument("--reuse-policy", dest="reusePolicy", choices=["strict", "relaxed"],
                            default=None,
                            help=("CVMFS reuse strictness (ADR-0001). 'strict' (default): reuse only on "
                                  "exact content-hash match; result is publishable. 'relaxed': also graft "
                                  "deployed packages of a blessed release matched by (name, architecture, "
                                  "build_id) for fast local dev; the result is loose-provenance and is "
                                  "refused by the publish path. Falls back to the defaults `reuse_policy:` "
                                  "value, else 'strict'."))
  build_remote.add_argument("--reuse-from", dest="reuseFrom", metavar="PATH|cvmfs", default=None,
                            help=("Reuse deployed components via their published modulefiles at this "
                                  "absolute modules-tree path (distinct from --remote-store, which is the "
                                  "tarball store). The literal 'cvmfs' resolves the exact location from the "
                                  "defaults `system:` layout (module_dir under cvmfs_dir); fails if that is "
                                  "not configured. A trailing '::relaxed' or '::strict' also sets the reuse "
                                  "policy (e.g. 'cvmfs::relaxed'); it must agree with --reuse-policy if both "
                                  "are given."))
  build_remote.add_argument("--build-local", dest="buildLocal", metavar="PKG[,PKG...]", default="",
                            help=("Comma-separated packages to always build locally even under "
                                  "--reuse-policy relaxed (e.g. a package you need patched), rather than "
                                  "grafting them from the base."))
  build_parser.add_argument("--initdotsh-from-modules", dest="initdotshFromModules",
                            action="store_const", const=True, default=None,
                            help=("(default) Set up each build's dependency environment from the "
                                  "dependencies' modulefiles — the single source of truth for runtime "
                                  "AND development — instead of the legacy build-time init.sh. Because "
                                  "this changes build behaviour it is a HASHED input; --legacy-initdotsh "
                                  "restores the pre-modules (aliBuild-compatible) hashes."))
  build_parser.add_argument("--legacy-initdotsh", dest="initdotshFromModules",
                            action="store_const", const=False,
                            help=("Use the legacy build-time init.sh instead of deriving the dependency "
                                  "environment from modulefiles. Produces hashes byte-identical to the "
                                  "pre-modules default, so bits can still reuse alidist tarballs. Also "
                                  "selectable with BITS_LEGACY_INITDOTSH=1 in the environment — the "
                                  "aliBuild compatibility wrapper sets it."))
  build_remote.add_argument("--write-store", dest="writeStore", metavar="STORE", default="",
                            help=("Where to upload newly built packages. Same syntax as --remote-store, "
                                  "except ::rw is not recognised."))
  build_remote.add_argument("--insecure", dest="insecure", action="store_true",
                            help="Don't validate TLS certificates when connecting to an https:// remote store.")
  _add_s3_connection_opts(build_remote)
  build_remote.add_argument("--prefetch-workers", dest="prefetchWorkers", type=int, default=-1,
                            metavar="N",
                            help="""\
                            Start N background threads that pre-download pre-built tarballs and source
                            archives for all packages in the build graph before they are needed, so that
                            downloads overlap the (serial) preparation loop instead of blocking it. A
                            .downloading sentinel file coordinates with the build loop so no file is
                            fetched twice. Default: -1 (auto = min(builders, 4)); 0 disables prefetch.
                            Works in all build modes.
                            """)
  build_remote.add_argument("--parallel-downloads", dest="parallelDownloads", type=int, default=2,
                            metavar="N",
                            help="""\
                            Maximum number of package downloads the build scheduler runs concurrently
                            (the scheduler's "download" task cap, separate from the --builders compile
                            cap). Default: 2. Works with --builders > 1.
                            """)
  build_remote.add_argument("--parallel-sources", dest="parallelSources", type=int, default=1,
                            metavar="N",
                            help="""\
                            Download up to N source URLs in parallel within a single package's sources:
                            list. Default: 1 (sequential, preserving existing behaviour). Works in all
                            build modes.
                            """)

  build_dirs = build_parser.add_argument_group(title="Customise bits directories")
  add_chdir(build_dirs,
            help=("Change to the specified directory before building. "
                  "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  add_work_dir(build_dirs,
               help=("The toplevel directory under which builds should be done and build results "
                     "should be installed. Default '%(default)s'."))
  add_config_dir(build_dirs,
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_search_path(build_dirs)
  build_dirs.add_argument("--reference-sources", dest="referenceSources", metavar="MIRRORDIR",
                          default="%(workDir)s/MIRROR",
                          help=("The directory where reference git repositories will be cloned. "
                                "'%%(workDir)s' will be substituted by WORKDIR. Default '%(default)s'."))

  build_cleanup = build_parser.add_argument_group(title="Cleaning up after building")
  build_cleanup.add_argument("--aggressive-cleanup", dest="aggressiveCleanup", action="store_true",
                             help="Delete as much build data as possible when cleaning up.")
  build_cleanup.add_argument("--no-auto-cleanup", dest="autoCleanup", action="store_false",
                             help="Do not clean up build directories automatically after a build.")

  build_system = build_parser.add_mutually_exclusive_group()
  build_system.add_argument("--prefer-system", "--always-prefer-system", dest="preferSystem",
                            nargs=0, const=True, default=False, action=_WarnAliasAction,
                            help="Always use system packages when compatible.")
  build_system.add_argument("--no-system", dest="noSystem", nargs="?", const="*", default=None, metavar="PACKAGES",
                            help="Never use system packages for the provided, command separated, PACKAGES, even if compatible.")
  build_parser.add_argument(
      "--brew", dest="brew", action="store_true", default=False,
      help=(
          "macOS only: allow recipes that source a system package from Homebrew "
          "to run 'brew install <formula>' automatically when the formula is "
          "missing. Without --brew, such recipes fail with a message telling you "
          "which formula to 'brew install'. Exported to recipe checks as "
          "BITS_BREW=1."
      ),
  )

  build_checksums = build_parser.add_argument_group(
      title="Source and patch checksum verification",
      description="Verify the integrity of downloaded source tarballs and patch files "
                  "declared with an inline checksum suffix (e.g. "
                  "\"https://example.com/foo.tar.gz,sha256:abc123...\"). "
                  "These flags override the checksum_mode / write_checksums fields "
                  "that can be set in a defaults-*.sh profile.")
  build_checksums_mode = build_checksums.add_mutually_exclusive_group()
  build_checksums_mode.add_argument(
      "--check-checksums", dest="checkChecksums", action="store_true", default=False,
      help="Verify checksums during download; warn on mismatch. "
           "Missing declarations are silently ignored. "
           "Overrides checksum_mode in the active defaults profile.")
  build_checksums_mode.add_argument(
      "--enforce-checksums", dest="enforceChecksums", action="store_true", default=False,
      help="Verify checksums during download; abort on mismatch. "
           "Also abort when a source or patch entry carries no checksum declaration. "
           "Overrides checksum_mode in the active defaults profile.")
  build_checksums_mode.add_argument(
      "--print-checksums", dest="printChecksums", action="store_true", default=False,
      help="Compute and print checksums for all sources and patches in "
           "ready-to-paste YAML format after the build completes. "
           "Works for already-compiled packages (reads from the download cache). "
           "Overrides checksum_mode in the active defaults profile.")
  build_checksums.add_argument(
      "--write-checksums", dest="writeChecksums", action="store_true", default=False,
      help="Write (or update) the checksums/<package>.checksum file in the "
           "recipe directory after the build completes. Works for already-compiled "
           "packages (reads from the download cache). Also records the pinned git "
           "commit SHA for source: + tag: packages. Independent of the mode flags "
           "above; overrides write_checksums in the active defaults profile.")

  # Store-integrity flag
  build_parser.add_argument(
      "--store-integrity", dest="storeIntegrity", action="store_true", default=False,
      help=(
          "Enable local tarball integrity ledger.  After each upload the tarball's "
          "SHA-256 is recorded in $WORK_DIR/STORE_CHECKSUMS/.  On every subsequent "
          "recall the digest is recomputed and compared; a mismatch is a fatal error "
          "that indicates the file may have been tampered with in the remote store.  "
          "Disabled by default for backward compatibility.  "
          "Record it with 'bits use build --store-integrity' to enable it persistently."
      ),
  )

  # Provider-policy flag
  build_parser.add_argument(
      "--provider-policy", dest="providerPolicy", metavar="POLICY", default=None,
      help=(
          "Control where each repository-provider's checkout is inserted into "
          "BITS_PATH.  Format: a comma-separated list of NAME:POSITION pairs, "
          "where POSITION is either 'prepend' or 'append' (case-insensitive).  "
          "Example: --provider-policy bits-providers:prepend,myorg:append  "
          "By default every provider uses 'append' (safe mode) regardless of "
          "what its recipe declares.  This flag is the only way to grant a "
          "provider prepend access."
      ),
  )

  # From-manifest flag (build replay)
  build_parser.add_argument(
      "--from-manifest", dest="fromManifest", metavar="FILE", default=None,
      help=(
          "Replay a previous build from a manifest JSON file written by bits.  "
          "The manifest records the requested packages, architecture, defaults, "
          "providers, and per-package checksums.  When this flag is given the "
          "PACKAGE positional argument is optional; if omitted, the packages "
          "listed in the manifest's 'requested_packages' field are built.  "
          "Each recalled tarball is verified against the manifest's "
          "'tarball_sha256' to detect store tampering.  "
          "Example: bits build --from-manifest bits-manifest-latest.json"
      ),
  )

  # Options for clean subcommand
  add_architecture(clean_parser,
                   help=("Clean up build results for this architecture. Default is the current system "
                         "architecture, which is '%(default)s'."))
  clean_parser.add_argument("--aggressive-cleanup", dest="aggressiveCleanup", action="store_true",
                            help="Delete as much build data as possible when cleaning up.")
  clean_dirs = clean_parser.add_argument_group(title="Customise bits directories")
  add_chdir(clean_dirs,
            help=("Change to the specified directory before cleaning up. "
                  "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  add_work_dir(clean_dirs,
               help="The toplevel directory used in previous builds. Default '%(default)s'.")

  # Options for the deps subcommand
  deps_parser.add_argument("package", metavar="PACKAGE",
                           help="Calculate dependency tree for %(metavar)s.")

  add_architecture(deps_parser,
                   help=("Resolve dependencies as if on the specified architecture. When used with "
                         "--docker, use a Docker image for the specified architecture. Default is "
                         "the current system architecture, which is '%(default)s'."))
  add_defaults(deps_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  deps_parser.add_argument("--disable", dest="disable", default=[], metavar="PACKAGE", action="append",
                           help=("Assume we're not building %(metavar)s and all its (unique) dependencies. "
                                 "You can specify this option multiple times or separate multiple arguments "
                                 "with commas."))
  deps_parser.add_argument("-e", dest="environment", action="append", default=[],
                           help="KEY=VALUE binding to add to the environment. May be specified multiple times.")

  deps_graph = deps_parser.add_argument_group(title="Customise graph output")
  deps_graph.add_argument("--neat", dest="neat", action="store_true",
                          help="Produce a graph with transitive reduction.")
  deps_graph.add_argument("--outdot", dest="outdot", metavar="FILE",
                          help="Keep intermediate Graphviz dot file in %(metavar)s.")
  deps_graph.add_argument("--outgraph", dest="outgraph", metavar="FILE",
                          help="Store final output PDF file in %(metavar)s.")

  deps_docker = deps_parser.add_argument_group(title="Use a Docker container", description="""\
  If you're planning to build inside a Docker container, e.g. using bits
  build's --docker option, it may be useful to resolve dependencies inside that
  container as well, as which system packages are picked up may differ.
  """)
  deps_docker.add_argument("--docker", dest="docker", action="store_true",
                           help="Check for available system packages inside a Docker container.")
  deps_docker.add_argument("--docker-image", dest="dockerImage", metavar="IMAGE", default=None,
                           help=("The Docker image to use. Implies --docker. By default, an image "
                                 "is chosen based on the current or selected architecture."))
  deps_docker.add_argument("--docker-extra-args", default="", metavar="ARGLIST",
                           help=("Command-line arguments to pass to 'docker run'. "
                                 "Passed through verbatim -- separate multiple arguments "
                                 "with spaces, and make sure quoting is correct! Implies --docker."))

  add_config_dir(deps_parser.add_argument_group(title="Customise bits directories"),
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_search_path(deps_parser)

  deps_system = deps_parser.add_mutually_exclusive_group()
  deps_system.add_argument("--prefer-system", "--always-prefer-system", dest="preferSystem",
                           nargs=0, const=True, default=False, action=_WarnAliasAction,
                           help="Always use system packages when compatible.")
  deps_system.add_argument("--no-system", dest="noSystem", nargs="?", const="*", default=None, metavar="PACKAGES",
                           help="Never use system packages for PACKAGES, even if compatible.")

  # Options for the doctor subcommand
  doctor_parser.add_argument("packages", metavar="PACKAGE", nargs="*", default=[],
                             help=("Check whether all system requirements of %(metavar)s are satisfied. "
                                   "May be specified multiple times. "
                                   "Optional when --runner is used."))
  add_architecture(doctor_parser,
                   help=("Resolve requirements as if on the specified architecture. When used with "
                         "--docker, use a Docker image for the specified architecture. Default is "
                         "the current system architecture, which is '%(default)s'."))
  add_defaults(doctor_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  doctor_parser.add_argument("--disable", dest="disable", default=[], metavar="PACKAGE", action="append",
                             help=("Assume we're not building %(metavar)s and all its (unique) dependencies. "
                                   "You can specify this option multiple times or separate multiple arguments "
                                   "with commas."))
  doctor_parser.add_argument("-e", dest="environment", action="append", default=[],
                            help="KEY=VALUE binding to add to the build environment. May be specified multiple times.")

  doctor_system = doctor_parser.add_mutually_exclusive_group()
  doctor_system.add_argument("--prefer-system", "--always-prefer-system", dest="preferSystem",
                             nargs=0, const=True, default=False, action=_WarnAliasAction,
                             help="Always use system packages when compatible.")
  doctor_system.add_argument("--no-system", dest="noSystem", nargs="?", const="*", default=None, metavar="PACKAGES",
                             help="Never use system packages for the provided, command separated, PACKAGES, even if compatible.")

  doctor_docker = doctor_parser.add_argument_group(title="Use a Docker container", description="""\
  If you're planning to build inside a Docker container, e.g. using bits
  build's --docker option, it may be useful to resolve dependencies inside that
  container as well, as which system packages are picked up may differ.
  """)
  doctor_docker.add_argument("--docker", dest="docker", action="store_true",
                             help="Check for available system packages inside a Docker container.")
  doctor_docker.add_argument("--docker-image", dest="dockerImage", metavar="IMAGE", default=None,
                             help=("The Docker image to use. Implies --docker. By default, an image "
                                   "is chosen based on the current or selected architecture."))
  doctor_docker.add_argument("--docker-extra-args", metavar="ARGLIST", default="",
                             help=("Command-line arguments to pass to 'docker run'. "
                                   "Passed through verbatim -- separate multiple arguments "
                                   "with spaces, and make sure quoting is correct! Implies --docker."))

  doctor_remote = doctor_parser.add_argument_group(title="Re-use prebuilt tarballs", description="""\
  Reusing prebuilt tarballs saves compilation time, as common packages need not
  be rebuilt from scratch. rsync://, https://, b3:// and s3:// remote stores
  are recognised. Some of these require credentials: s3:// remotes require an
  ~/.s3cfg; b3:// remotes require AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
  environment variables. A useful remote store is
  'https://s3.cern.ch/swift/v1/alibuild-repo'. It requires no credentials and
  provides tarballs for the most common supported architectures.
  """)
  doctor_remote.add_argument("--no-remote-store", action="store_true",
                            help="Disable the use of the remote store, even if it is enabled by default.")
  doctor_remote.add_argument("--remote-store", dest="remoteStore", metavar="STORE", default="", help="""\
  Where to find prebuilt tarballs to reuse. See above for available remote stores.
  End with ::rw if you want to upload (in that case, ::rw is stripped and --write-store
  is set to the same value). May be set to a default store on some
  architectures; use --no-remote-store to disable it in that case.
  """)
  doctor_remote.add_argument("--write-store", dest="writeStore", metavar="STORE", default="",
                            help=("Where to upload newly built packages. Same syntax as --remote-store, "
                                  "except ::rw is not recognised."))
  doctor_remote.add_argument("--insecure", dest="insecure", action="store_true",
                            help="Don't validate TLS certificates when connecting to an https:// remote store.")
  _add_s3_connection_opts(doctor_remote)

  doctor_dirs = doctor_parser.add_argument_group(title="Customise bits directories")
  add_chdir(doctor_dirs,
            help=("Change to the specified directory before doing anything. "
                  "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  add_work_dir(doctor_dirs,
               help=("The toplevel directory under which builds should be done and build results "
                     "should be installed. Default '%(default)s'."))
  add_config_dir(doctor_dirs,
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_search_path(doctor_dirs)

  # Mode flags — apply to --runner, --check-store, and future modes
  doctor_parser.add_argument(
      "--json", dest="json_output", action="store_true", default=False,
      help="Emit a machine-readable JSON report.  "
           "Applies to --runner and --check-store modes.",
  )
  doctor_parser.add_argument(
      "--check-store", dest="checkStore", action="store_true", default=False,
      help=(
          "After resolving the dependency tree, probe the remote store to report "
          "which packages have a pre-built tarball and which will need compilation.  "
          "Requires --remote-store (or a default store for the architecture).  "
          "Makes one HTTP HEAD request per package.  "
          "For branch builds, re-run with 'bits status --fetch-repos --check-store' "
          "for exact hashes."
      ),
  )

  doctor_runner = doctor_parser.add_argument_group(
      title="Runner environment validation (--runner mode)",
      description=(
          "When --runner is given, bits doctor validates the full build-runner "
          "environment — compiler, git, Docker daemon, podman/sandbox, QEMU binfmt "
          "handlers, CVMFS mounts, disk space, and remote-store reachability — "
          "instead of checking package system requirements.  "
          "The PACKAGE positional argument is optional in this mode."
      ),
  )
  doctor_runner.add_argument(
      "--runner", dest="runner", action="store_true", default=False,
      help="Validate the full build-runner environment.  "
           "May be combined with --json for machine-readable output.",
  )
  doctor_runner.add_argument(
      "--cvmfs-repos", dest="cvmfsRepos", metavar="PATH", action="append", default=[],
      help=("CVMFS repository path to check (e.g. /cvmfs/alice.cern.ch).  "
            "May be specified multiple times.  "
            "Can also be set as $BITS_CVMFS_REPOS (comma-separated)."),
  )
  doctor_runner.add_argument(
      "--min-disk", dest="minDisk", type=float, default=10.0, metavar="GIB",
      help="Minimum free disk space in GiB expected in --work-dir.  "
           "A lower value triggers a WARN, not a FAIL.  Default: %(default)s.",
  )
  doctor_runner.add_argument(
      "--prepub-url", dest="prepubUrl", default=None, metavar="URL",
      help=("When set, probe GET <URL>/api/v1/health to verify that the "
            "cvmfs-prepub service is reachable and healthy.  "
            "Required only for communities that use the cvmfs-prepub "
            "direct-upload path (--prepub-url on bits publish).  "
            "Example: https://prepub.example.org:8080"),
  )

  # Options for the brew subcommand
  add_architecture(brew_parser,
                   help=("Generate the Brewfile for the specified architecture. Only recipes whose "
                         "prefer_system matches this architecture are included. Default '%(default)s'."))
  add_defaults(brew_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  brew_parser.add_argument("-o", "--output", dest="output", metavar="FILE", default=None,
                           help=("Write the Brewfile to %(metavar)s. Use '-' for stdout. "
                                 "Default: <CONFIGDIR>/macos/Brewfile (next to the recipes, "
                                 "which are the source of truth)."))
  brew_parser.add_argument("--check", dest="check", action="store_true", default=False,
                           help=("Do not write; exit non-zero if FILE is missing or differs from what "
                                 "would be generated (for CI / pre-commit)."))
  add_config_dir(brew_parser,
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_chdir(brew_parser,
            help=("Change to the specified directory before doing anything. "
                  "Alternatively, set BITS_CHDIR. Default '%(default)s'."))

  # Options for the init subcommand
  init_parser.add_argument("pkgname", nargs="?", default="", metavar="PACKAGE",
                           help="Package to clone locally. One of the packages in CONFIGDIR.")
  add_architecture(init_parser,
                   help=("Parse defaults using the specified architecture. Default is "
                         "the current system architecture, which is '%(default)s'."))

  add_defaults(init_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  init_parser.add_argument("-z", "--devel-prefix", dest="develPrefix", default=".",
                           help=("Directory under which to clone the repository of build recipes. "
                                 "See also: -c/--config-dir. Default '%(default)s'."))

  init_parser.add_argument("--dist", metavar="[USER/REPO@]BRANCH", dest="dist", default="",
                           type=bits_string,
                           help=("Download the given repository containing build recipes into "
                                 "CONFIGDIR. Syntax: [user/repo@]branch or [url@]branch. The "
                                 "default repo is 'alisw/alidist; the default branch is the "
                                 "repository's main branch."))

  init_dirs = init_parser.add_argument_group(title="Customise bits directories")
  add_chdir(init_dirs,
            help=("Change to the specified directory before doing anything. "
                  "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  add_work_dir(init_dirs,
               help=("The toplevel directory under which builds should be done and "
                     "build results should be installed. Default '%(default)s'."))
  add_config_dir(init_dirs, default="%(prefix)salidist",
                 help=("The directory where build recipes will be placed. '%%(prefix)s' will "
                       "be replaced with 'DEVELPREFIX/'. Default '%(default)s'."))
  init_dirs.add_argument("--reference-sources", dest="referenceSources", metavar="MIRRORDIR",
                         default="%(workDir)s/MIRROR",
                         help=("The directory where reference git repositories will be cloned. "
                               "'%%(workDir)s' will be substituted by WORKDIR. Default '%(default)s'."))

  # Options recorded as a `bits use` profile (config mode: no PACKAGE given)
  init_cfg = init_parser.add_argument_group(
      title="Persistent configuration (bits use)",
      description="With no PACKAGE, 'bits init' records the supplied options as a "
                  "'bits use' profile (./.bitsuse or a ~/.bits/use record) so you do not "
                  "repeat them on every build, then exits. --architecture goes to [common], "
                  "the rest to [build]. organisation/providers have no build flag — set "
                  "$BITS_ORGANISATION / $BITS_PROVIDERS for those.")
  init_cfg.add_argument("--providers", dest="providers", default=None, metavar="URL",
                        help="URL of the bits-providers repository. Has no build-time flag; "
                             "set the BITS_PROVIDERS environment variable instead.")
  init_cfg.add_argument("--remote-store", dest="initRemoteStore", default=None, metavar="URL",
                        help="Binary store to fetch pre-built tarballs from (saved as "
                             "'--remote-store' in the [build] profile).")
  init_cfg.add_argument("--write-store", dest="initWriteStore", default=None, metavar="URL",
                        help="Binary store to upload newly-built tarballs to (saved as "
                             "'--write-store' in the [build] profile).")
  init_cfg.add_argument("--organisation", dest="organisation", default=None, metavar="NAME",
                        help="Organisation selecting the registry/provider 'home' repo. Has no "
                             "build-time flag; set the BITS_ORGANISATION environment variable "
                             "instead (the aliBuild wrapper sets it).")

  # version takes no options; the architecture is auto-detected for display.

  # Options for the publish command
  publish_parser.add_argument("package", metavar="PACKAGE", nargs="?", default=None,
                              help="Name of the package to publish. With --release-view, optional: names "
                                   "the release's top package to pick its build_id when the build area "
                                   "holds more than one.")
  publish_parser.add_argument("version", metavar="VERSION", nargs="?", default=None,
                              help="Version (and optional revision) to publish. Defaults to the latest build.")
  publish_parser.add_argument("--release-view", "--view", dest="publishView", metavar="NAME",
                              default=None, action=_WarnAliasAction,
                              help="Instead of a package, publish the merged VIEW for a release to "
                                   "<cvmfs-target>/Views/NAME-<build_id>/<arch>/. The build_id is read "
                                   "from the packages' .meta.json, not given here.")
  publish_parser.add_argument("--cvmfs-target", dest="cvmfsTarget", required=False, metavar="PATH",
                              help="Absolute path the package will occupy on CVMFS (e.g. /cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9). With --release-view, the CVMFS root the Views/ tree lives under.")
  publish_parser.add_argument("--module-target", dest="moduleTarget", metavar="PATH", default=None,
                              help="CVMFS path of the separate modules tree. When given (prepub path), "
                                   "the package's etc/modulefiles are tar'd and published as an "
                                   "independent job here, since modulefiles live in a different tree "
                                   "(module_dir) from the payload — so they are installed even with "
                                   "--no-relocate.")
  # --spool is required for the legacy rsync-to-spool path; omit it when using --prepub-url.
  publish_parser.add_argument("--spool", dest="spool", default=None, metavar="[USER@HOST:]PATH",
                              help=("Ingestion spool root.  Either a local directory or a remote rsync "
                                    "target (user@host:/path).  Required unless --prepub-url is given."))
  add_work_dir(publish_parser,
               help="bits work directory containing the installed packages. Default: %(default)s.")
  add_architecture(publish_parser,
                   help="Target architecture. Default: %(default)s.")
  publish_parser.add_argument("--scratch-dir", dest="scratchDir", default=None, metavar="DIR",
                              help="Directory for the temporary CVMFS working copy. Defaults to a system temp dir.")
  publish_parser.add_argument("--rsync-opts", dest="rsyncOpts", default=None, metavar="OPTS",
                              help="Extra options passed verbatim to rsync (e.g. '-e \"ssh -i key\"').  Legacy spool path only.")
  publish_parser.add_argument("--no-relocate", dest="noRelocate", action="store_true", default=False,
                              help=("Skip the relocation step. Use this when the package was built "
                                    "directly at its final CVMFS path (--cvmfs-prefix on bits build), "
                                    "so all embedded paths are already correct."))
  publish_parser.add_argument("--to", dest="publishTo", default=None,
                              choices=["s3", "cvmfs", "both"],
                              help=("Where to publish: 's3' (upload to the write store for reuse), "
                                    "'cvmfs' (via --spool/--prepub-url), or 'both'. Default: 'cvmfs' "
                                    "when --cvmfs-target is given (backward compatible), else 's3'."))
  publish_parser.add_argument("--write-store", dest="writeStore", default="", metavar="STORE",
                              help=("S3 write store for '--to s3' (e.g. b3://<bucket> or s3://<bucket>). "
                                    "Falls back to WRITE_STORE / BITS_WRITE_STORE in the environment."))
  publish_parser.add_argument("--from-manifest", dest="fromManifest", nargs="?",
                              const="latest", default=None, metavar="MANIFEST",
                              help=("Bulk-upload every package in a build manifest to the S3 store. "
                                    "This is the default when no PACKAGE is given, so bare "
                                    "'bits publish' uploads the latest manifest. Optionally give a "
                                    "manifest file path; 'latest' (default) uses the newest under "
                                    "WORKDIR/MANIFESTS. Use --store to pick the target."))
  add_remote_store(publish_parser, dest="publishStore",
                   help=("S3 store URL/bucket for --from-manifest. Accepts an https URL "
                         "(https://<host>/<bucket>), b3://<bucket>, or s3://<bucket>. "
                         "Default: %(default)s"))
  publish_parser.add_argument("--certify", dest="certify", action="store_true", default=False,
                              help=("After a successful upload, open a merge request in the manifests repo "
                                    "adding this build's manifest under manifests/<group>/. CI validates the "
                                    "MR author is an admin, signs the common manifest, and publishes it. "
                                    "Uses the GitLab API + your PAT (works even with SSH push)."))
  publish_parser.add_argument("--certify-group", dest="certifyGroup", metavar="GROUP", default=None,
                              help=("Group directory to submit the manifest to (manifests/<group>/). Implies "
                                    "--certify. Defaults to `system: certify_group:` in the active defaults, "
                                    "so a configured community can just run `bits publish`."))
  publish_parser.add_argument("--no-certify", dest="noCertify", action="store_true", default=False,
                              help="Never open a certification MR, even if defaults configure it.")
  publish_parser.add_argument("--manifests-remote", dest="manifestsRemote", metavar="GIT_URL", default=None,
                              help=("Git remote of the bits-manifests project, e.g. "
                                    "ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git. Only the host + "
                                    "path are used (to build the HTTPS API URL). Defaults to "
                                    "`system: manifests_remote:` in the active defaults."))
  publish_parser.add_argument("--certify-ref", dest="certifyRef", metavar="REF", default=None,
                              help="Target branch of the certification MR. Default: the repo's default branch.")
  publish_parser.add_argument("--gitlab-token", dest="gitlabToken", metavar="PAT", default=None,
                              help=("GitLab PAT to trigger certification (default: $BITS_CERTIFIER_TOKEN / "
                                    "$GITLAB_TOKEN / ~/.bits/gitlab-token)."))
  publish_parser.add_argument("--certifier", dest="certifier", metavar="USER", default=None,
                              help=("Record USER as certified_by in the submitted manifest (audit trail in the "
                                    "manifests-repo history). Use when the MR is opened by a bot on behalf of a "
                                    "human whose authority was already verified (e.g. bits-console). Defaults to "
                                    "$GITLAB_USER_LOGIN."))

  # cvmfs-prepub direct-upload path (replaces the spool + bits-ingest + bits-publisher flow).
  _prepub = publish_parser.add_argument_group(
      "cvmfs-prepub direct upload",
      "Upload the package directly to a running cvmfs-prepub service over HTTPS, "
      "bypassing the rsync-to-spool pipeline.  Requires cvmfs-prepub ≥ 0.1.0.",
  )
  _prepub.add_argument("--prepub-url", dest="prepubUrl", default=None, metavar="URL",
                       help=("Base URL of the cvmfs-prepub API (no trailing slash), e.g. "
                             "https://prepub.example.org:8080.  When set, --spool is not required."))
  _prepub.add_argument("--prepub-token", dest="prepubToken", default=None, metavar="TOKEN",
                       help=("Bearer token for the cvmfs-prepub API.  If omitted the value of the "
                             "PREPUB_API_TOKEN environment variable is used."))
  _prepub.add_argument("--prepub-repo", dest="prepubRepo", default=None, metavar="REPO",
                       help=("CVMFS repository name to pass to the API, e.g. software.cern.ch.  "
                             "Derived automatically from --cvmfs-target when not specified."))
  _prepub.add_argument("--prepub-path", dest="prepubPath", default=None, metavar="SUBPATH",
                       help=("Lease sub-path relative to the repository root, e.g. atlas/24.0 "
                             "(no leading slash).  Derived automatically from --cvmfs-target "
                             "when not specified."))
  _prepub.add_argument("--prepub-webhook", dest="prepubWebhook", default=None, metavar="URL",
                       help="Optional webhook URL that cvmfs-prepub POSTs to on job completion.")
  _prepub.add_argument("--prepub-poll-interval", dest="prepubPollInterval", type=int,
                       default=10, metavar="SEC",
                       help="Seconds between status polls while waiting for the job.  Default: 10.")
  _prepub.add_argument("--prepub-timeout", dest="prepubTimeout", type=int,
                       default=1800, metavar="SEC",
                       help="Total seconds to wait for the job to reach a terminal state.  Default: 1800.")
  _prepub.add_argument("--prepub-no-verify-tls", dest="prepubNoVerifyTls", action="store_true",
                       default=False,
                       help="Disable TLS certificate verification (self-signed certs / dev mode only).")
  _prepub.add_argument("--prepub-bearer-auth", dest="prepubBearerAuth", action="store_true",
                       default=False,
                       help=("Send the token as 'Authorization: Bearer' instead of signing the "
                             "request. Only for a cvmfs-prepub running auth_mode=bearer; the "
                             "secret then travels on every request, so anyone who observes one "
                             "holds publish rights until it is rotated. By default each request "
                             "carries a per-request HMAC and the secret never leaves this host."))

  # Options for the certify subcommand
  certify_parser.add_argument("manifests", metavar="MANIFEST", nargs="*", default=None,
                              help=("Build-manifest JSON files or directories to merge. A directory "
                                    "is scanned recursively for *.json. Default: WORKDIR/MANIFESTS."))
  certify_parser.add_argument("-o", "--out", dest="out", metavar="FILE", required=True,
                              help="Path to write the merged common manifest (its .sig is written alongside).")
  certify_parser.add_argument("--key", dest="key", metavar="PEM", required=True,
                              help="Ed25519 private key (PEM) to sign the common manifest with.")
  certify_parser.add_argument("--group", dest="group", metavar="GROUP", default=None,
                              help=("Tag entries that lack a group with GROUP, so the consumer trust filter "
                                    "(--trust-groups) can scope reuse. Use 'common' for the shared base layer."))
  certify_parser.add_argument("--require-approval", dest="requireApproval", action="store_true", default=False,
                              help=("Refuse to sign unless a listed group admin approved the merge request "
                                    "(read from the forge — GitLab CI env). Defence-in-depth over CODEOWNERS."))
  certify_parser.add_argument("--admins", dest="admins", metavar="FILE", default=None,
                              help=("Admin policy file: overall admins ('@handle' or '* @handle' lines) "
                                    "plus per-group admins ('<group> @handle'). Overall admins can "
                                    "approve/override any group."))
  certify_parser.add_argument("--changed-groups", dest="changedGroups", metavar="G1,G2", default=None,
                              help=("Restrict the approval re-check to these groups (the ones changed in "
                                    "this MR; e.g. from a git diff). Default: every group present."))
  certify_parser.add_argument("--architectures", dest="architectures", metavar="A1,A2", default=None,
                              help=("Certify only these platforms: merge, store-validate and sign only "
                                    "BOMs of these effective architectures ('shared' is one too), leaving "
                                    "other platforms' signed manifests untouched. A listed platform whose "
                                    "BOMs are all gone gets an EMPTY signed manifest (revocation). "
                                    "Default: every architecture present in the manifests."))
  certify_parser.add_argument("--certifier", dest="certifier", metavar="USERNAME", default=None,
                              help=("GitLab username of the already-authenticated initiator (default: "
                                    "$GITLAB_USER_LOGIN, which GitLab sets for an API-triggered pipeline). "
                                    "Must be an authorised admin; recorded as certified_by. No API call."))
  certify_parser.add_argument("--certifier-token", dest="certifierToken", metavar="PAT", default=None,
                              help=("A GitLab PAT that identifies the initiating admin (GET /user). When "
                                    "given (or $BITS_CERTIFIER_TOKEN), that authenticated identity must be "
                                    "an authorised admin and is recorded as certified_by, instead of "
                                    "reading MR approvals."))
  certify_parser.add_argument("--valid-days", dest="validDays", type=int, default=None, metavar="DAYS",
                              help=("Stamp an 'expires' DAYS from now into the signed manifest; consumers "
                                    "fail closed once it is past (offline anti-replay). Default: no expiry."))
  certify_parser.add_argument("--source-commit", dest="sourceCommit", metavar="SHA", default=None,
                              help="Record the certified manifests-repo commit SHA (default: $CI_COMMIT_SHA).")
  add_remote_store(certify_parser, dest="certifyStore",
                   help=("S3 store URL/bucket to validate hashes against. Accepts https, "
                         "b3://<bucket>, or s3://<bucket>. Default: %(default)s"))
  certify_parser.add_argument("--no-store-check", dest="noStoreCheck", action="store_true", default=False,
                              help=("Skip validating each hash against the store before signing. "
                                    "Only for offline dry merges; a real certification must verify the store."))
  add_work_dir(certify_parser,
               help="bits work directory (source of MANIFESTS when no MANIFEST is given). Default: %(default)s.")
  add_architecture(certify_parser,
                   help="Architecture for store-path resolution. Default: %(default)s.")

  # Options for the compliance subcommand
  compliance_parser.add_argument("packages", metavar="PACKAGE", nargs="*", default=[],
                                 help=("Audit the dependency closure of %(metavar)s (group mode): recipe "
                                       "repositories are discovered exactly as bits build does — config dir, "
                                       "defaults profile, repository providers — and only the resolved closure "
                                       "is audited. Typically the group's meta-package(s), e.g. 'externals "
                                       "generators'. Without %(metavar)s, one recipe directory is scanned "
                                       "(--recipes, default the current directory)."))
  add_config_dir(compliance_parser,
                 help="The directory containing build recipes (group mode). Default '%(default)s'.")
  add_search_path(compliance_parser)
  add_architecture(compliance_parser,
                   help=("Resolve the closure as if on %(metavar)s (group mode). Default is the "
                         "current system architecture, '%(default)s'."))
  add_defaults(compliance_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh (group mode).")
  compliance_parser.add_argument("--disable", dest="disable", default=[], metavar="PACKAGE", action="append",
                                 help=("Assume we're not building %(metavar)s and all its (unique) dependencies "
                                       "(group mode). Repeat or comma-separate."))
  compliance_parser.add_argument("--recipes", dest="recipesDir", metavar="DIR", default=None,
                                 help=("Recipe repository to audit (a directory of *.sh recipes, "
                                       "e.g. an lcg.bits checkout). Default: the current directory."))
  add_remote_store(compliance_parser, dest="complianceStore",
                   help=("S3 store to audit against the recipe flags. Accepts https, "
                         "b3://<bucket>, or s3://<bucket>. Default: %(default)s"))
  compliance_parser.add_argument("--no-store-check", dest="noStoreCheck", action="store_true", default=False,
                                 help="Audit the recipes only; skip the store walk and the public-access probe.")
  add_work_dir(compliance_parser,
               help="bits work directory (scratch for the store client). Default: %(default)s.")
  compliance_parser.add_argument("--enforce", dest="enforce", action="store_true", default=False,
                                 help=("ADMIN: remove non-compliant packages from the store — delete their "
                                       "TARS objects, rev-index markers and SOURCES archives, rewrite the "
                                       "per-build BOMs without them, and (with --key) re-certify the affected "
                                       "architectures. Requires S3 write credentials. Combine with --dry-run "
                                       "to preview every action first."))
  compliance_parser.add_argument("--dry-run", dest="dryRun", action="store_true", default=False,
                                 help="With --enforce: print every deletion/rewrite without touching anything.")
  compliance_parser.add_argument("--key", dest="enforceKey", metavar="PEM", default=None,
                                 help=("With --enforce: Ed25519 release key to re-sign the affected "
                                       "architectures' common manifests after the purge. Without it the next "
                                       "CI certification heals them (removed objects are dropped as missing)."))

  # Options for the gc subcommand
  gc_parser.add_argument("--trust-manifest", dest="trustManifest", required=True, metavar="PATH",
                         help="Signed common manifest whose hashes are the GC roots. Must verify.")
  add_remote_store(gc_parser, dest="gcStore",
                   help="S3 store URL/bucket to sweep. Default: %(default)s")
  add_architecture(gc_parser,
                   help="Architecture store tree to sweep. Default: %(default)s.")
  add_work_dir(gc_parser,
               help="bits work directory (for the S3 client). Default: %(default)s.")
  gc_parser.add_argument("--grace-days", dest="graceDays", type=float, default=7.0, metavar="DAYS",
                         help=("Never sweep an object younger than DAYS, so artifacts from an in-flight "
                               "build not yet in any signed manifest are not raced away. Default: %(default)s."))
  gc_parser.add_argument("--allow-empty", dest="allowEmpty", action="store_true", default=False,
                         help="Permit sweeping when the verified manifest has zero roots (dangerous).")
  gc_parser.add_argument("-n", "--dry-run", dest="dryRun", action="store_true", default=False,
                         help="Report what would be swept without deleting anything.")

  # Options for the store-stats subcommand
  add_remote_store(store_stats_parser, dest="storeStatsStore",
                   help=("S3 store URL/bucket to summarise. Accepts https, b3://<bucket>, "
                         "or s3://<bucket>. Default: %(default)s"))
  store_stats_parser.add_argument("--manifests", dest="manifests", metavar="PATH", nargs="*", default=None,
                                  help=("Build-manifest JSON files/directories that attribute hashes to a "
                                        "build (manifest). Default: WORKDIR/MANIFESTS."))
  store_stats_parser.add_argument("--trust-manifest", dest="trustManifest", metavar="PATH", default=None,
                                  help=("Comma-separated signed common manifests; their verified 'sources' "
                                        "mark which builds are signed. Optional (unset ⇒ all unsigned)."))
  store_stats_parser.add_argument("--tars-prefix", dest="tarsPrefix", metavar="PREFIX", default="TARS/",
                                  help="Store root prefix under which <arch>/store/... lives. Default: %(default)s")
  store_stats_parser.add_argument("-o", "--out", dest="out", metavar="FILE", default="store.json",
                                  help="Path to write the store document. Default: %(default)s")
  store_stats_parser.add_argument("--monitor-url", dest="monitorUrl", metavar="URL", default=None,
                                  help="Also POST Prometheus gauges here (falls back to $METRICS_URL).")
  add_work_dir(store_stats_parser,
               help="bits work directory (S3 client + default MANIFESTS). Default: %(default)s.")
  add_architecture(store_stats_parser,
                   help="Architecture for store-path resolution. Default: %(default)s.")

  # Options for the cleanup subcommand
  add_work_dir(cleanup_parser,
               help="Persistent bits work directory to clean. Default: %(default)s.")
  add_architecture(cleanup_parser,
                   help="Architecture sub-directory to scan. Default: %(default)s.")
  cleanup_parser.add_argument("--max-age", dest="maxAgeDays", type=float, default=7.0, metavar="DAYS",
                              help=("Evict packages whose sentinel has not been touched in more than "
                                    "DAYS days. Default: %(default)s. Set to 0 to disable age-based "
                                    "eviction (only disk-pressure mode runs)."))
  cleanup_parser.add_argument("--min-free", dest="minFreeGb", type=float, default=None, metavar="GIB",
                              help=("When free space on the workDir filesystem is below GIB gibibytes, "
                                    "evict least-recently-used packages until the threshold is met. "
                                    "Disabled by default; set a value to enable disk-pressure eviction."))
  cleanup_parser.add_argument("--disk-pressure-only", dest="diskPressureOnly", action="store_true",
                              default=False,
                              help="Run only disk-pressure eviction; skip age-based eviction.")
  cleanup_parser.add_argument("--retain", dest="retain", action="store_true", default=False,
                              help=("Manifest-rooted retention sweep over ALL architectures in the "
                                    "workDir. Keeps the packages of the newest --keep-builds local build "
                                    "manifests per architecture (the latest iterations, including failed "
                                    "ones) and certified packages NOT yet published to CVMFS; evicts "
                                    "content that is safe upstream — uploaded to the store, in the "
                                    "verified signed manifest AND recorded as published to CVMFS — plus "
                                    "superseded old attempts, orphan store tarballs, BUILD dirs and "
                                    "dangling links. Per-architecture fail-closed: an arch whose signed "
                                    "manifest cannot be fetched/verified is skipped entirely."))
  cleanup_parser.add_argument("--keep-builds", dest="keepBuilds", type=int, default=2, metavar="N",
                              help="With --retain: keep the newest %(metavar)s build manifests per "
                                   "architecture. Default %(default)s.")
  add_remote_store(cleanup_parser, dest="retainStore", default=None,
                   help=("With --retain: remote store to reconstruct the signed common "
                         "manifests from, one per architecture found on disk (plus 'shared') — "
                         "same derivation as bits build's signed reuse. http(s) and b3:///s3:// "
                         "forms accepted."))
  cleanup_parser.add_argument("--trust-manifest", dest="trustManifests", metavar="PATH|URL",
                              action="append", default=[],
                              help=("With --retain: explicit signed common manifest(s) in addition to (or "
                                    "instead of) --store derivation (repeatable; URLs are fetched with "
                                    "their .sig)."))
  cleanup_parser.add_argument("--mark-published-from", dest="markPublishedFrom", metavar="PATH|URL",
                              default=None,
                              help=("With --retain: backfill CVMFS publish markers (.published/) from a "
                                    "cvmfs-status.json publish record before sweeping, so released "
                                    "content becomes evictable."))
  cleanup_parser.add_argument("--grace-days", dest="graceDays", type=float, default=1.0, metavar="DAYS",
                              help="With --retain: never evict anything modified more recently than "
                                   "%(metavar)s days ago. Default %(default)s.")
  cleanup_parser.add_argument("-n", "--dry-run", dest="dryRun", action="store_true", default=False,
                              help="Print what would be evicted without actually removing anything.")

  # Options for the verify subcommand
  verify_parser.add_argument(
      "--from-manifest", dest="fromManifest", required=True, metavar="FILE",
      help="Path to the bits build manifest JSON file to verify against.",
  )
  verify_parser.add_argument(
      "--cvmfs-root", dest="cvmfsRoot", metavar="PATH", default=None,
      help=("Root of the CVMFS tarball store to search first "
            "(e.g. /cvmfs/alice.cern.ch).  "
            "Searched before --work-dir."),
  )
  add_work_dir(verify_parser,
               help=("Local bits work directory containing the TARS/ store.  "
                     "Default '%(default)s'."))
  verify_parser.add_argument(
      "--no-providers", dest="noProviders", action="store_true", default=False,
      help="Skip verification of provider checkout commits.",
  )
  verify_parser.add_argument(
      "--json", dest="json_output", action="store_true", default=False,
      help="Emit a machine-readable JSON report instead of the human-readable table.",
  )

  # Options for the status subcommand
  status_parser.add_argument(
      "pkgname", metavar="PACKAGE", nargs="+",
      help="One or more packages to resolve (including all dependencies).",
  )
  add_defaults(status_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  add_architecture(status_parser,
                   help=("Target architecture. Default is the current system architecture, "
                         "which is '%(default)s'."))
  add_work_dir(status_parser,
               help="The bits work directory to inspect. Default '%(default)s'.")
  add_config_dir(status_parser,
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_search_path(status_parser)
  add_chdir(status_parser,
            help=("Change to the specified directory before doing anything. "
                  "Default '%(default)s'."))
  status_parser.add_argument(
      "--reference-sources", dest="referenceSources", metavar="MIRRORDIR",
      default="%(workDir)s/MIRROR",
      help=("Directory where reference git repos are cached. "
            "'%%(workDir)s' will be substituted. Default '%(default)s'."),
  )
  status_parser.add_argument(
      "--no-local", dest="noDevel", metavar="PACKAGE", default=[],
      action="append",
      help=("Do not treat the named package as a local checkout even if a "
            "matching directory exists in the current directory. "
            "May be repeated or comma-separated."),
  )
  status_parser.add_argument(
      "--force-tracked", dest="forceTracked", default=False, action="store_true",
      help="Ignore all local checkouts; treat every package as remote.",
  )
  status_parser.add_argument(
      "--disable", dest="disable", metavar="PACKAGE", default=[],
      action="append",
      help="Disable the given package(s) from the build. May be repeated.",
  )
  status_parser.add_argument(
      "--force-rebuild", dest="force_rebuild", metavar="PACKAGE", default=[],
      action="append",
      help="Force a rebuild status for the given package(s). May be repeated.",
  )
  status_parser.add_argument(
      "-u", "--fetch-repos", dest="fetchRepos", action="store_true", default=False,
      help=("Fetch / clone reference repositories to populate the ref cache. "
            "Without this flag, only already-cached refs are used; packages "
            "whose refs are not cached are reported as hash_unknown."),
  )
  status_parser.add_argument(
      "--remote-store", dest="remoteStore", metavar="STORE", default="",
      help="Remote binary store URL. Used only when --check-store is given.",
  )
  status_parser.add_argument(
      "--no-remote-store", dest="no_remote_store", action="store_true", default=False,
      help="Disable any remote store (even if a default is configured).",
  )
  status_parser.add_argument(
      "--check-store", dest="checkStore", action="store_true", default=False,
      help=("Probe the remote store to detect tarballs not yet mirrored "
            "locally. Implies a network round-trip per package."),
  )
  status_parser.add_argument(
      "--json", dest="json_output", action="store_true", default=False,
      help="Emit a machine-readable JSON report instead of the human-readable table.",
  )

  # ── cvmfs-path ────────────────────────────────────────────────────────────
  # Resolve a package's CVMFS publish path from the group's templates
  # (defaults-release.sh) without building. Used by the publish pipeline's
  # pre-build namespace reserve so the reserved path matches what the build
  # will record in .meta.json. Authorization stays in the pipeline (it passes
  # --admin/--login); this command only expands templates.
  cvmfs_path_parser = subparsers.add_parser(
      "cvmfs-path",
      help="resolve a package's CVMFS publish path from the group's templates",
      description=(
          "Resolve the CVMFS publish path for a package from the group's path "
          "templates (declared in defaults-release.sh under system:), without "
          "building. Prints the absolute /cvmfs/<repo>/<path>. The publish "
          "pipeline's pre-build reserve uses this so the reserved namespace and "
          "the published path derive from the same single source."
      ),
  )
  cvmfs_path_parser.add_argument(
      "--package", dest="package", metavar="NAME", required=True,
      help="Package name ({pkg} in the template).")
  cvmfs_path_parser.add_argument(
      "--version", dest="version", metavar="VER", default="",
      help="Version/tag segment ({tag}/{version} in the template).")
  cvmfs_path_parser.add_argument(
      "--platform", dest="platform", metavar="PLAT", default="",
      help="Platform ({platform} in the template).")
  cvmfs_path_parser.add_argument(
      "--install-dir", dest="installDir", metavar="DIR", default="",
      help="CVMFS install-dir ({install_dir} in the template).")
  cvmfs_path_parser.add_argument(
      "--kind", dest="kind", choices=["releases", "modules", "shared"],
      default="releases",
      help="Which template to resolve (default: %(default)s).")
  cvmfs_path_parser.add_argument(
      "--admin", dest="admin", action="store_true", default=False,
      help="Resolve the admin (group-prefix) path. Without it, a user path "
           "under <user_prefix>/<login> is resolved (requires --login).")
  cvmfs_path_parser.add_argument(
      "--login", dest="login", metavar="USER", default="",
      help="User login for a non-admin path ({user}; appended to user_prefix).")
  cvmfs_path_parser.add_argument(
      "--prefix", dest="prefix", metavar="ROOT", default="",
      help="Fallback CVMFS root used only when the loaded defaults declare no "
           "system.prefix (for recipe sets that cannot declare their own).")
  add_defaults(cvmfs_path_parser,
               help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  add_architecture(cvmfs_path_parser,
                   help="Target architecture used to load the defaults. Default '%(default)s'.")
  add_config_dir(cvmfs_path_parser,
                 help="The directory containing build recipes. Default '%(default)s'.")
  add_search_path(cvmfs_path_parser)
  add_chdir(cvmfs_path_parser,
            help="Change to the specified directory before doing anything. "
                 "Default '%(default)s'.")
  cvmfs_path_parser.add_argument(
      "--disable", dest="disable", metavar="PACKAGE", default=[], action="append",
      help="Disable the given package(s) when loading defaults. May be repeated.")

  # $BITS_ORGANISATION (the aliBuild wrapper exports it) selects the registry/
  # provider "home" so build/etc. — not just init — pick it up. An explicit
  # --organisation still wins via normal argparse precedence. Injected as a
  # default on the actions that consume it.
  _org_env = os.environ.get("BITS_ORGANISATION")
  if _org_env:
    _org_parsers = [build_parser, clean_parser, cleanup_parser, deps_parser,
                    doctor_parser, init_parser, verify_parser, status_parser]
    for _sp in _org_parsers:
      _sp.set_defaults(organisation=_org_env)
    for _sp in subparsers.choices.values():
      if _sp not in _org_parsers and any(_a.dest == "organisation" for _a in _sp._actions):
        _sp.set_defaults(organisation=_org_env)
  # BITS_PATH is seeded by --search-path (applied after parsing) or an explicit
  # $BITS_PATH; the explicit env var always wins.
  _explicit_bits_path = bool(os.environ.get("BITS_PATH"))

  # Make sure old option ordering behavior is actually still working
  prog = sys.argv[0]
  rest = sys.argv[1:]
  # A bare --parallel/--builders (no following integer) means "auto": insert the
  # count so the nargs='?' optional never swallows the PACKAGE positional (argparse
  # would otherwise read 'ROOT' in `build --parallel ROOT` as the worker count).
  _norm = []
  for _i, _tok in enumerate(rest):
    _norm.append(_tok)
    if _tok in ("--parallel", "--builders"):
      _nxt = rest[_i + 1] if _i + 1 < len(rest) else None
      if _nxt is None or not _nxt.lstrip("+-").isdigit():
        _norm.append(str(BUILDERS_AUTO))
  rest = _norm
  # Subcommands that define their OWN --dry-run/-n: hoisting the flag before
  # the subcommand would let the parent parser consume it, and the subparser's
  # default (False) would then overwrite it — silently turning a dry run into
  # a real one, which for `compliance --enforce` means real deletions.
  # Identify the SUBCOMMAND as the first token that names one (scanning in
  # order): a mere option VALUE that happens to equal 'gc'/'cleanup' (a
  # package named gc, --disable gc, a path segment) appears after the real
  # subcommand and must not flip this guard — matching on set(rest) did.
  _subcommand = next((x for x in rest if x in subparsers.choices), None)
  _own_dry_run = _subcommand in ("cleanup", "compliance", "gc")
  def optionOrder(x):
    # --debug/-d must come before any subcommand so the parent parser sees them.
    # --dry-run/-n is also a top-level flag (for build), BUT some subparsers
    # have their own (see _own_dry_run above).
    if x in ["--debug", "-d"]:
      return 0
    if x in ["-n", "--dry-run"] and not _own_dry_run:
      return 0
#   if x in ["build", "init", "clean", "analytics", "doctor", "deps"]:
    if x in ["build", "init", "clean", "doctor", "deps"]:
      return 1
    return 2
  rest.sort(key=optionOrder)
  sys.argv = [prog] + rest

  # For "bits init" config mode: record which flags were explicit on the CLI so
  # that doInitConfig() can write only the settings the user actually specified.
  # We scan argv AFTER the sort so the subcommand is reliably at index 1.
  _init_explicit_flags: set = set()
  _argv_tail = sys.argv[2:]   # everything after the subcommand name
  for _tok in _argv_tail:
    if _tok.startswith("--"):
      # normalise: "--remote-store" → "remote_store", "--work-dir=sw" → "work_dir"
      _init_explicit_flags.add(_tok.lstrip("-").split("=")[0].replace("-", "_"))
    elif _tok.startswith("-") and len(_tok) == 2:
      # short flags: -w, -a, -C, -z
      _init_explicit_flags.add(_tok[1:])

  args = finaliseArgs(parser.parse_args(), parser)
  # --search-path (CLI) seeds BITS_PATH, but never over an explicit $BITS_PATH
  # the user set in the environment.
  _sp = getattr(args, "searchPath", None)
  if _sp and not _explicit_bits_path:
    os.environ["BITS_PATH"] = str(_sp).strip()
  args._init_explicit = _init_explicit_flags
  return (args, parser)


def matchValidArch(architecture):
  # Recognise an architecture by content rather than by a fixed string layout,
  # so custom `architecture:` templates (ubuntu2510_x86_64, x86_64-ubuntu2510,
  # ...) build without --force-unknown-architecture. We still enforce the same
  # distro/CPU *combinations* the old regex did (e.g. osx pairs only with
  # x86-64/arm64, not ppc64), just independently of order and of the
  # x86-64/x86_64 separator.
  distro = arch_distro_token(architecture)
  machine = arch_machine_token(architecture)
  if not distro or not machine:
    return False
  machine = machine.replace("_", "-")           # canonical dashed form
  family = re.match(r"[a-z]+", distro).group(0)  # strip trailing version digits
  if family == "slc":
    return machine in ("x86-64", "ppc64", "ppc64le", "aarch64")
  if family in ("ubuntu", "ubt", "osx", "fedora"):
    return machine in ("x86-64", "arm64")
  # Other recognised distros (alma, centos, rocky, rhel, el, debian): accept the
  # common server CPUs.
  return machine in ("x86-64", "arm64", "aarch64", "ppc64", "ppc64le")


def _architecture_given_on_cmdline(argv):
  """True iff -a/--architecture was passed explicitly (so a defaults
  `architecture:` template must be ignored).

  Must recognise the same forms argparse accepts, including any unambiguous
  abbreviation of ``--architecture`` (``--arch``, ``--archi``, …): only
  ``--architecture`` starts with ``--arch``, so any such prefix is unambiguous.
  Missing this let a caller passing ``--arch VALUE`` have its architecture
  silently overwritten by the defaults `architecture:` template.
  """
  for tok in argv:
    opt = tok.split("=", 1)[0]
    if opt == "-a" or (opt.startswith("--arch") and "--architecture".startswith(opt)):
      return True
    # bundled short form: -aVALUE (but not a long option)
    if len(tok) > 2 and tok[0] == "-" and tok[1] == "a" and not tok.startswith("--"):
      return True
  return False


def _defaults_architecture_template(args):
  """Return the `architecture:` template string from the merged defaults chain,
  or None. Read defensively: a malformed/unreadable defaults set must not break
  argument parsing (the build flow re-reads and reports defaults errors)."""
  try:
    meta, _ = readDefaults(args.configDir, args.defaults,
                           lambda *a, **k: None, args.architecture)
    val = meta.get("architecture")
    return val if isinstance(val, str) and val.strip() else None
  except Exception:
    return None

ARCHITECTURE_TABLE = """\
On Linux, x86-64:
   RHEL6 / SLC6 compatible: slc6_x86-64
   RHEL7 / CC7 compatible: slc7_x86-64
   RHEL8 / CC8 compatible: slc8_x86-64
   RHEL9 / ALMA9 compatible: slc9_x86-64
   Ubuntu 20.04 compatible: ubuntu2004_x86-64
   Ubuntu 22.04 compatible: ubuntu2204_x86-64
   Ubuntu 24.04 compatible: ubuntu2404_x86-64
   Fedora 33 compatible: fedora33_x86-64
   Fedora 34 compatible: fedora34_x86-64

On Linux, ARM:
   RHEL9 / ALMA9 compatible: slc9_aarch64

On Linux, POWER8 / PPC64 (little endian):
   RHEL7 / CC7 compatible: slc7_ppc64

On Mac, 1-2 latest supported OSX versions:
   Intel: osx_x86-64
   Apple Silicon: osx_arm64
"""

# When updating this variable, also update docs/docs/user.md!
S3_SUPPORTED_ARCHS = "slc7_x86-64", "slc8_x86-64", "ubuntu2004_x86-64", "ubuntu2204_x86-64", "ubuntu2404_x86-64", "slc9_x86-64", "slc9_aarch64"
# Match S3 support by (distro, machine) rather than exact string, so an
# equivalent layout (e.g. ubuntu2404_x86_64) still resolves to the same entry.
_S3_SUPPORTED_ARCH_KEYS = {normalise_arch_key(a) for a in S3_SUPPORTED_ARCHS}

def _parse_flavours(raw):
  """Parse repeated/comma-separated --flavour values into an ordered dict.

  NAME -> "true"; NAME=VALUE -> "VALUE"; !NAME -> "false". Whitespace is
  trimmed; empty tokens are ignored; later entries win on a repeated name.
  """
  result = {}
  for chunk in (raw or []):
    for tok in str(chunk).split(","):
      tok = tok.strip()
      if not tok:
        continue
      if tok.startswith("!"):
        name, value = tok[1:].strip(), "false"
      elif "=" in tok:
        name, _, value = tok.partition("=")
        name, value = name.strip(), value.strip()
      else:
        name, value = tok, "true"
      if name:
        result[name] = value
  return result


def _with_release_base(defaults):
  """Ensure "release" is the base of the defaults chain.

  ``--defaults`` defaults to ``"release"``, so ``release`` is the conceptual
  base of every build. Selecting another profile (e.g. ``--defaults dev4``)
  should *overlay* it on top of release — i.e. behave like ``release::dev4`` —
  rather than replacing it, so stack-wide globals (compiler flags, sandbox
  policy, MACOSX_DEPLOYMENT_TARGET, …) can live once in ``defaults-release``.

  ``release`` is prepended only when not already present anywhere in the chain
  (an explicit ``release::x`` or ``x::release`` is respected as written).
  ``readDefaults`` silently skips a missing ``defaults-release`` file, so stacks
  that do not ship one are unaffected.
  """
  defaults = list(defaults)
  if "release" not in defaults:
    defaults = ["release"] + defaults
  return defaults


def finaliseArgs(args, parser):

  # Nothing to finalise for version, architecture, or verify
  # if args.action in ["version", "analytics", "architecture"]:
  if args.action in ["version", "architecture", "verify", "stats"]:
    return args

  # Minimal finalisation for cvmfs-path: only the defaults profile is loaded
  # (no package/version resolution), so just normalise the defaults + disable
  # lists into the shapes parseDefaults expects.
  if args.action == "cvmfs-path":
    if hasattr(args, "defaults"):
      args.defaults = _with_release_base(args.defaults.split("::"))
    args.disable = normalise_multiple_options(args.disable)
    return args

  # Minimal finalisation for status: normalise lists and expand referenceSources.
  if args.action == "status":
    if hasattr(args, "defaults"):
      args.defaults = _with_release_base(args.defaults.split("::"))
    args.noDevel       = normalise_multiple_options(args.noDevel)
    args.disable       = normalise_multiple_options(args.disable)
    args.force_rebuild = normalise_multiple_options(args.force_rebuild)
    args.referenceSources = args.referenceSources % {"workDir": args.workDir}
    # Repository-provider configuration, mirroring the general path below:
    # `bits status` reports what `bits build` WOULD do, so it must resolve
    # recipes through the same provider repositories — without this it
    # reported provider-supplied packages as missing/hash_unknown.
    _alibuild = os.environ.get("BITS_BRANDING", "").strip().lower() == "alibuild"
    args.bits_providers = (
      os.environ.get("BITS_PROVIDERS")
      or ("" if _alibuild else "https://github.com/bitsorg/bits-providers"))
    if args.bits_providers:
      os.environ.setdefault("BITS_PROVIDERS", args.bits_providers)
    args.provider_policy = _parse_provider_policy(
      getattr(args, "providerPolicy", None) or "")
    return args

  # compliance group mode rides the general finalisation: it needs the
  # defaults split, the disable normalisation and — crucially — the
  # BITS_PROVIDERS / provider_policy resolution below for repo discovery.
  if hasattr(args, "defaults"):
    args.defaults = _with_release_base(args.defaults.split("::"))

  # Resolve --flavour into an ordered {name: value} dict (see _parse_flavours).
  if hasattr(args, "flavours"):
    args.flavours = _parse_flavours(args.flavours)

  # --build-local: comma/space-separated → list (ADR-0001 relaxed-reuse opt-out).
  if hasattr(args, "buildLocal"):
    args.buildLocal = [p for p in (args.buildLocal or "").replace(",", " ").split() if p]

  # ── BITS_PROVIDERS ───────────────────────────────────────────────────────
  # Resolve ``bits_providers``.  Precedence: $BITS_PROVIDERS (explicit override,
  # also settable via 'bits init --providers') then a built-in default. The
  # resolved value is stored on ``args`` and written back to the environment so
  # child processes inherit it. Under the aliBuild wrapper (BITS_BRANDING=aliBuild)
  # the built-in default is off (classic aliBuild uses a local alidist checkout);
  # native `bits` defaults to the provider path.
  _BITS_PROVIDERS_DEFAULT = "https://github.com/bitsorg/bits-providers"
  _alibuild_mode = os.environ.get("BITS_BRANDING", "").strip().lower() == "alibuild"
  _providers_default = "" if _alibuild_mode else _BITS_PROVIDERS_DEFAULT
  args.bits_providers = os.environ.get("BITS_PROVIDERS") or _providers_default
  if args.bits_providers:
    os.environ.setdefault("BITS_PROVIDERS", args.bits_providers)

  # ── provider_policy ──────────────────────────────────────────────────────
  # Effective provider-position policy from the --provider-policy flag, parsed
  # into {name: "prepend"|"append"} for build.py to pass to the provider loader.
  args.provider_policy = _parse_provider_policy(getattr(args, "providerPolicy", None) or "")

  # ── from-manifest (build replay) ─────────────────────────────────────────
  # When --from-manifest is given, the manifest's ``requested_packages`` list
  # is used as the package list so the user does not have to repeat it on the
  # command line.  An explicitly provided PACKAGE argument takes precedence
  # (allows overriding a specific package while reusing the rest of the
  # manifest's configuration).
  from_manifest = getattr(args, "fromManifest", None)
  if from_manifest and args.action == "build":
    import json, os as _os
    if not _os.path.isfile(from_manifest):
      parser.error("--from-manifest: file not found: %s" % from_manifest)
    try:
      with open(from_manifest) as _fh:
        _manifest_data = json.load(_fh)
    except (ValueError, OSError) as _exc:
      parser.error("--from-manifest: cannot read manifest: %s" % _exc)
    # If no packages were given on the command line, fill them in from the
    # manifest so the user can just say: bits build --from-manifest FILE
    if not getattr(args, "pkgname", None):
      args.pkgname = list(_manifest_data.get("requested_packages", []))
      if not args.pkgname:
        parser.error("--from-manifest: manifest has no 'requested_packages'")
    # Store the loaded manifest data on args so doBuild can use it for
    # version pinning and tarball verification.
    args.fromManifestData = _manifest_data

  # ── architecture template (defaults-release.sh) ──────────────────────────
  # Precedence: an explicit --architecture wins and any template is ignored.
  # Otherwise, if a defaults file in the chain defines `architecture:` -- a
  # literal string or a %(os)s / %(machine)s / %(_machine)s template -- the
  # architecture is recomputed from it against the locally detected platform.
  # With neither, the auto-detected string already in args.architecture stands.
  if args.action in ["build", "clean"] and getattr(args, "architecture", None) \
     and hasattr(args, "defaults") and not _architecture_given_on_cmdline(sys.argv):
    tmpl = _defaults_architecture_template(args)
    if tmpl:
      try:
        args.architecture = apply_arch_template(tmpl, detectArchComponents())
      except ValueError as exc:
        parser.error(str(exc))

  # --architecture can be specified in both clean and build.
  if args.action in ["build", "clean"] and not args.architecture:
    parser.error("Cannot determine architecture. Please pass it explicitly.\n\n"
                 + ARCHITECTURE_TABLE)

  if args.action == "build" and not args.forceUnknownArch and not matchValidArch(args.architecture):
    parser.error("Unknown / unsupported architecture: {architecture}.\n\n{table}"
                 "Alternatively, you can use the `--force-unknown-architecture' option."
                 .format(table=ARCHITECTURE_TABLE, architecture=args.architecture))

  if "noDevel" in args:
    args.noDevel = normalise_multiple_options(args.noDevel)
  if "disable" in args:
    args.disable = normalise_multiple_options(args.disable)
  if "force_rebuild" in args:
    args.force_rebuild = normalise_multiple_options(args.force_rebuild)

  if args.action in ["build", "init"]:
    args.referenceSources = args.referenceSources % {"workDir": args.workDir}
    # Do this cleanup as early as possible to avoid false positives due to
    # stale git logs from previous invocations.
    cleanup_git_log(args.referenceSources)

  if args.action in ("build", "doctor", "deps"):
    if args.dockerImage or args.docker_extra_args:
      args.docker = True

    args.docker_extra_args = shlex.split(args.docker_extra_args)
    args.docker_extra_args.append("--network=host")
    # Pin the build container to the full set of online host CPUs so that
    # make -j sees the real core count rather than the cgroup
    # quota inherited from the GitLab runner process.
    # /sys/devices/system/cpu/online gives the kernel-reported online CPU
    # list (e.g. "0-7") which reflects actual hardware, not the caller's
    # cgroup CPU quota.  Only inject if the user hasn't already specified
    # --cpuset-cpus in --docker-extra-args.
    if not any(a.startswith("--cpuset-cpus") for a in args.docker_extra_args):
      args.docker_extra_args.append("--cpuset-cpus=" + _host_online_cpus())

    # Hard memory cap on the build container so that no single build can OOM
    # the HOST (see _docker_memory_args). Skipped when the user passes any
    # --memory* themselves.
    if not any(a.startswith("--memory") for a in args.docker_extra_args):
      args.docker_extra_args.extend(_docker_memory_args())

    if args.docker and args.architecture.startswith("osx"):
      parser.error("cannot use `-a %s` and --docker" % args.architecture)

    if args.docker and commands.getstatusoutput("which docker")[0]:
      parser.error("cannot use --docker as docker executable is not found")

    # If specified, used the docker image requested, otherwise, if running
    # in docker the docker image is given by the first part of the
    # architecture we want to build for.
    if args.docker and not args.dockerImage:
      # Derive the builder image from the distro token wherever it sits in the
      # architecture string (pattern, not positional split), so reordered or
      # underscore-machine layouts still resolve. Fall back to the legacy
      # first-underscore field if no known distro token is recognised.
      distro_token = arch_distro_token(args.architecture) or args.architecture.split("_")[0]
      args.dockerImage = "registry.cern.ch/alisw/%s-builder" % distro_token

    # ── --docker-platform / cross-compilation ─────────────────────────────────
    # Derive the Docker --platform value from --architecture when the user has
    # not set it explicitly.  If the target architecture matches the host we
    # leave it as None (no --platform flag → daemon uses native image variant,
    # zero overhead).  If they differ we inject the matching platform string so
    # that Docker pulls the correct image variant and QEMU transparently
    # emulates the target ISA inside the builder container.
    #
    # The special sentinel value "native" lets users opt out of automatic
    # injection even when cross-compiling (useful on native ARM runners that
    # happen to run an x86-64 bits client, or for testing).
    if args.docker:
      if getattr(args, "dockerPlatform", None) == "native":
        args.dockerPlatform = None
      elif not getattr(args, "dockerPlatform", None):
        from bits_helpers.arch import docker_platform_for_arch, detectArch as _detectArch
        target_plat = docker_platform_for_arch(args.architecture)
        host_plat   = docker_platform_for_arch(_detectArch())
        if target_plat and target_plat != host_plat:
          args.dockerPlatform = target_plat
        else:
          args.dockerPlatform = None

    # --sandbox-image implies --sandbox=podman. Promote from the mode-unset
    # states ("off" is the default, "auto" the legacy default); an explicit
    # "podman"/"sandbox-exec" is left as chosen.
    if getattr(args, "sandboxImage", None) and getattr(args, "sandbox", "off") in ("off", "auto"):
      args.sandbox = "podman"

  if "annotate" in args:
    for comment_assignment in args.annotate:
      if "=" not in comment_assignment:
        parser.error("--annotate takes arguments of the form PACKAGE=COMMENT")
    args.annotate = {
      package: comment
      for package, _, comment
      in (assignment.partition("=") for assignment in args.annotate)
    }


  if args.action in ("build", "doctor"):

    # Store URL from the environment when not set on the CLI. Precedence:
    # CLI > BITS_REMOTE_STORE (runner env) > REMOTE_STORE (CI common) >
    # built-in default. --no-remote-store below still clears it.
    if not args.remoteStore:
      args.remoteStore = os.environ.get("BITS_REMOTE_STORE") or os.environ.get("REMOTE_STORE") or ""
    if not args.writeStore:
      args.writeStore = os.environ.get("BITS_WRITE_STORE") or os.environ.get("WRITE_STORE") or ""

    # Explicit = came from CLI/env. If so it wins over a defaults
    # `system: remote_store:` (applied later in build.py, where defaults load);
    # otherwise system.remote_store overrides the built-in arch default below.
    args.remoteStoreExplicit = bool(args.remoteStore)

    # A public read store is enabled by default on selected platforms. Unlike
    # aliBuild, activating a store does NOT force --no-system (reuse is
    # content-hash addressed); use --no-system for a self-contained build.
    if normalise_arch_key(args.architecture) in _S3_SUPPORTED_ARCH_KEYS and not args.preferSystem and not args.no_remote_store:
      if not args.remoteStore:
        args.remoteStore = "https://s3.cern.ch/swift/v1/alibuild-repo"
    elif args.no_remote_store:
      args.remoteStore = ""

    if args.remoteStore.endswith("::rw") and args.writeStore:
      parser.error("cannot specify ::rw and --write-store at the same time")

    if args.remoteStore.endswith("::rw"):
      args.remoteStore = args.remoteStore[0:-4]
      args.writeStore = args.remoteStore

  if args.action in ["build", "init"]:
    if "develPrefix" in args and args.develPrefix is None:
      if "chdir" in args:
        args.develPrefix = basename(abspath(args.chdir))
      else:
        args.develPrefix = basename(dirname(abspath(args.configDir)))
    if getattr(args, "docker", False):
      args.develPrefix = "{}-{}".format(args.develPrefix, args.architecture) if "develPrefix" in args else args.architecture

  if args.action == "init":
    args.configDir = args.configDir % {"prefix": args.develPrefix + "/"}
  elif args.action == "build":
    # Resource monitoring defaults ON in parallel mode (--builders > 1) and OFF
    # for serial builds, unless the user passed --resource-monitoring /
    # --no-resource-monitoring explicitly (in which case it is True/False here).
    if args.resourceMonitoring is None:
      args.resourceMonitoring = getattr(args, "builders", 1) > 1
    if args.resourceMonitoring:
      try:
        import psutil  # noqa: F401  # availability probe: imported for its ImportError side effect, not used by name
      except Exception:
        args.resourceMonitoring = False
        print("Warning: Unable to use psutil. Disabling resource monitoring")
    pass
  elif args.action == "clean":
    pass
  else:
    pass
  return args
