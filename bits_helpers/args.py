import argparse
from bits_helpers.utilities import detectArch, normalise_multiple_options
from bits_helpers.workarea import cleanup_git_log
import configparser
import multiprocessing

import re
import os
import shlex

import subprocess as commands
from os.path import abspath, dirname, basename, exists
import sys

# Default workdir: fall back on "sw" if env is not set or empty
DEFAULT_WORK_DIR = os.environ.get("BITS_WORK_DIR") or os.environ.get("ALICE_WORK_DIR") or "sw"


def _host_online_cpus():
  """Return the kernel's online-CPU range string for use with --cpuset-cpus.

  Reads /sys/devices/system/cpu/online (e.g. "0-7" or "0-3,5-7"), which
  reflects the actual hardware CPU set regardless of any cgroup CPU quota
  that may be in effect for the calling process.  Falls back to
  ``os.cpu_count()`` on platforms where sysfs is unavailable (macOS, WSL1).

  This value is injected as ``--cpuset-cpus`` into every Docker build
  container so that ``make -j``, makeflow, and similar tools always see the
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

# cd to this directory before start
DEFAULT_CHDIR = os.environ.get("BITS_CHDIR") or "."

# Search order for bits.rc config files (highest priority first).
# Each entry is evaluated at import time so that ~ is expanded once.
_BITS_RC_SEARCH_PATHS = [
    "bits.rc",
    ".bitsrc",
    os.path.expanduser("~/.bitsrc"),
]


def _parse_provider_policy(value: str) -> dict:
  """Parse a ``provider_policy`` string into a ``{provider_name: position}`` dict.

  The format is a comma-separated list of ``name:position`` pairs where
  *position* is either ``"prepend"`` or ``"append"``::

      bits-providers:prepend, myorg-recipes:append

  Provider names are lower-cased for consistent lookup.  Malformed entries
  and unrecognised position values are skipped with a warning printed to
  stderr.  Returns an empty dict for an empty or missing *value*.

  This is the sole parsing point used by both the ``bits.rc`` key
  ``provider_policy`` and the ``--provider-policy`` CLI flag so that
  both inputs share identical validation logic.
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


def _read_bits_rc() -> dict:
  """Return settings from the first bits.rc / .bitsrc / ~/.bitsrc found.

  Only the ``[bits]`` section is returned; all keys are lower-cased.
  Returns an empty dict when no config file is present.

  Example bits.rc::

      [bits]
      providers = https://github.com/org/bits-stdlib.git@stable
      sw_dir    = /opt/sw
  """
  cfg = configparser.ConfigParser()
  for path in _BITS_RC_SEARCH_PATHS:
    if exists(path):
      cfg.read(path)
      break
  return dict(cfg["bits"]) if "bits" in cfg else {}


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
  parser = argparse.ArgumentParser(epilog="""\
  For help about each option, specify --help after the option itself. For
  complete documentation please refer to https://alisw.github.io/alibuild.
  """)

  parser.add_argument("-d", "--debug", dest="debug", action="store_true", help="Enable debug log output")
  parser.add_argument("-n", "--dry-run", dest="dryRun", action="store_true",
                      help="Print what would happen, without actually doing it.")

  subparsers = parser.add_subparsers(dest="action")
  '''
  analytics_parser = subparsers.add_parser("analytics", help="turn on / off analytics",
                                           description="Control analytics state.")
  '''
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

  # Options for the analytics command
  # analytics_parser.add_argument("state", choices=["on", "off"], help="Whether to report analytics or not")

  # Options for the build command
  build_parser.add_argument("pkgname", metavar="PACKAGE", nargs="+",
                            help="One of the packages in CONFIGDIR. May be specified multiple times.")

  build_parser.add_argument("--defaults", dest="defaults", default="release", metavar="DEFAULT",
                            help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")

  build_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
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
  build_parser.add_argument("--builders", dest="builders", type=int, default=1,
                            help=("The number of independent packages to build in parallel. "
                                  "Default is: %(default)d."))
  build_parser.add_argument("--resource-monitoring", dest="resourceMonitoring", action="store_true",
                            help="Enable resource monitoring for each built package.")
  build_parser.add_argument("--resources", dest="resources", default=None,
                            help="JSON files containing resources utilization of packages.")
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
  build_parser.add_argument("--makeflow", default=False, action="store_true",
                            help=("Use makeflow for paralle workflow execution. "))
  build_parser.add_argument("--only-deps", dest="onlyDeps", default=False, action="store_true",
                            help="Only build dependencies, not the main package (e.g. for caching)")
  build_parser.add_argument("--gcc-toolchain", dest="gccToolchain", default=None, metavar="PACKAGE", action="append",
                            help=("Override gcc toolchain version tag"))  

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
                                  "--cpuset-cpus=<host-online-CPUs> so that make -j and makeflow "
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
  of malicious or buggy recipes.  On Linux, podman (rootless) is used; on
  macOS, the built-in sandbox-exec is used (no VM, no overhead).
  When --docker is active, a nested podman container is added inside the
  builder container for an additional isolation layer.
  """)
  build_sandbox.add_argument(
      "--sandbox", dest="sandbox", metavar="MODE", default="auto",
      choices=["off", "auto", "podman", "sandbox-exec"],
      help=(
          "Recipe sandbox mode. "
          "'auto' (default): use podman on Linux if available, "
          "sandbox-exec on macOS, nested podman when --docker is active. "
          "'podman': always use podman (requires --docker or --sandbox-image). "
          "'sandbox-exec': macOS only. "
          "'off': no sandboxing."
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

  build_remote = build_parser.add_argument_group(title="Re-use prebuilt tarballs", description="""\
  Reusing prebuilt tarballs saves compilation time, as common packages need not
  be rebuilt from scratch. rsync://, https://, b3:// and s3:// remote stores
  are recognised. Some of these require credentials: s3:// remotes require an
  ~/.s3cfg; b3:// remotes require AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
  environment variables. A useful remote store is
  'https://s3.cern.ch/swift/v1/alibuild-repo'. It requires no credentials and
  provides tarballs for the most common supported architectures.
  """)
  build_remote.add_argument("--no-remote-store", action="store_true",
                            help="Disable the use of the remote store, even if it is enabled by default.")
  build_remote.add_argument("--remote-store", dest="remoteStore", metavar="STORE", default="",
                            help="""\
                            Where to find prebuilt tarballs to reuse. See above for available remote stores.
                            End with ::rw if you want to upload (in that case, ::rw is stripped and --write-store
                            is set to the same value). Implies --no-system. May be set to a default store on some
                            architectures; use --no-remote-store to disable it in that case.
                            """)
  build_remote.add_argument("--write-store", dest="writeStore", metavar="STORE", default="",
                            help=("Where to upload newly built packages. Same syntax as --remote-store, "
                                  "except ::rw is not recognised. Implies --no-system."))
  build_remote.add_argument("--insecure", dest="insecure", action="store_true",
                            help="Don't validate TLS certificates when connecting to an https:// remote store.")
  build_remote.add_argument("--pipeline", dest="pipeline", action="store_true", default=False,
                            help="""\
                            (Requires --makeflow) Activates Options 1 and 4: split each package's Makeflow
                            rules into three targets (.build, .tar, .upload) so tarball creation and remote
                            upload run concurrently with downstream package builds. Silently ignored without
                            --makeflow. Has no effect when --write-store is not set.
                            """)
  build_remote.add_argument("--prefetch-workers", dest="prefetchWorkers", type=int, default=0,
                            metavar="N",
                            help="""\
                            Start N background threads that pre-download pre-built tarballs and source
                            archives for all packages in the build graph before they are needed. A
                            .downloading sentinel file coordinates with the build loop so no file is
                            fetched twice. Default: 0 (disabled). Works in all build modes.
                            """)
  build_remote.add_argument("--parallel-sources", dest="parallelSources", type=int, default=1,
                            metavar="N",
                            help="""\
                            Download up to N source URLs in parallel within a single package's sources:
                            list. Default: 1 (sequential, preserving existing behaviour). Works in all
                            build modes.
                            """)
  build_remote.add_argument("--makeflow-jobs", dest="makeflowJobs", type=int, default=4,
                            metavar="N",
                            help="""\
                            (Requires --makeflow) Maximum number of build jobs Makeflow runs in parallel
                            on the local machine (passed as --max-local N to makeflow). Each build job
                            itself uses all available CPU cores (controlled by -j / --jobs), so running
                            too many simultaneously causes CPU oversubscription and degrades performance.
                            Default: 4. Set to 0 to let Makeflow use its own default (number of CPU
                            cores, which typically causes severe oversubscription).
                            """)

  build_dirs = build_parser.add_argument_group(title="Customise bits directories")
  build_dirs.add_argument("-C", "--chdir", metavar="DIR", dest="chdir", default=DEFAULT_CHDIR,
                          help=("Change to the specified directory before building. "
                                "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  build_dirs.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR,
                          help=("The toplevel directory under which builds should be done and build results "
                                "should be installed. Default '%(default)s'."))
  build_dirs.add_argument("-c", "--config-dir", dest="configDir", default=os.environ.get("BITS_REPO_DIR","alidist"),
                          help="The directory containing build recipes. Default '%(default)s'.")
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
  build_system.add_argument("--always-prefer-system", dest="preferSystem", action="store_true",
                            help="Always use system packages when compatible.")
  build_system.add_argument("--no-system", dest="noSystem", nargs="?", const="*", default=None, metavar="PACKAGES",
                            help="Never use system packages for the provided, command separated, PACKAGES, even if compatible.")

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
          "May also be enabled persistently with 'store_integrity = true' in bits.rc."
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
          "what its recipe declares.  This flag (or the equivalent bits.rc key "
          "'provider_policy') is the only way to grant a provider prepend "
          "access."
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
  clean_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                            help=("Clean up build results for this architecture. Default is the current system "
                                  "architecture, which is '%(default)s'."))
  clean_parser.add_argument("--aggressive-cleanup", dest="aggressiveCleanup", action="store_true",
                            help="Delete as much build data as possible when cleaning up.")
  clean_dirs = clean_parser.add_argument_group(title="Customise bits directories")
  clean_dirs.add_argument("-C", "--chdir", metavar="DIR", dest="chdir", default=DEFAULT_CHDIR,
                          help=("Change to the specified directory before cleaning up. "
                                "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  clean_dirs.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR,
                          help="The toplevel directory used in previous builds. Default '%(default)s'.")

  # Options for the deps subcommand
  deps_parser.add_argument("package", metavar="PACKAGE",
                           help="Calculate dependency tree for %(metavar)s.")

  deps_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                           help=("Resolve dependencies as if on the specified architecture. When used with "
                                 "--docker, use a Docker image for the specified architecture. Default is "
                                 "the current system architecture, which is '%(default)s'."))
  deps_parser.add_argument("--defaults", dest="defaults", default="release", metavar="DEFAULT",
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

  deps_parser.add_argument_group(title="Customise bits directories") \
             .add_argument("-c", "--config-dir", dest="configDir", default=os.environ.get("BITS_REPO_DIR","alidist"),
                           help="The directory containing build recipes. Default '%(default)s'.")

  deps_system = deps_parser.add_mutually_exclusive_group()
  deps_system.add_argument("--always-prefer-system", dest="preferSystem", action="store_true",
                           help="Always use system packages when compatible.")
  deps_system.add_argument("--no-system", dest="noSystem", nargs="?", const="*", default=None, metavar="PACKAGES",
                           help="Never use system packages for PACKAGES, even if compatible.")

  # Options for the doctor subcommand
  doctor_parser.add_argument("packages", metavar="PACKAGE", nargs="*", default=[],
                             help=("Check whether all system requirements of %(metavar)s are satisfied. "
                                   "May be specified multiple times. "
                                   "Optional when --runner is used."))
  doctor_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                             help=("Resolve requirements as if on the specified architecture. When used with "
                                   "--docker, use a Docker image for the specified architecture. Default is "
                                   "the current system architecture, which is '%(default)s'."))
  doctor_parser.add_argument("--defaults", dest="defaults", default="release", metavar="DEFAULT",
                             help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.")
  doctor_parser.add_argument("--disable", dest="disable", default=[], metavar="PACKAGE", action="append",
                             help=("Assume we're not building %(metavar)s and all its (unique) dependencies. "
                                   "You can specify this option multiple times or separate multiple arguments "
                                   "with commas."))
  doctor_parser.add_argument("-e", dest="environment", action="append", default=[],
                            help="KEY=VALUE binding to add to the build environment. May be specified multiple times.")

  doctor_system = doctor_parser.add_mutually_exclusive_group()
  doctor_system.add_argument("--always-prefer-system", dest="preferSystem", action="store_true",
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
  is set to the same value). Implies --no-system. May be set to a default store on some
  architectures; use --no-remote-store to disable it in that case.
  """)
  doctor_remote.add_argument("--write-store", dest="writeStore", metavar="STORE", default="",
                            help=("Where to upload newly built packages. Same syntax as --remote-store, "
                                  "except ::rw is not recognised. Implies --no-system."))
  doctor_remote.add_argument("--insecure", dest="insecure", action="store_true",
                            help="Don't validate TLS certificates when connecting to an https:// remote store.")

  doctor_dirs = doctor_parser.add_argument_group(title="Customise bits directories")
  doctor_dirs.add_argument("-C", "--chdir", metavar="DIR", dest="chdir", default=DEFAULT_CHDIR,
                           help=("Change to the specified directory before doing anything. "
                                 "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  doctor_dirs.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR,  # TODO: previous default was "workDir".
                           help=("The toplevel directory under which builds should be done and build results "
                                 "should be installed. Default '%(default)s'."))
  doctor_dirs.add_argument("-c", "--config", dest="configDir", default=os.environ.get("BITS_REPO_DIR","alidist"),
                           help="The directory containing build recipes. Default '%(default)s'.")

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
            "Can also be set as 'cvmfs_repos' (comma-separated) in bits.rc."),
  )
  doctor_runner.add_argument(
      "--min-disk", dest="minDisk", type=float, default=10.0, metavar="GIB",
      help="Minimum free disk space in GiB expected in --work-dir.  "
           "A lower value triggers a WARN, not a FAIL.  Default: %(default)s.",
  )

  # Options for the init subcommand
  init_parser.add_argument("pkgname", nargs="?", default="", metavar="PACKAGE",
                           help="Package to clone locally. One of the packages in CONFIGDIR.")
  init_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                           help=("Parse defaults using the specified architecture. Default is "
                                 "the current system architecture, which is '%(default)s'."))

  init_parser.add_argument("--defaults", dest="defaults", default="release", metavar="DEFAULT",
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
  init_dirs.add_argument("-C", "--chdir", metavar="DIR", dest="chdir", default=DEFAULT_CHDIR,
                         help=("Change to the specified directory before doing anything. "
                               "Alternatively, set BITS_CHDIR. Default '%(default)s'."))
  init_dirs.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR,
                         help=("The toplevel directory under which builds should be done and "
                               "build results should be installed. Default '%(default)s'."))
  init_dirs.add_argument("-c", "--config-dir", dest="configDir", default="%(prefix)salidist",
                         help=("The directory where build recipes will be placed. '%%(prefix)s' will "
                               "be replaced with 'DEVELPREFIX/'. Default '%(default)s'."))
  init_dirs.add_argument("--reference-sources", dest="referenceSources", metavar="MIRRORDIR",
                         default="%(workDir)s/MIRROR",
                         help=("The directory where reference git repositories will be cloned. "
                               "'%%(workDir)s' will be substituted by WORKDIR. Default '%(default)s'."))

  # Options for creating / updating bits.rc (config mode: no PACKAGE given)
  init_cfg = init_parser.add_argument_group(
      title="Persistent configuration (bits.rc)",
      description="These options write settings to bits.rc so you do not need to repeat them "
                  "on every 'bits build' invocation. When no PACKAGE is given, 'bits init' "
                  "writes the supplied options to bits.rc and exits.")
  init_cfg.add_argument("--providers", dest="providers", default=None, metavar="URL",
                        help="URL of the bits-providers repository (written as 'providers' in bits.rc). "
                             "Equivalent to the BITS_PROVIDERS environment variable.")
  init_cfg.add_argument("--remote-store", dest="initRemoteStore", default=None, metavar="URL",
                        help="Binary store to fetch pre-built tarballs from (written as 'remote_store' "
                             "in bits.rc). Accepts the same URL formats as 'bits build --remote-store'.")
  init_cfg.add_argument("--write-store", dest="initWriteStore", default=None, metavar="URL",
                        help="Binary store to upload newly-built tarballs to (written as 'write_store' "
                             "in bits.rc). Accepts the same URL formats as 'bits build --write-store'.")
  init_cfg.add_argument("--organisation", dest="organisation", default=None, metavar="NAME",
                        help="Organisation name stored under the 'organisation' key in bits.rc. "
                             "May be used by defaults profiles and recipe tooling.")
  init_cfg.add_argument("--rc-file", dest="rcFile", default="bits.rc", metavar="FILE",
                        help="Path of the bits.rc file to create or update. Default '%(default)s'.")
  init_cfg.add_argument("--append", dest="appendRc", action="store_true", default=False,
                        help="Merge the new settings into an existing bits.rc rather than "
                             "overwriting it. Without this flag a fresh file is written.")

  # Options for the version subcommand
  version_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                              help=("Display the specified architecture next to the version number. Default is "
                                    "the current system architecture, which is '%(default)s'."))

  # Options for the publish command
  publish_parser.add_argument("package", metavar="PACKAGE",
                              help="Name of the package to publish.")
  publish_parser.add_argument("version", metavar="VERSION", nargs="?", default=None,
                              help="Version (and optional revision) to publish. Defaults to the latest build.")
  publish_parser.add_argument("--cvmfs-target", dest="cvmfsTarget", required=True, metavar="PATH",
                              help="Absolute path the package will occupy on CVMFS (e.g. /cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9).")
  publish_parser.add_argument("--spool", dest="spool", required=True, metavar="[USER@HOST:]PATH",
                              help="Ingestion spool root.  Either a local directory or a remote rsync target (user@host:/path).")
  publish_parser.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR, metavar="WORKDIR",
                              help="bits work directory containing the installed packages. Default: %(default)s.")
  publish_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH", default=detectedArch,
                              help="Target architecture. Default: %(default)s.")
  publish_parser.add_argument("--scratch-dir", dest="scratchDir", default=None, metavar="DIR",
                              help="Directory for the temporary CVMFS working copy. Defaults to a system temp dir.")
  publish_parser.add_argument("--rsync-opts", dest="rsyncOpts", default=None, metavar="OPTS",
                              help="Extra options passed verbatim to rsync (e.g. '-e \"ssh -i key\"').")
  publish_parser.add_argument("--no-relocate", dest="noRelocate", action="store_true", default=False,
                              help=("Skip the relocation step. Use this when the package was built "
                                    "directly at its final CVMFS path (--cvmfs-prefix on bits build), "
                                    "so all embedded paths are already correct."))

  # Options for the cleanup subcommand
  cleanup_parser.add_argument("-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR,
                              metavar="WORKDIR",
                              help="Persistent bits work directory to clean. Default: %(default)s.")
  cleanup_parser.add_argument("-a", "--architecture", dest="architecture", metavar="ARCH",
                              default=detectedArch,
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
  verify_parser.add_argument(
      "-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR, metavar="DIR",
      help=("Local bits work directory containing the TARS/ store.  "
            "Default '%(default)s'."),
  )
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
  status_parser.add_argument(
      "--defaults", dest="defaults", default="release", metavar="DEFAULT",
      help="Use defaults from CONFIGDIR/defaults-%(metavar)s.sh.",
  )
  status_parser.add_argument(
      "-a", "--architecture", dest="architecture", metavar="ARCH",
      default=detectedArch,
      help=("Target architecture. Default is the current system architecture, "
            "which is '%(default)s'."),
  )
  status_parser.add_argument(
      "-w", "--work-dir", dest="workDir", default=DEFAULT_WORK_DIR, metavar="DIR",
      help=("The bits work directory to inspect. Default '%(default)s'."),
  )
  status_parser.add_argument(
      "-c", "--config", dest="configDir",
      default=os.environ.get("BITS_REPO_DIR", "alidist"),
      help="The directory containing build recipes. Default '%(default)s'.",
  )
  status_parser.add_argument(
      "-C", "--chdir", metavar="DIR", dest="chdir", default=DEFAULT_CHDIR,
      help=("Change to the specified directory before doing anything. "
            "Default '%(default)s'."),
  )
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
      help="Disable any remote store (even if set in bits.rc).",
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

  # Apply bits.rc values as default overrides so that persistent settings written
  # by "bits init" (config mode) take effect on every subsequent invocation.
  # CLI flags still win: set_defaults only fills gaps not covered by the user.
  _rc_early = _read_bits_rc()
  _rc_defaults: dict = {}
  _RC_KEY_TO_DEST = [
      # (bits.rc key,        argparse dest)
      ("work_dir",           "workDir"),
      ("architecture",       "architecture"),
      ("defaults",           "defaults"),
      ("config_dir",         "configDir"),
      ("reference_sources",  "referenceSources"),
      ("remote_store",       "remoteStore"),
      ("write_store",        "writeStore"),
      ("organisation",       "organisation"),
      # provider_policy is handled separately in finaliseArgs (needs parsing),
      # but listing it here causes the raw string to be set as a default so
      # the CLI flag still wins via normal argparse precedence.
      ("provider_policy",    "providerPolicy"),
      # prerequisites_url: community-specific URL shown when compiler/git absent.
      ("prerequisites_url",  "prerequisitesUrl"),
  ]
  for _rc_key, _dest in _RC_KEY_TO_DEST:
    if _rc_early.get(_rc_key):
      _rc_defaults[_dest] = _rc_early[_rc_key]
  if _rc_defaults:
    # set_defaults on the *parent* parser is overridden by each subparser's own
    # argument-level defaults (add_argument(..., default=...)).  We must call
    # set_defaults on every subparser individually so that bits.rc values win
    # over hardcoded argument defaults while still losing to explicit CLI flags.
    for _sp in [build_parser, clean_parser, cleanup_parser, deps_parser, doctor_parser, init_parser, verify_parser, status_parser]:
      _sp.set_defaults(**_rc_defaults)

  # Make sure old option ordering behavior is actually still working
  prog = sys.argv[0]
  rest = sys.argv[1:]
  _cleanup_invocation = "cleanup" in rest
  def optionOrder(x):
    # --debug/-d must come before any subcommand so the parent parser sees them.
    # --dry-run/-n is also a top-level flag (for build), BUT the "cleanup"
    # subparser has its OWN --dry-run/-n.  If we're in a cleanup invocation
    # we must NOT hoist -n/-dry-run before "cleanup" or the parent parser will
    # consume it and the cleanup subparser's default (False) will overwrite it.
    if x in ["--debug", "-d"]:
      return 0
    if x in ["-n", "--dry-run"] and not _cleanup_invocation:
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
  args._init_explicit = _init_explicit_flags
  return (args, parser)

VALID_ARCHS_RE = "^slc[5-9]_(x86-64|ppc64|aarch64)$|^(ubuntu|ubt|osx|fedora)[0-9]*_(x86-64|arm64)$"

def matchValidArch(architecture):
  return bool(re.match(VALID_ARCHS_RE, architecture))

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

def finaliseArgs(args, parser):

  # Nothing to finalise for version, architecture, or verify
  # if args.action in ["version", "analytics", "architecture"]:
  if args.action in ["version", "architecture", "verify"]:
    return args

  # Minimal finalisation for status: normalise lists and expand referenceSources.
  if args.action == "status":
    if hasattr(args, "defaults"):
      args.defaults = args.defaults.split("::")
    args.noDevel       = normalise_multiple_options(args.noDevel)
    args.disable       = normalise_multiple_options(args.disable)
    args.force_rebuild = normalise_multiple_options(args.force_rebuild)
    args.referenceSources = args.referenceSources % {"workDir": args.workDir}
    return args

  if hasattr(args, "defaults"):
    args.defaults = args.defaults.split("::")

  # ── bits.rc / BITS_PROVIDERS ─────────────────────────────────────────────
  # Read persistent configuration from the first bits.rc / .bitsrc /
  # ~/.bitsrc found, then resolve ``bits_providers``.  Precedence:
  #   1. BITS_PROVIDERS environment variable (explicit override)
  #   2. ``providers`` key in the [bits] section of the config file
  #   3. Built-in default: the official bitsorg/bits-providers repository
  #
  # The resolved value is stored on ``args`` and also written back to the
  # environment so that child processes inherit it.
  _BITS_PROVIDERS_DEFAULT = "https://github.com/bitsorg/bits-providers"
  _rc = _read_bits_rc()
  args.bits_providers = (
    os.environ.get("BITS_PROVIDERS")
    or _rc.get("providers")
    or _BITS_PROVIDERS_DEFAULT
  )
  os.environ.setdefault("BITS_PROVIDERS", args.bits_providers)

  # ── store_integrity ───────────────────────────────────────────────────────
  # The flag is off by default.  It can be activated either by the CLI flag
  # (--store-integrity) or by adding 'store_integrity = true' to bits.rc.
  # The CLI flag always wins when present; the rc key serves as a persistent
  # opt-in so the feature does not need to be spelled out on every invocation.
  if not getattr(args, "storeIntegrity", False):
    args.storeIntegrity = _rc.get("store_integrity", "").strip().lower() in ("1", "true", "yes")

  # ── provider_policy ──────────────────────────────────────────────────────
  # Resolve the effective provider-position policy from (highest priority):
  #   1. --provider-policy CLI flag
  #   2. provider_policy key in bits.rc / .bitsrc
  # The raw string is parsed into {name: "prepend"|"append"} and stored on
  # args so that build.py can pass it straight through to the provider loader.
  _raw_policy = getattr(args, "providerPolicy", None) or _rc.get("provider_policy", "")
  args.provider_policy = _parse_provider_policy(_raw_policy)

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
    # make -j and makeflow see the real core count rather than the cgroup
    # quota inherited from the GitLab runner process.
    # /sys/devices/system/cpu/online gives the kernel-reported online CPU
    # list (e.g. "0-7") which reflects actual hardware, not the caller's
    # cgroup CPU quota.  Only inject if the user hasn't already specified
    # --cpuset-cpus in --docker-extra-args.
    if not any(a.startswith("--cpuset-cpus") for a in args.docker_extra_args):
      args.docker_extra_args.append("--cpuset-cpus=" + _host_online_cpus())

    if args.docker and args.architecture.startswith("osx"):
      parser.error("cannot use `-a %s` and --docker" % args.architecture)

    if args.docker and commands.getstatusoutput("which docker")[0]:
      parser.error("cannot use --docker as docker executable is not found")

    # If specified, used the docker image requested, otherwise, if running
    # in docker the docker image is given by the first part of the
    # architecture we want to build for.
    if args.docker and not args.dockerImage:
      args.dockerImage = "registry.cern.ch/alisw/%s-builder" % args.architecture.split("_")[0]

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
        from bits_helpers.utilities import docker_platform_for_arch, detectArch as _detectArch
        target_plat = docker_platform_for_arch(args.architecture)
        host_plat   = docker_platform_for_arch(_detectArch())
        if target_plat and target_plat != host_plat:
          args.dockerPlatform = target_plat
        else:
          args.dockerPlatform = None

    # --sandbox-image implies --sandbox=podman
    if getattr(args, "sandboxImage", None) and getattr(args, "sandbox", "auto") == "auto":
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

    # On selected platforms, caching is active by default
    if args.architecture in S3_SUPPORTED_ARCHS and not args.preferSystem and not args.no_remote_store:
      args.noSystem = "*"
      if not args.remoteStore:
        args.remoteStore = "https://s3.cern.ch/swift/v1/alibuild-repo"
    elif args.no_remote_store:
      args.remoteStore = ""

    if args.remoteStore or args.writeStore:
      args.noSystem = "*"

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
    if args.resourceMonitoring:
      try:
        import psutil
      except:
        args.resourceMonitoring = False
        print("Warning: Unable to use psutil. Disabling resource monitoring")
    pass
  elif args.action == "clean":
    pass
  else:
    pass
  return args
