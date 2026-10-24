# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits doctor`` without packages: is this machine set up to run bits?

Checks the Python bits runs with and its modules, running as root, git and a
compiler, the container engine (docker, or podman and its rootless setup), disk
space and the configured stores. Each check is PASS/WARN/FAIL/SKIP with a
one-line fix; any FAIL makes the exit code 1.
"""
import getpass
import importlib.util
import os
import re
import shutil
import subprocess
import sys

from bits_helpers.doctor import (PASS, FAIL, WARN, SKIP, _check_compiler,
                                 _check_disk_space, _check_host_tool, _check_store)

# (import name, pip name). yaml/requests/jinja2 are imported at startup, so bits
# cannot even reach doctor without them; listed for completeness.
_MODULES = [("yaml", "pyyaml"), ("requests", "requests"), ("distro", "distro"),
            ("jinja2", "jinja2")]
_SIGNING_MODULES = [("cryptography", "cryptography")]   # signed reuse; optional
_B3_MODULES = [("boto3", "boto3"), ("botocore", "botocore")]  # b3:// stores only
# `stat -f -c %T` names of file systems rootless podman storage cannot live on.
_NETWORK_FS = ("nfs", "afs", "cifs", "smb2", "ceph", "fuse", "fuseblk", "lustre", "gpfs")


def _run(cmd, timeout=30, stdout_only=False):
  """(returncode, output) of *cmd*; (127, message) if it cannot run."""
  try:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout if stdout_only else p.stdout + p.stderr).strip()
  except (OSError, subprocess.SubprocessError) as exc:
    return 127, str(exc)


def _pip_hint(names):
  if os.geteuid() == 0:
    return ("run bits as your own user (not sudo) and install there with: "
            "python3 -m pip install --user %s" % " ".join(names))
  return "install with: %s -m pip install --user %s" % (sys.executable, " ".join(names))


def check_python():
  v = "%d.%d.%d" % sys.version_info[:3]
  if sys.version_info < (3, 8):
    return FAIL, "%s is Python %s; bits needs 3.8 or newer" % (sys.executable, v)
  return PASS, "%s (Python %s)" % (sys.executable, v)


def check_modules(modules, missing_status):
  missing = [pip for mod, pip in modules if importlib.util.find_spec(mod) is None]
  if not missing:
    return PASS, "all present: %s" % ", ".join(mod for mod, _ in modules)
  return missing_status, "missing for %s: %s; %s" % (
      sys.executable, ", ".join(missing), _pip_hint(missing))


def check_site():
  """The site configuration files in effect, and where the main values come from."""
  from bits_helpers import site
  found = site.files()
  if not found:
    return WARN, "no site configuration files (bits' own site.d is missing)"
  keys = ("store.read", "store.operations", "store.s3_endpoint", "containers.registry",
          "containers.tag", "providers.url")
  values = ["%s=%s [%s]" % (k, site.get(k), os.path.basename(site.origin(k) or "?"))
            for k in keys if site.get(k) is not None]
  return PASS, "%s; %s" % (", ".join(found), "; ".join(values) or "no values")


def check_root():
  if os.geteuid() != 0:
    return PASS, "running as %s" % getpass.getuser()
  return WARN, ("running as root: packages installed with pip --user are not "
                "visible, and files in the work dir end up owned by root. Run "
                "bits as your own user (no sudo).")


def _has_subids(path, user, uid):
  try:
    with open(path) as f:
      return any(line.split(":", 1)[0] in (user, str(uid)) for line in f)
  except OSError:
    return False


def check_container_engine():
  """Checks for `docker` (real docker, or podman behind it). Empty if absent."""
  from bits_helpers.args import _is_rootless_podman, _rootless_podman_controllers
  if not shutil.which("docker"):
    return [("container engine", SKIP, "docker not found (only needed for --docker builds)")]
  _, version = _run(["docker", "--version"], stdout_only=True)   # not podman-docker's banner
  podman = "podman" in version.lower()
  engine = "podman" if podman else "docker"
  checks = []
  rc, out = _run(["docker", "info"], timeout=60)
  if rc:
    checks.append(("container engine", WARN, "%s does not respond (needed for --docker "
                   "builds): %s" % (engine, out.splitlines()[-1] if out else "no output")))
  else:
    checks.append(("container engine", PASS, version.splitlines()[0] if version else engine))
  rootless = podman and _is_rootless_podman()
  if rootless:
    user, uid = getpass.getuser(), os.getuid()
    ctrls = _rootless_podman_controllers()
    if ctrls is None:
      checks.append(("podman resource limits", SKIP, "cgroup controllers not readable"))
    else:
      missing = [c for c in ("cpuset", "memory") if c not in ctrls]
      if not ctrls:
        checks.append(("podman resource limits", WARN,
                       "no systemd user session for %s (su, or ssh without lingering): "
                       "podman cannot apply any limit, bits builds without them. Log in "
                       "directly, or run 'loginctl enable-linger %s'." % (user, user)))
      elif missing:
        checks.append(("podman resource limits", WARN,
                       "controller(s) %s not delegated to %s: bits builds without those "
                       "limits. To enable (root): put '[Service]' and 'Delegate=cpu cpuset "
                       "io memory pids' in /etc/systemd/system/user@.service.d/"
                       "delegate.conf, run 'systemctl daemon-reload', log in again."
                       % (", ".join(missing), user)))
      else:
        checks.append(("podman resource limits", PASS, "cpuset and memory delegated"))
    no_ids = [f for f in ("/etc/subuid", "/etc/subgid") if not _has_subids(f, user, uid)]
    if no_ids and shutil.which("getsubids"):
      no_ids = [f for f in no_ids
                if _run(["getsubids"] + (["-g"] if f.endswith("gid") else []) + [user])[0]]
    if no_ids:
      checks.append(("podman subordinate ids", FAIL,
                     "no range for %s in %s: rootless podman cannot unpack images. "
                     "Ask an admin, e.g.: usermod --add-subuids 100000-165535 "
                     "--add-subgids 100000-165535 %s" % (user, " and ".join(no_ids), user)))
    else:
      checks.append(("podman subordinate ids", PASS, "%s has subuid/subgid ranges" % user))
    rc, root = _run(["podman", "info", "--format", "{{.Store.GraphRoot}}"], stdout_only=True)
    if rc or not root:
      checks.append(("podman storage", WARN, "podman info failed (storage on AFS/NFS is a "
                     "common cause): %s" % (root.splitlines()[-1] if root else "no output")))
    else:
      rc, fstype = _run(["stat", "-f", "-c", "%T", root], stdout_only=True)
      if fstype in _NETWORK_FS:
        checks.append(("podman storage", FAIL,
                       "%s is on %s, which rootless podman cannot use: set graphroot "
                       "to a local disk in ~/.config/containers/storage.conf" % (root, fstype)))
      elif rc or not fstype or fstype.startswith("UNKNOWN"):
        checks.append(("podman storage", WARN, "%s: file system type unknown (%s); it must "
                       "be a local disk" % (root, fstype or "stat failed")))
      else:
        checks.append(("podman storage", PASS, "%s (%s)" % (root, fstype)))
  if shutil.which("getenforce"):
    _, mode = _run(["getenforce"])
    if mode.strip() == "Enforcing" and rootless:
      checks.append(("SELinux", PASS, "Enforcing; bits turns off SELinux labels for its "
                     "podman containers"))
    else:
      checks.append(("SELinux", PASS, mode.strip() or "unknown"))
  return checks


def check_s3cmd_store(url):
  """s3:// stores are accessed with s3cmd and ~/.s3cfg."""
  if not shutil.which("s3cmd"):
    return FAIL, "%s: s3cmd not found (s3:// stores use s3cmd; b3:// uses boto3)" % url
  if not os.path.isfile(os.path.expanduser("~/.s3cfg")):
    return FAIL, "%s: ~/.s3cfg not found (run 's3cmd --configure')" % url
  return PASS, "%s: s3cmd and ~/.s3cfg present" % url


def _s3_client(settings):
  """boto3 client for one bucket's *settings* (sync.s3_settings), as
  Boto3RemoteSync makes it, with short timeouts."""
  import boto3
  from botocore.config import Config
  style = settings["addressing_style"]
  config = Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 1},
                  **({"s3": {"addressing_style": style}} if style else {}))
  kwargs = {"endpoint_url": settings["endpoint"], "config": config,
            "aws_access_key_id": settings["access_key"],
            "aws_secret_access_key": settings["secret_key"]}
  if settings["session_token"]:
    kwargs["aws_session_token"] = settings["session_token"]
  if settings["region"]:
    kwargs["region_name"] = settings["region"]
  return boto3.client("s3", **kwargs)


def check_s3_store(url, args, label):
  """Credentials and bucket access for a b3:// (boto3) store."""
  from bits_helpers.sync import resolve_and_export_s3_config, s3_settings
  resolve_and_export_s3_config(getattr(args, "s3Endpoint", None),
                               getattr(args, "s3AccessKey", None),
                               getattr(args, "s3SecretKey", None),
                               getattr(args, "s3Region", None),
                               getattr(args, "s3AddressingStyle", None))
  bucket = url[len("b3://"):].split("/", 1)[0]
  settings = s3_settings(bucket)
  if not (settings["access_key"] and settings["secret_key"]):
    key = "BITS_S3_%s_" % re.sub(r"[^A-Za-z0-9]", "_", bucket).upper()
    return FAIL, ("%s: no S3 credentials. Set %sACCESS_KEY_ID and %sSECRET_ACCESS_KEY "
                  "(or a [%s] section in ~/.bits/s3keys) for this bucket, or "
                  "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, --s3-access-key/"
                  "--s3-secret-key or ~/.bits/s3keys for all" % (url, key, key, bucket))
  if importlib.util.find_spec("boto3") is None:
    return FAIL, "%s: boto3 missing; %s" % (url, _pip_hint(["boto3"]))
  try:
    _s3_client(settings).head_bucket(Bucket=bucket)
  except Exception as exc:   # botocore ClientError, endpoint or network errors
    return FAIL, "%s: bucket not accessible with these credentials (%s)" % (url, exc)
  return PASS, "%s: credentials found, bucket accessible (%s)" % (url, label)


def run_setup_checks(args):
  from bits_helpers.sync import store_urls
  reads = [u.rstrip("/") for u in store_urls(getattr(args, "remoteStore", ""))]
  writes = [u.rstrip("/") for u in store_urls(getattr(args, "writeStore", ""))]
  b3 = any(u.startswith("b3://") for u in reads + writes)
  checks = [
    ("python", *check_python()),
    ("python modules", *check_modules(_MODULES, FAIL)),
    ("python modules (signing)", *check_modules(_SIGNING_MODULES, WARN)),
    ("python modules (b3:// store)", *check_modules(_B3_MODULES, FAIL if b3 else WARN)),
    ("site configuration", *check_site()),
    ("user", *check_root()),
    ("git", *_check_host_tool("git")),
    ("C++ compiler", *_check_compiler()),
  ]
  checks += check_container_engine()
  work_dir = getattr(args, "workDir", "sw") or "sw"
  checks.append(("disk space (%s)" % work_dir,
                 *_check_disk_space(work_dir, float(getattr(args, "minDisk", None) or 10.0))))
  stores = [(u, "read store") for u in reads] + \
           [(u, "write store") for u in writes if u not in reads]
  for url, label in stores:
    label = label + (" " + url if len(stores) > 1 else "")
    if url.startswith("b3://"):
      checks.append((label, *check_s3_store(url, args, label)))
    elif url.startswith("s3://"):
      checks.append((label, *check_s3cmd_store(url)))
    else:
      checks.append((label, *_check_store(url, getattr(args, "insecure", False))))
  if not stores:
    checks.append(("stores", SKIP, "no --remote-store / --write-store configured"))
  return checks
