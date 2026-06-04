"""Recipe sandbox: wrap build commands with podman (Linux) or sandbox-exec (macOS).

Sandbox modes
-------------
off
    No sandboxing; the recipe script runs directly on the host (or inside
    the Docker container when ``--docker`` is used).  This is the behaviour
    of all previous versions of bits.

auto  (default)
    Choose the best available sandbox automatically:

    * **Docker mode** (``--docker``): nested podman inside the container,
      if podman is available in the builder image.  Falls back to ``off``
      with a warning.
    * **macOS, no Docker**: ``sandbox-exec`` (built-in, zero overhead).
    * **Linux, no Docker**: podman (rootless) if available; ``off`` otherwise.

podman
    Always use podman.  Requires ``--docker`` or ``--sandbox-image`` to
    supply the container image.  Raises ``ValueError`` if podman is not
    found.

sandbox-exec
    macOS only.  Raises ``ValueError`` on any other platform.

Per-recipe network control
--------------------------
Recipes can declare::

    sandbox_network: on    # (default) outgoing network is blocked
    sandbox_network: off   # outgoing network is allowed

``on`` means "the network restriction is on" (network is blocked).
``off`` means "the network restriction is off" (network is allowed).
This is useful for recipes that need to ``pip install`` or ``gem install``
at build time.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tempfile
from shlex import quote
from typing import Optional

from bits_helpers.log import warning, debug


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_dind() -> bool:
    """Return True if the current process appears to be running inside a container.

    Checks for ``/.dockerenv``, then inspects ``/proc/1/cgroup`` for
    docker/kubernetes markers.  Returns False on macOS or when ``/proc``
    is unavailable.
    """
    if sys.platform == "darwin":
        return False
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as fh:
            return any(
                kw in line
                for line in fh
                for kw in ("docker", "kubepods", "containerd")
            )
    except OSError:
        return False


def podman_available() -> bool:
    """Return True if a working podman executable is on PATH.

    Runs ``podman info`` with a 5-second timeout to verify the daemon/runtime
    is reachable, not just that the binary exists.
    """
    if not shutil.which("podman"):
        return False
    try:
        result = subprocess.run(
            ["podman", "info", "--format=json"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def sandbox_exec_available() -> bool:
    """Return True if ``sandbox-exec`` is available (macOS only)."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def resolve_sandbox_mode(requested: str, docker_active: bool) -> str:
    """Return the effective sandbox mode.

    :param requested: one of ``"off"``, ``"auto"``, ``"podman"``, ``"sandbox-exec"``
    :param docker_active: True when ``--docker`` was passed to ``bits build``
    :raises ValueError: when an explicitly requested mode is unavailable
    """
    if requested == "off":
        return "off"

    if requested == "podman":
        if not podman_available():
            raise ValueError(
                "--sandbox=podman requested but podman is not available on this system"
            )
        return "podman"

    if requested == "sandbox-exec":
        if not sandbox_exec_available():
            raise ValueError(
                "--sandbox=sandbox-exec is only supported on macOS"
            )
        return "sandbox-exec"

    # --- auto ---
    if docker_active:
        # The build already runs inside a Docker container; add an additional
        # nested-podman layer for recipe isolation.
        if podman_available():
            return "podman"
        warning(
            "sandbox=auto with --docker: podman not found, sandboxing disabled. "
            "Install podman in the builder image to enable recipe sandboxing."
        )
        return "off"

    # Local build (no outer Docker)
    if sys.platform == "darwin":
        return "sandbox-exec" if sandbox_exec_available() else "off"

    # Linux, no outer Docker: do NOT use — or even probe — podman here.  Recipe
    # sandboxing with podman is only meaningful when a container image is
    # available, i.e. inside --docker (the nested-podman branch above) or when
    # the user explicitly asks for it via --sandbox=podman / --sandbox-image
    # (handled by the requested == "podman" branch). Returning "off" here keeps
    # an ordinary `bits build` from invoking `podman info` and from depending on
    # a working rootless-podman setup.
    return "off"


# ---------------------------------------------------------------------------
# macOS sandbox-exec profile
# ---------------------------------------------------------------------------

_SBPL_TEMPLATE = """\
(version 1)
(deny default)
(allow process*)
(allow signal)
(allow file-read*)
{builddir_rules}
; Temp dirs. On macOS /tmp and /var are symlinks to /private/tmp and
; /private/var, and SBPL subpath matching resolves symlinks, so the kernel
; sees the canonical /private/... paths. Allow both the symlink names (in case
; a path is matched pre-resolution) and the canonical targets — otherwise the
; compiler's own temp files ($TMPDIR=/var/folders/.../T -> /private/var/...)
; are denied and clang reports "unable to make temporary file" /
; "C compiler cannot create executables".
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/var/folders"))
(allow file-write* (subpath "/var/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/private/var/folders"))
(allow file-write* (subpath "/private/var/tmp"))
; Standard character devices. /dev is outside every allowed write subpath, so
; without these the default (deny) breaks `> /dev/null`, process substitution
; (/dev/fd), ptys, entropy, etc. — i.e. essentially every autotools configure.
; Raw block/disk devices (/dev/disk*, /dev/rdisk*) are deliberately NOT listed
; so a buggy or malicious recipe still cannot scribble over the raw disk.
(allow file-write*
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/random")
  (literal "/dev/urandom")
  (literal "/dev/tty")
  (literal "/dev/dtracehelper")
  (literal "/dev/ptmx")
  (subpath "/dev/fd")
  (regex #"^/dev/ttys[0-9]+$"))
(allow sysctl*)
(allow mach*)
(allow ipc*)
{network_rule}
"""


def make_sbpl_profile(allow_network: bool, builddir: str) -> str:
    """Write a temporary SBPL sandbox profile and return its path.

    The caller is responsible for deleting the file when done.  The profile
    denies everything by default, then allows process execution, file access
    within *builddir* and system temp directories, and optionally network
    access.

    :param allow_network: when True, ``(allow network*)`` is added
    :param builddir: absolute host path of the bits work directory;
                     the recipe is allowed to write anywhere beneath it
    """
    # Allow writes under the build dir AND its symlink-resolved real path.
    # SBPL subpath matching resolves symlinks, so if the work dir lives under a
    # symlinked prefix (e.g. /var -> /private/var) only the canonical path
    # actually matches; include both so either form works.
    builddirs = [builddir]
    real = os.path.realpath(builddir)
    if real != builddir:
        builddirs.append(real)

    # FIX: SBPL string literals are delimited by double-quotes, so a '"' in a
    # builddir would escape the literal and allow injection of arbitrary SBPL
    # rules (e.g. lifting the write restriction to cover /etc).  Reject early.
    for d in builddirs:
        if '"' in d:
            raise ValueError(
                f"workdir path contains '\"' which cannot be safely embedded in an "
                f"SBPL sandbox profile: {d!r}. Use a path without double-quote "
                f"characters."
            )
    builddir_rules = "\n".join(
        '(allow file-write* (subpath "%s"))' % d for d in builddirs
    )

    network_rule = "(allow network*)" if allow_network else ""
    content = _SBPL_TEMPLATE.format(
        builddir_rules=builddir_rules,
        network_rule=network_rule,
    )
    fd, path = tempfile.mkstemp(suffix=".sb", prefix="bits-sandbox-")
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    return path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def wrap_build_command(
    build_command: str,
    spec: dict,
    opts,
    *,
    workdir: str,
    docker_active: bool = False,
    container_workdir: Optional[str] = None,
    docker_image: Optional[str] = None,
) -> str:
    """Return *build_command* wrapped with the configured sandbox, or unchanged.

    Network semantics (per-recipe):

    * ``sandbox_network: on``  (default) — outgoing network is **blocked**
    * ``sandbox_network: off``           — outgoing network is **allowed**

    :param build_command:
        The shell string that runs the recipe (either ``env … bash build.sh``
        for local builds, or ``docker run … bash -ex /build.sh`` for Docker).
    :param spec:
        Parsed recipe dict.  Checked for the ``sandbox_network`` key.
    :param opts:
        Parsed CLI namespace.  Used for ``opts.sandbox`` (mode string) and
        ``opts.sandboxImage`` (override image for podman).
    :param workdir:
        Absolute host path of the bits work directory.
    :param docker_active:
        True when ``bits build --docker`` is in effect.  Triggers nested-podman
        mode instead of wrapping the host command.
    :param container_workdir:
        The workdir path *inside* the Docker container (e.g. ``/container/bits/sw``).
        Required when *docker_active* is True.
    :param docker_image:
        The Docker image name.  Used as the nested podman image when no
        ``--sandbox-image`` was given.
    """
    # defaults-* packages (defaults-release, defaults-user, …) are pure
    # configuration packages with no compiled build script — they inject
    # default values into the build system and never run inside a sandbox.
    # Skip silently so the "sandbox=podman requires a container image" warning
    # is not emitted for every defaults package in the dependency tree.
    pkg_name = spec.get("package", "")
    if pkg_name.startswith("defaults-"):
        return build_command

    # Semantics: sandbox_network is "is the network *restriction* on?".
    #   on  -> restriction on  -> network BLOCKED (allow_network False)
    #   off -> restriction off -> network ALLOWED (allow_network True)
    # Precedence: a recipe's own `sandbox_network:` field wins; otherwise fall
    # back to the global default in opts.sandboxNetwork, which build.py resolves
    # from --sandbox-network or the active defaults `sandbox_network:` (see
    # defaults-release.sh), defaulting to "on". This lets a stack with many
    # pip-installing recipes flip the default once instead of annotating every
    # recipe.
    global_default = getattr(opts, "sandboxNetwork", "on") or "on"
    sandbox_network = spec.get("sandbox_network", global_default)
    # YAML's SafeLoader parses bare on/off/yes/no as booleans, so a recipe line
    # `sandbox_network: off` arrives here as Python False (not the string
    # "off"). Normalise both forms so quoted and unquoted recipes behave the
    # same and nobody silently keeps the network blocked by forgetting quotes.
    if isinstance(sandbox_network, bool):
        allow_network = (sandbox_network is False)
    else:
        allow_network = (str(sandbox_network).strip().lower()
                         in ("off", "false", "no", "0"))

    requested = getattr(opts, "sandbox", "auto")
    mode = resolve_sandbox_mode(requested, docker_active)
    debug(
        "Sandbox mode for %s: %s (requested=%s, docker=%s, sandbox_network=%s)",
        spec.get("package", "?"), mode, requested, docker_active, sandbox_network,
    )

    if mode == "off":
        return build_command

    image = getattr(opts, "sandboxImage", None) or docker_image
    if mode == "podman" and not image:
        # No container image available.  If the user explicitly requested podman
        # (--sandbox=podman) this is a configuration mistake — warn loudly.
        # If podman was chosen automatically (sandbox=auto, Linux host) there is
        # simply no image to use; downgrade silently to "off" so that running
        # "bits build PKG" locally doesn't flood the console with per-package
        # warnings on every build invocation.
        if requested == "podman":
            warning(
                "sandbox=podman requires a container image (--docker or "
                "--sandbox-image). Sandboxing disabled for package %s.",
                spec.get("package", "?"),
            )
        else:
            debug(
                "sandbox=auto: podman available but no image supplied; "
                "sandboxing disabled for package %s.",
                spec.get("package", "?"),
            )
        return build_command

    # --- sandbox-exec (macOS) ---
    if mode == "sandbox-exec":
        profile = make_sbpl_profile(allow_network, workdir)
        return "sandbox-exec -f {} /bin/bash -c {}".format(
            quote(profile), quote(build_command)
        )

    # --- podman ---
    network_flag = "" if allow_network else "--network=none "

    if docker_active:
        # Nested podman: the Docker container runs a second-level podman
        # container for the recipe script.  The outer container launches with
        # ``bash -ex /build.sh`` as its command; we replace that with a nested
        # ``podman run … bash -ex /build.sh``.
        inner_workdir = container_workdir or workdir

        if detect_dind():
            warning(
                "bits is already running inside a Docker container (DinD). "
                "Nested podman requires the outer container to have been "
                "started with --security-opt seccomp=unconfined (or equivalent "
                "unprivileged user-namespace support). "
                "If builds fail with 'user namespaces not supported', "
                "disable sandboxing with --sandbox=off for this job."
            )

        nested_cmd = (
            "podman run --rm --userns=keep-id "
            "-v {wd}:{wd} "
            "{net}"
            "{img} "
            "bash -ex /build.sh"
        ).format(
            wd=quote(inner_workdir),
            net=network_flag,
            img=quote(image),
        )

        # Replace only the last occurrence of the inner Docker entrypoint
        marker = "bash -ex /build.sh"
        idx = build_command.rfind(marker)
        if idx == -1:
            warning(
                "sandbox: could not locate inner build entrypoint in Docker command; "
                "sandbox not applied for package %s.",
                spec.get("package", "?"),
            )
            return build_command
        return build_command[:idx] + nested_cmd

    else:
        # Local (no Docker): wrap the entire build_command string with podman.
        # The workdir is mounted at the same absolute path so that all embedded
        # paths (scriptDir, WORK_DIR_OVERRIDE, etc.) resolve correctly inside
        # the container.
        return (
            "podman run --rm --userns=keep-id "
            "-v {wd}:{wd} "
            "{net}"
            "{img} "
            "/bin/bash -c {cmd}"
        ).format(
            wd=quote(workdir),
            net=network_flag,
            img=quote(image),
            cmd=quote(build_command),
        )
