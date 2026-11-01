# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""The bits version: which release tag, commit and date this code is.

Resolution order: a git checkout of the source (``git describe``, exact even
with local edits), then the ``_version.py`` setuptools_scm writes into an
installed package, then setuptools_scm itself. Python 3.8 compatible.
"""

import os
import re
import subprocess

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
_DESCRIBE_RE = re.compile(r"^(?P<tag>.+)-(?P<n>[0-9]+)-g(?P<commit>[0-9a-f]+)$")


def _git(*args):
  # --no-optional-locks: never take .git/index.lock, so a concurrent git
  # command in the checkout cannot fail because bits was imported.
  return subprocess.run(("git", "--no-optional-locks", "-C", _ROOT) + args,
                        capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def _from_git():
  """Version info from the source checkout, or None outside one."""
  if not os.path.exists(os.path.join(_ROOT, ".git")):
    return None
  try:
    # Not `describe --dirty`: it refreshes the index and takes its lock.
    desc = _git("describe", "--tags", "--long", "--always")
    date = _git("log", "-1", "--format=%cs")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
  except (OSError, subprocess.SubprocessError):
    return None   # no git, or a checkout git refuses (e.g. another owner)
  m = _DESCRIBE_RE.match(desc)
  if m:
    tag, n, commit = m.group("tag"), int(m.group("n")), m.group("commit")
  else:   # no tag reachable: just the commit
    tag, n, commit = "", 0, desc
  if dirty:
    desc += "-dirty"
  # The plain tag when built exactly from it, else the describe string.
  version = tag if (tag and n == 0 and not dirty) else desc
  return {"version": version, "tag": tag, "distance": n, "commit": commit,
          "date": date, "dirty": dirty, "source": "git"}


def _from_package():
  """Version info from an installed package (setuptools_scm), or None."""
  try:
    from bits_helpers._version import version
  except ImportError:
    try:
      from setuptools_scm import get_version
      version = get_version(root=_ROOT)
    except Exception:   # not installed, or no SCM metadata (LookupError)
      return None
  m = _DESCRIBE_RE.match(version)
  if m:   # git describe --tags --long, as a recipe writes it (bits.bits)
    n = int(m.group("n"))
    return {"version": m.group("tag") if n == 0 else version, "tag": m.group("tag"),
            "distance": n, "commit": m.group("commit"), "date": "", "dirty": False,
            "source": "package"}
  # setuptools_scm form: 0.6.dev199+g648dfe8[.d20261002]
  m = re.search(r"\+g([0-9a-f]+)", version)
  d = re.search(r"\.d([0-9]{4})([0-9]{2})([0-9]{2})$", version)
  return {"version": version, "tag": "", "distance": 0,
          "commit": m.group(1) if m else "",
          "date": "-".join(d.groups()) if d else "", "dirty": bool(d),
          "source": "package"}


def version_info():
  """Dict with version, tag, distance, commit, date, dirty and source; or None."""
  return _from_git() or _from_package()


def version_line(info=None):
  """One line for ``bits --version``, e.g.
  ``bits 0.5-199-g648dfe8 (tag 0.5 +199, commit 648dfe8, 2026-10-02)``."""
  info = info if info is not None else version_info()
  if not info:
    return "bits unknown (neither an installed package nor a git checkout)"
  parts = []
  if info["tag"]:
    parts.append("tag %s" % info["tag"] + (" +%d" % info["distance"] if info["distance"] else ""))
  if info["commit"]:
    parts.append("commit %s" % info["commit"])
  if info["date"]:
    parts.append(info["date"])
  if info["dirty"]:
    parts.append("with local changes")
  return "bits %s%s" % (info["version"], " (%s)" % ", ".join(parts) if parts else "")
