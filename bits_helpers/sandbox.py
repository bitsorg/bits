# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

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


_REFUSAL: dict = {}


def sandbox_exec_refusal() -> Optional[str]:
    """Why ``sandbox-exec`` cannot run here, or None. It is tried once: a
    process already in a seatbelt sandbox (another tool's, or a bits build's)
    cannot start a second one, and would otherwise fail in every recipe."""
    if "r" not in _REFUSAL:
        try:
            rc = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)",
                                 "/usr/bin/true"], capture_output=True, timeout=30).returncode
            _REFUSAL["r"] = None if rc == 0 else (
                "sandbox-exec cannot start a sandbox here: this process is already in "
                "one (seatbelt sandboxes do not nest)")
        except Exception as exc:              # pylint: disable=broad-except
            _REFUSAL["r"] = "sandbox-exec cannot run: %s" % exc
    return _REFUSAL["r"]


_TRUE = ("1", "on", "yes", "true")


def default_mode(env_value, defaults_value, docker_active: bool) -> str:
    """The sandbox mode when --sandbox is not given: ``sandbox-exec`` for a macOS
    build outside Docker when $BITS_SANDBOX, else the defaults' ``sandbox:``,
    says on; ``off`` otherwise. $BITS_SANDBOX=0 turns it off whatever the
    defaults say."""
    value = env_value if env_value not in (None, "") else defaults_value
    on = value is True or str(value).strip().lower() in _TRUE
    if not (on and sys.platform == "darwin" and not docker_active):
        return "off"
    # Asked for by default, not on the command line: where it cannot run, the
    # build goes on unsandboxed, as with --sandbox=auto.
    refusal = None if sandbox_exec_available() else "sandbox-exec is not installed"
    refusal = refusal or sandbox_exec_refusal()
    if refusal:
        warning("BITS_SANDBOX / defaults sandbox: %s; sandboxing disabled.", refusal)
        return "off"
    return "sandbox-exec"


_PODMAN_IN_IMAGE_CACHE: dict = {}


def podman_in_image(image: str, docker_cmd: str = "docker") -> bool:
    """Return True if ``podman`` is on PATH inside the builder *image*.

    The nested-podman sandbox runs ``podman`` INSIDE the builder image, so the
    host having podman is irrelevant. Probe the image once (result cached).
    Note: a transient docker failure caches a negative for the run.
    """
    if not image:
        return False
    if image in _PODMAN_IN_IMAGE_CACHE:
        return _PODMAN_IN_IMAGE_CACHE[image]
    ok = False
    try:
        result = subprocess.run(
            [docker_cmd, "run", "--rm", "--entrypoint=", image,
             "sh", "-c", "command -v podman"],
            capture_output=True, timeout=60,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    _PODMAN_IN_IMAGE_CACHE[image] = ok
    return ok


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def resolve_sandbox_mode(requested: str, docker_active: bool, image=None) -> str:
    """Return the effective sandbox mode.

    :param requested: one of ``"off"``, ``"auto"``, ``"podman"``, ``"sandbox-exec"``
    :param docker_active: True when ``--docker`` was passed to ``bits build``
    :param image: builder image name; under ``--docker`` podman must exist INSIDE
        it (the nested-podman sandbox), so availability is probed in the image,
        not on the host.
    :raises ValueError: when an explicitly requested mode is unavailable
    """
    if requested == "off":
        return "off"

    if requested == "podman":
        # Under --docker the nested podman runs in the image; otherwise it runs
        # on the host (rootless podman driving --sandbox-image).
        available = podman_in_image(image) if docker_active else podman_available()
        if not available:
            raise ValueError(
                "--sandbox=podman requested but podman is not available %s"
                % ("in the builder image" if docker_active else "on this system")
            )
        return "podman"

    if requested == "sandbox-exec":
        if not sandbox_exec_available():
            raise ValueError(
                "--sandbox=sandbox-exec is only supported on macOS"
            )
        refusal = sandbox_exec_refusal()
        if refusal:
            raise ValueError("%s; build with --sandbox=off (or BITS_SANDBOX=0)" % refusal)
        return "sandbox-exec"

    # --- auto ---
    if docker_active:
        # The build already runs inside a Docker container; add an additional
        # nested-podman layer for recipe isolation -- but only if podman is
        # actually present in the builder image (probing the host would wrongly
        # pick podman for an image that lacks it, then fail at build time).
        if podman_in_image(image):
            return "podman"
        warning(
            "sandbox=auto with --docker: podman not found in the builder image, "
            "sandboxing disabled. Install podman in the image to enable recipe "
            "sandboxing."
        )
        return "off"

    # Local build (no outer Docker)
    if sys.platform == "darwin":
        if not sandbox_exec_available():
            return "off"
        refusal = sandbox_exec_refusal()
        if refusal:
            warning("sandbox=auto: %s; sandboxing disabled.", refusal)
            return "off"
        return "sandbox-exec"

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


def _both(path: str, literal: bool = False) -> list:
    """*path* and its symlink-resolved form: SBPL matches resolved paths (e.g.
    /private/var for /var), so a rule must name the canonical one too. For a
    *literal* (a symlink the build replaces, like ``latest``) only its directory
    is resolved: the link's target is another version's, not this build's."""
    if literal:
        real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    else:
        real = os.path.realpath(path)
    return [path] if real == path else [path, real]


def package_write_paths(env: dict, workdir: str, build_workdir: str,
                        extra=()) -> tuple:
    """What one package's build may write, as ``(subpaths, literals, dirs)``:
    the trees that are its own (its BUILD, INSTALLROOT, TMP, store tarball,
    tarball links, installation and source directories), the symlinks it
    points at itself (``latest``), and the directories it may create on the
    way to them. Nothing else in the work dir: not another package's files.

    *env* is the package's build environment (``PKGHASH``, ``PKGNAME``, …, as
    build_template.sh gets it); *extra* are --sandbox-allow-write paths.
    """
    h, name = env["PKGHASH"], env["PKGNAME"]
    arch = env["EFFECTIVE_ARCHITECTURE"]
    verrev = env["PKGVERSION"] + ("-" + env["PKGREVISION"] if env.get("PKGREVISION") else "")
    family = env.get("PKGFAMILY") or ""
    pkgpath = os.path.join(arch, family, name, verrev) if family else \
        os.path.join(arch, name, verrev)
    w, b = workdir, build_workdir
    subpaths = [
        os.path.join(b, "BUILD", h),
        os.path.join(w, "INSTALLROOT", h),
        os.path.join(w, "TMP", h),
        os.path.join(w, "TARS", arch, "store", h[:2], h),
        os.path.join(w, "TARS", arch, name),
        os.path.join(w, pkgpath),
        os.path.join(w, "SOURCES", name, env["PKGVERSION"], env.get("COMMIT_HASH") or "0"),
    ]
    if env.get("BITS_FETCH_CACHE"):          # relative to the work dir
        subpaths.append(os.path.join(w, env["BITS_FETCH_CACHE"]))
    literals = [os.path.join(b, "BUILD", name + "-latest"),
                os.path.join(w, os.path.dirname(pkgpath), "latest")]
    if env.get("DEVEL_PREFIX"):
        literals.append(os.path.join(b, "BUILD", "%s-latest-%s" % (name, env["DEVEL_PREFIX"])))
    if env.get("BUILD_FAMILY"):
        literals.append(os.path.join(w, os.path.dirname(pkgpath),
                                     "latest-" + env["BUILD_FAMILY"]))
    # Shared caches a compiler or a package manager writes to, and the module
    # files a package's post-relocate.sh may write outside its own tree.
    caches = [env.get("CCACHE_DIR") or os.environ.get("CCACHE_DIR") or "",
              os.path.join(os.path.expanduser("~"), "Library", "Caches"),
              env.get("MODULES_STAGING") or os.environ.get("MODULES_STAGING") or "",
              os.path.join(w, "MODULES")]
    subpaths += [c for c in caches if c] + [os.path.abspath(e) for e in extra]
    # Directories that may have to be created (mkdir -p) on the way to them.
    dirs = set()
    for top in {w, b}:
        dirs.add(top)
        for d in ("BUILD", "SOURCES", "TARS", "SPECS", "INSTALLROOT", "TMP"):
            dirs.add(os.path.join(top, d))
        for p in subpaths + literals:
            rel = os.path.relpath(p, top)
            if rel.startswith(".."):
                continue
            parent = os.path.dirname(p)
            while parent != top and parent.startswith(top + os.sep):
                dirs.add(parent)
                parent = os.path.dirname(parent)
    return subpaths, literals, sorted(dirs)


def _sbpl_quote(path: str) -> str:
    # SBPL string literals are delimited by double-quotes: a '"' in a path would
    # end the literal and let the rest inject rules (e.g. write access to /etc).
    if '"' in path:
        raise ValueError(
            "path contains '\"' which cannot be safely embedded in an SBPL sandbox "
            "profile: %r. Use a path without double-quote characters." % path)
    return '"%s"' % path


def make_sbpl_profile(allow_network: bool, builddir: str, write=None, path=None,
                      replay=None) -> str:
    """Write an SBPL sandbox profile and return its path.

    The profile denies everything by default, then allows process execution,
    reading, writing the system temp directories and, with *write* (the
    ``(subpaths, literals, dirs)`` of :func:`package_write_paths`), only the
    package's own files; without it, anywhere under *builddir* (the work dir).
    The network is allowed only with *allow_network*.

    *path*: where to write it (the package's ``sandbox.sb`` beside build.sh,
    kept for replaying the build); a temporary file, for the caller to
    delete, when None. *replay*: the command it is used with, as a comment.
    """
    if write is None:
        rules = ["(allow file-write* (subpath %s))" % _sbpl_quote(d) for d in _both(builddir)]
    else:
        subpaths, literals, dirs = write
        rules = ["(allow file-write* (subpath %s))" % _sbpl_quote(d)
                 for p in subpaths for d in _both(p)]
        rules += ["(allow file-write* (literal %s))" % _sbpl_quote(d)
                  for p in literals for d in _both(p, literal=True)]
        # Creating a directory on the way, and touching its metadata, but not
        # writing into it: that needs one of the paths above.
        rules += ["(allow file-write-create file-write-mode file-write-owner file-write-times"
                  " file-write-flags (literal %s))" % _sbpl_quote(d)
                  for p in dirs for d in _both(p)]
    network_rule = "(allow network*)" if allow_network else ""
    content = _SBPL_TEMPLATE.format(
        builddir_rules="\n".join(rules),
        network_rule=network_rule,
    )
    if replay:
        content = ("; Written by bits for one package's build. Replay it, with the build's\n"
                   "; environment (bits --debug prints the command), as:\n;   %s\n%s" % (
                       replay, content))
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".sb", prefix="bits-sandbox-")
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
    else:
        with open(path, "w") as fh:
            fh.write(content)
    return path


def failure_hint(build_command: str) -> str:
    """A hint for a failed build that ran in the macOS sandbox, or ""."""
    if "sandbox-exec -f" not in build_command:
        return ""
    return ("The build ran in a macOS sandbox that lets it write only its own "
            "directories, the temp dirs and a few caches. `grep 'Operation not "
            "permitted'` in the log shows what it was refused; the profile is "
            "sandbox.sb beside build.sh. --sandbox-allow-write PATH allows a "
            "path, --sandbox=off (or BITS_SANDBOX=0) turns the sandbox off.")


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
    pkg_env: Optional[dict] = None,
    build_workdir: Optional[str] = None,
    script_dir: Optional[str] = None,
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
    :param pkg_env:
        The package's build environment. With it, sandbox-exec lets the build
        write only the package's own files (:func:`package_write_paths`, in
        *build_workdir* for BUILD); without it, anywhere in *workdir*.
    :param script_dir:
        The directory of the package's build.sh: its profile is kept there as
        sandbox.sb. A temporary file without it.
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
    # The recipe may set it top-level or under `system:` (as defaults files do).
    recipe_system = spec.get("system") if isinstance(spec.get("system"), dict) else {}
    sandbox_network = spec.get("sandbox_network",
                               recipe_system.get("sandbox_network", global_default))
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
    image = getattr(opts, "sandboxImage", None) or docker_image
    mode = resolve_sandbox_mode(requested, docker_active, image=image)
    debug(
        "Sandbox mode for %s: %s (requested=%s, docker=%s, sandbox_network=%s)",
        spec.get("package", "?"), mode, requested, docker_active, sandbox_network,
    )

    if mode == "off":
        return build_command

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
        write = None
        if pkg_env is not None:
            write = package_write_paths(pkg_env, workdir, build_workdir or workdir,
                                        getattr(opts, "sandboxAllowWrite", None) or ())
        profile = make_sbpl_profile(
            allow_network, workdir, write=write,
            path=os.path.join(script_dir, "sandbox.sb") if script_dir else None,
            replay="sandbox-exec -f sandbox.sb /bin/bash -e -x build.sh" if script_dir else None)
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
