# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sync backends for bits."""

import glob
import os
import os.path
import re
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.exceptions import RequestException
from urllib.parse import quote

from bits_helpers.cmd import execute
from bits_helpers.log import debug, info, error, dieOnError, ProgressPrint
from bits_helpers.utilities import resolve_store_path, resolve_links_path, symlink, effective_arch, ver_rev


# Default S3 endpoint. Kept for backward compatibility with aliBuild: when no
# endpoint is configured (neither CLI nor env), b3:// stores talk to CERN S3.
DEFAULT_S3_ENDPOINT = "https://s3.cern.ch"

# Default private file to read S3 credentials from when they are not in the
# environment: ~/.bits/s3keys (alongside ~/.bits/gitlab-token). Override with
# $BITS_AWS_KEYS_FILE.
DEFAULT_AWS_KEYS_FILE = "~/.bits/s3keys"


def _load_aws_keys_file(path):
  """Read S3 credentials from a private key file (default ~/.bits/s3keys).

  Accepts simple ``KEY=VALUE`` lines (optionally ``export``-prefixed and/or
  quoted) as well as AWS-credentials INI style (``aws_access_key_id = ...``);
  ``#`` comments and ``[section]`` headers are ignored. Returns a dict with any
  of: access_key, secret_key, session_token, region, endpoint (empty if the
  file is absent). Warns — but does not fail — when the file is group/other
  readable, since it holds a secret.
  """
  import stat
  out = {}
  if not path or not os.path.isfile(path):
    return out
  alias = {
      "aws_access_key_id": "access_key", "access_key_id": "access_key",
      "aws_secret_access_key": "secret_key", "secret_access_key": "secret_key",
      "aws_session_token": "session_token", "session_token": "session_token",
      "aws_default_region": "region", "aws_region": "region", "region": "region",
      "bits_s3_endpoint_url": "endpoint", "s3_endpoint_url": "endpoint",
      "endpoint_url": "endpoint", "endpoint": "endpoint",
  }
  try:
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
      from bits_helpers.log import warning
      warning("%s is group/other-readable but holds S3 credentials; run "
              "`chmod 600 %s`", path, path)
    with open(path) as fh:
      for line in fh:
        line = line.strip()
        if not line or line[0] in "#[":
          continue
        if line.startswith("export "):
          line = line[7:]
        if "=" not in line:
          continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"').strip("'")
        if v and k in alias:
          out[alias[k]] = v
  except OSError:
    pass
  return out


def resolve_and_export_s3_config(endpoint=None, access_key=None, secret_key=None,
                                 region=None, addressing_style=None):
  """Resolve the S3 connection settings and export them to the environment.

  Precedence for every setting (highest first):
    1. an explicit value (a --s3-* command-line flag);
    2. BITS_<NAME> -- the per-host override, typically set in the gitlab-runner
       `environment`. It exists because GitLab makes a CI/CD variable win over a
       same-named runner `environment` entry, so a runner cannot override the
       common config unless bits looks at a distinct name first;
    3. <NAME> -- the common value, typically a GitLab CI/CD variable;
    4. the built-in default.
  The resolved values are written back into os.environ under their canonical
  names so that the boto3 client (Boto3RemoteSync) and the
  `bits_helpers.upload_cmd` subprocess spawned by --pipeline all see the same
  connection without threading secrets through the command line.

  Backward compatible: with no --s3-* flags and no env vars, the endpoint
  defaults to CERN S3 and credentials come from AWS_ACCESS_KEY_ID /
  AWS_SECRET_ACCESS_KEY exactly as before (aliBuild behaviour).
  """
  # Fallback credential source, below flags and env: a private ~/.bits/s3keys file
  # (override path with $BITS_AWS_KEYS_FILE). Flags and env (CI) still win.
  _file = _load_aws_keys_file(os.path.expanduser(
      os.environ.get("BITS_AWS_KEYS_FILE") or DEFAULT_AWS_KEYS_FILE))

  def _pick(*names):
    for n in names:
      v = os.environ.get(n)
      if v:
        return v
    return None

  endpoint = (endpoint
              or _pick("BITS_S3_ENDPOINT_URL",
                       "S3_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL")
              or _file.get("endpoint")
              or DEFAULT_S3_ENDPOINT)
  os.environ["S3_ENDPOINT_URL"] = endpoint

  access_key = access_key or _pick("BITS_AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID") or _file.get("access_key")
  if access_key:
    os.environ["AWS_ACCESS_KEY_ID"] = access_key
  secret_key = secret_key or _pick("BITS_AWS_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY") or _file.get("secret_key")
  if secret_key:
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
  session_token = _pick("BITS_AWS_SESSION_TOKEN", "AWS_SESSION_TOKEN") or _file.get("session_token")
  if session_token:
    os.environ["AWS_SESSION_TOKEN"] = session_token

  region = (region or _pick("BITS_AWS_DEFAULT_REGION", "AWS_DEFAULT_REGION", "AWS_REGION")
            or _file.get("region"))
  if region:
    os.environ["AWS_DEFAULT_REGION"] = region
  addressing_style = addressing_style or _pick("BITS_S3_ADDRESSING_STYLE", "S3_ADDRESSING_STYLE")
  if addressing_style:
    os.environ["S3_ADDRESSING_STYLE"] = addressing_style
  return {"endpoint": endpoint, "region": region, "addressing_style": addressing_style}


def remote_from_url(read_url, write_url, architecture, work_dir, insecure=False,
                    s3_endpoint=None, s3_access_key=None, s3_secret_key=None,
                    s3_region=None, s3_addressing_style=None):
  """Parse remote store URLs and return the correct RemoteSync instance for them."""
  # For S3-backed stores, resolve + export the connection config before any S3
  # backend is built, so boto3 and the --pipeline subprocess share one
  # endpoint/credentials. No-op for non-S3 stores (rsync/cvmfs/https).
  if (read_url or "").startswith(("s3://", "b3://")) or \
     (write_url or "").startswith(("s3://", "b3://")):
    resolve_and_export_s3_config(s3_endpoint, s3_access_key, s3_secret_key,
                                 s3_region, s3_addressing_style)
  # Read-only read stores (a CVMFS filesystem mount, an HTTP mirror) cannot
  # upload. When a separate --write-store is also given it is necessarily a
  # different backend, so pair the read-only reader with a writer built from
  # write_url (see DualRemoteSync). Without this, the write store was silently
  # dropped below and nothing was ever uploaded.
  if write_url and (read_url.startswith("cvmfs://") or read_url.startswith("http")):
    if read_url.startswith("http"):
      reader = HttpRemoteSync(read_url, architecture, work_dir, insecure)
    else:
      reader = CVMFSRemoteSync(read_url, None, architecture, work_dir)
    return DualRemoteSync(reader, _writer_from_url(write_url, architecture, work_dir))

  if read_url.startswith("http"):
    return HttpRemoteSync(read_url, architecture, work_dir, insecure)
  if read_url.startswith("s3://"):
    return S3RemoteSync(read_url, write_url, architecture, work_dir)
  if read_url.startswith("b3://"):
    return Boto3RemoteSync(read_url, write_url, architecture, work_dir)
  if read_url.startswith("cvmfs://"):
    return CVMFSRemoteSync(read_url, None, architecture, work_dir)
  if read_url:
    return RsyncRemoteSync(read_url, write_url, architecture, work_dir)
  return NoRemoteSync()


def _writer_from_url(write_url, architecture, work_dir):
  """Build a write helper for *write_url*, for the DualRemoteSync case.

  Constructed with read == write == write_url so the helper's skip/exists checks
  consult the write store itself; only its upload_* methods are ever called.
  """
  if write_url.startswith("s3://"):
    return S3RemoteSync(write_url, write_url, architecture, work_dir)
  if write_url.startswith("b3://"):
    return Boto3RemoteSync(write_url, write_url, architecture, work_dir)
  if write_url.startswith("cvmfs://"):
    dieOnError(True, "Cannot use a cvmfs:// store as a --write-store: CVMFS is read-only.")
  return RsyncRemoteSync(write_url, write_url, architecture, work_dir)


class DualRemoteSync:
  """Read packages from one backend, upload freshly-built ones to another.

  Used when packages are recalled from a read-only store (e.g. a CVMFS mount)
  but newly built packages must be published to a different store (e.g. S3).
  Reads delegate to *reader*; uploads delegate to *writer*.

  Only freshly-built packages are uploaded. A package recalled from the read
  store carries a non-empty ``spec["cachedTarball"]`` (and, for CVMFS, only a
  synthetic tarball of symlinks into ``/cvmfs``); uploading it would publish a
  stub and/or duplicate an artifact that already lives on the read store. This
  mirrors build.py's own "built_from_source" vs "from_store" distinction.
  """

  def __init__(self, reader, writer) -> None:
    self.reader = reader
    self.writer = writer
    self.architecture = getattr(reader, "architecture", None)
    self.workdir = getattr(reader, "workdir", None)

  # build.py both reads and writes syncHelper.writeStore (the development-package
  # path sets it to "" to disable uploads), so proxy it onto the writer.
  @property
  def writeStore(self):
    return getattr(self.writer, "writeStore", None)

  @writeStore.setter
  def writeStore(self, value):
    self.writer.writeStore = value

  @staticmethod
  def _was_recalled(spec) -> bool:
    return bool(spec.get("cachedTarball"))

  # ── reads → reader ──────────────────────────────────────────────────────────
  def fetch_tarball(self, spec):
    return self.reader.fetch_tarball(spec)

  def fetch_symlinks(self, spec):
    return self.reader.fetch_symlinks(spec)

  def fetch_source(self, *args, **kwargs):
    return self.reader.fetch_source(*args, **kwargs)

  # ── writes → writer (freshly-built packages only) ────────────────────────────
  def upload_symlinks_and_tarball(self, spec):
    if self._was_recalled(spec):
      debug("Not uploading %s: recalled from the read store, not built this run.",
            spec.get("package", "?"))
      return None
    return self.writer.upload_symlinks_and_tarball(spec)

  def upload_shell_command(self, spec):
    if self._was_recalled(spec):
      return None
    return self.writer.upload_shell_command(spec)

  def upload_source(self, *args, **kwargs):
    return self.writer.upload_source(*args, **kwargs)


def _source_remote_path(url_checksum, filename):
  """Return the remote-store path for a cached source archive.

  The path mirrors the local ``SOURCES/cache/`` structure so that a plain
  rsync or S3 sync of the ``SOURCES/cache/`` subtree is sufficient to
  populate (or restore) the remote archive.

  Example::

      SOURCES/cache/ab/abcd1234.../libfoo-1.2.tar.gz
  """
  return "SOURCES/cache/{}/{}/{}".format(url_checksum[:2], url_checksum, filename)


class NoRemoteSync:
  """Helper class which does not do anything to sync"""
  def fetch_symlinks(self, spec) -> None:
    pass
  def fetch_tarball(self, spec) -> None:
    pass
  def upload_symlinks_and_tarball(self, spec) -> None:
    pass
  def upload_shell_command(self, spec):
    """Return None: no remote store, nothing to upload."""
    return None
  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    return False
  def upload_source(self, local_path, url_checksum, filename) -> None:
    pass

class PartialDownloadError(Exception):
  def __init__(self, downloaded, size) -> None:
    self.downloaded = downloaded
    self.size = size
  def __str__(self):
    return "only %d out of %d bytes downloaded" % (self.downloaded, self.size)


class HttpRemoteSync:
  def __init__(self, remoteStore, architecture, workdir, insecure) -> None:
    self.remoteStore = remoteStore
    self.writeStore = ""
    self.architecture = architecture
    self.workdir = workdir
    self.insecure = insecure
    self.httpTimeoutSec = 15
    self.httpConnRetries = 4
    self.httpBackoff = 0.4

  def getRetry(self, url, dest=None, returnResult=False, log=True, session=None, progress=debug):
    get = session.get if session is not None else requests.get
    url = quote(url, safe=":/")
    for i in range(0, self.httpConnRetries):
      if i > 0:
        pauseSec = self.httpBackoff * (2 ** (i - 1))
        debug("GET %s failed: retrying in %.2f", url, pauseSec)
        time.sleep(pauseSec)
        # If the download has failed, enable debug output, even if it was
        # disabled before. We disable debug output for e.g. symlink downloads
        # to make sure the output log isn't overwhelmed. If the download
        # failed, we want to know about it, though. Note that bits has to
        # be called with --debug for this to take effect.
        log = True
      try:
        if log:
          debug("GET %s: processing (attempt %d/%d)", url, i+1, self.httpConnRetries)
        if dest or returnResult:
          # Destination specified -- file (dest) or buffer (returnResult).
          # Use requests in stream mode
          resp = get(url, stream=True, verify=not self.insecure, timeout=self.httpTimeoutSec)
          # Never write an error body as if it were the file: a missing object
          # (404/NoSuchKey) or any HTTP error must not be saved as the archive.
          if resp.status_code == 404:
            return None
          resp.raise_for_status()
          size = int(resp.headers.get("content-length", "-1"))
          downloaded = 0
          reportTime = time.time()
          result = []

          try:
            destFp = open(dest+".tmp", "wb") if dest else None
            for chunk in filter(bool, resp.iter_content(chunk_size=32768)):
              if destFp:
                destFp.write(chunk)
              if returnResult:
                result.append(chunk)
              downloaded += len(chunk)
              if log and size != -1:
                now = time.time()
                if downloaded == size:
                  progress("[100%%] Download complete")
                elif now - reportTime > 1:
                  progress("[%.0f%%] downloaded...", 100 * downloaded / size)
                  reportTime = now
          finally:
            if destFp:
              destFp.close()

          if size not in (downloaded, -1):
            raise PartialDownloadError(downloaded, size)
          if dest:
            os.rename(dest+".tmp", dest)  # we should not have errors here
          return b''.join(result) if returnResult else True
        else:
          # For CERN S3 we need to construct the JSON ourself...
          s3Request = re.match("https://s3.cern.ch/swift/v1[/]+([^/]*)/(.*)$", url)
          if s3Request:
            [bucket, prefix] = s3Request.groups()
            url = "https://s3.cern.ch/swift/v1/{}/?prefix={}".format(bucket, prefix.lstrip("/"))
            resp = get(url, verify=not self.insecure, timeout=self.httpTimeoutSec)
            if resp.status_code == 404:
              # No need to retry any further
              return None
            resp.raise_for_status()
            return [{"name": os.path.basename(x), "type": "file"}
                    for x in resp.text.split()]
          else:
            # No destination specified: JSON request
            resp = get(url, verify=not self.insecure, timeout=self.httpTimeoutSec)
            if resp.status_code == 404:
              # No need to retry any further
              return None
            resp.raise_for_status()
            return resp.json()
      except (RequestException,ValueError,PartialDownloadError) as e:
        if i == self.httpConnRetries-1:
          error("GET %s failed: %s", url, e)
        if dest:
          try:
            os.unlink(dest+".tmp")
          except Exception:
            pass
    return None

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    # Check for any existing tarballs we can use instead of fetching new ones.
    for pkg_hash in spec["remote_hashes"]:
      try:
        have_tarballs = os.listdir(os.path.join(
          self.workdir, resolve_store_path(arch, pkg_hash)))
      except OSError:  # store path not readable
        continue
      for tarball in have_tarballs:
        # The revision group is made optional ((?:-[0-9]+)?) so that tarballs
        # built with force_revision="" (revision-less name) are also matched
        # and reused without a redundant re-download.
        if re.match(r"^{package}-{version}(?:-[0-9]+)?\.{arch}\.tar\.gz$".format(
            package=re.escape(spec["package"]),
            version=re.escape(spec["version"]),
            arch=re.escape(arch),
        ), os.path.basename(tarball)):
          tarball_full = os.path.join(self.workdir, resolve_store_path(arch, pkg_hash), tarball)
          if not os.path.isfile(tarball_full):
            warning("Dangling symlink in tarball store (ignoring): %s", tarball_full)
            continue
          debug("Previously downloaded tarball for %s with hash %s, reusing",
                spec["package"], pkg_hash)
          return

    with requests.Session() as session:
      debug("Updating remote store for package %s; trying hashes %s",
            spec["package"], ", ".join(spec["remote_hashes"]))
      store_path = use_tarball = None
      # Find the first tarball that matches any possible hash and fetch it.
      for pkg_hash in spec["remote_hashes"]:
        store_path = resolve_store_path(arch, pkg_hash)
        tarballs = self.getRetry("{}/{}/".format(self.remoteStore, store_path),
                                 session=session)
        if tarballs:
          use_tarball = tarballs[0]["name"]
          break

      if store_path is None or use_tarball is None:
        debug("Nothing fetched for %s (%s)", spec["package"],
              ", ".join(spec["remote_hashes"]))
        return

      os.makedirs(os.path.join(self.workdir, store_path), exist_ok=True)

      destPath = os.path.join(self.workdir, store_path, use_tarball)
      if not os.path.isfile(destPath):   # do not download twice
        progress = ProgressPrint("Downloading tarball for %s@%s" %
                                 (spec["package"], spec["version"]), min_interval=5.0)
        progress("[0%%] Starting download of %s", use_tarball)  # initialise progress bar
        self.getRetry("/".join((self.remoteStore, store_path, use_tarball)),
                      destPath, session=session, progress=progress)
        progress.end("done")

  def fetch_symlinks(self, spec) -> None:
    links_path = resolve_links_path(effective_arch(spec, self.architecture), spec["package"])
    os.makedirs(os.path.join(self.workdir, links_path), exist_ok=True)

    # If we already have a symlink we can use, don't update the list. This
    # speeds up rebuilds significantly.
    if any(f"/{pkg_hash[:2]}/{pkg_hash}/" in target
           for target in (os.readlink(os.path.join(self.workdir, links_path, link))
                          for link in os.listdir(os.path.join(self.workdir, links_path)))
           for pkg_hash in spec["remote_hashes"]):
      debug("Found symlink for %s@%s, not updating", spec["package"], spec["version"])
      return

    with requests.Session() as session:
      # Fetch manifest file with initial symlinks. This file is updated
      # regularly; we use it to avoid many small network requests.
      # The .manifest index is an optional optimisation that bits' upload paths
      # don't generate, so a missing one (getRetry -> None) is normal: fall back
      # to the per-symlink listing below.
      manifest = self.getRetry("{}/{}.manifest".format(self.remoteStore, links_path),
                               returnResult=True, session=session) or b""
      symlinks = {
        linkname.decode("utf-8"): target.decode("utf-8")
        for linkname, sep, target in (line.partition(b"\t")
                                      for line in manifest.splitlines())
        if sep and linkname and target
      }
      # Now add any remaining symlinks that aren't in the manifest yet. There
      # should always be relatively few of these, as the separate network
      # requests are a bit expensive.
      for link in self.getRetry("{}/{}/".format(self.remoteStore, links_path),
                                session=session):
        linkname = link["name"]
        if linkname in symlinks:
          # This symlink is already present in the manifest.
          continue
        if os.path.islink(os.path.join(self.workdir, links_path, linkname)):
          # We have this symlink locally. With local revisions, we won't produce
          # revisions that will conflict with remote revisions unless we upload
          # them anyway, so there's no need to redownload.
          continue
        # This symlink isn't in the manifest yet, and we don't have it locally,
        # so download it individually.
        symlinks[linkname] = \
            self.getRetry("/".join((self.remoteStore, links_path, linkname)),
                          returnResult=True, log=False, session=session) \
                .decode("utf-8").rstrip("\r\n")
    for linkname, target in symlinks.items():
      symlink("../../" + target.lstrip("./"),
              os.path.join(self.workdir, links_path, linkname))

  def upload_symlinks_and_tarball(self, spec) -> None:
    pass

  def upload_shell_command(self, spec):
    """Return None: HTTP backend is read-only."""
    return None

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    """Try to fetch a source archive from the HTTP remote store.

    Returns True if the file was successfully retrieved, False otherwise.
    """
    remote_path = _source_remote_path(url_checksum, filename)
    dest = os.path.join(dest_dir, filename)
    os.makedirs(dest_dir, exist_ok=True)
    result = self.getRetry("{}/{}".format(self.remoteStore, remote_path),
                           dest=dest, log=False)
    if not result and os.path.exists(dest):
      # getRetry returned None/False but may have left a partial file.
      try:
        os.unlink(dest)
      except OSError:
        pass
    return bool(result) and os.path.exists(dest)

  def upload_source(self, local_path, url_checksum, filename) -> None:
    pass  # HTTP backend is read-only; uploads must use rsync/S3/boto3


class RsyncRemoteSync:
  """Helper class to sync package build directory using RSync."""

  def __init__(self, remoteStore, writeStore, architecture, workdir) -> None:
    self.remoteStore = re.sub("^ssh://", "", remoteStore)
    self.writeStore = re.sub("^ssh://", "", writeStore)
    self.architecture = architecture
    self.workdir = workdir

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    info("Downloading tarball for %s@%s, if available", spec["package"], spec["version"])
    debug("Updating remote store for package %s with hashes %s", spec["package"],
          ", ".join(spec["remote_hashes"]))
    err = execute("""\
    for storePath in {storePaths}; do
      # Only get the first matching tarball. If there are multiple with the
      # same hash, we only need one and they should be interchangeable.
      if tars=$(rsync -s --list-only "{remoteStore}/$storePath/{pkg}-{ver}-*.{arch}.tar.gz" 2>/dev/null) &&
         # Strip away the metadata in rsync's file listing, leaving only the first filename.
         tar=$(echo "$tars" | sed -rn '1s#[- a-z0-9,/]* [0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}} ##p') &&
         mkdir -p "{workDir}/$storePath" &&
         # If we already have a file with the same name, assume it's up to date
         # with the remote. In reality, we'll have unpacked, relocated and
         # repacked the tarball from the remote, so the file differs, but
         # there's no point in downloading the one from the remote again.
         rsync -vW --ignore-existing "{remoteStore}/$storePath/$tar" "{workDir}/$storePath/"
      then
        break
      fi
    done
    """.format(pkg=spec["package"], ver=spec["version"], arch=arch,
               remoteStore=self.remoteStore,
               workDir=self.workdir,
               storePaths=" ".join(resolve_store_path(arch, pkg_hash)
                                   for pkg_hash in spec["remote_hashes"])))
    dieOnError(err, "Unable to fetch tarball from specified store.")

  def fetch_symlinks(self, spec) -> None:
    links_path = resolve_links_path(effective_arch(spec, self.architecture), spec["package"])
    os.makedirs(os.path.join(self.workdir, links_path), exist_ok=True)
    err = execute("rsync -rlvW --delete {remote_store}/{links_path}/ {workdir}/{links_path}/".format(
      remote_store=self.remoteStore,
      links_path=links_path,
      workdir=self.workdir,
    ))
    dieOnError(err, "Unable to fetch symlinks from specified store.")

  def _upload_script(self, spec) -> str:
    """Return the formatted rsync shell script for uploading *spec*'s artifacts."""
    arch = effective_arch(spec, self.architecture)
    return """\
set -e
cd {workdir}
tarball={package}-{ver_rev}.{eff_arch}.tar.gz
rsync -avR --ignore-existing "{links_path}/$tarball" {remote}/
for link_dir in dist dist-direct dist-runtime; do
  rsync -avR --ignore-existing "TARS/{build_arch}/$link_dir/{package}/{package}-{ver_rev}/" {remote}/
done
rsync -avR --ignore-existing "{store_path}/$tarball" {remote}/
""".format(
      workdir=self.workdir,
      remote=self.remoteStore,
      store_path=resolve_store_path(arch, spec["hash"]),
      links_path=resolve_links_path(arch, spec["package"]),
      eff_arch=arch,
      build_arch=self.architecture,
      package=spec["package"],
      ver_rev=ver_rev(spec),
    )

  def upload_symlinks_and_tarball(self, spec) -> None:
    if not self.writeStore:
      return
    # ver_rev(spec) is used here instead of "{version}-{revision}" because the
    # tarball filename and the dist-symlink directory name must match what was
    # written to disk by build_template.sh.  When force_revision is set to ""
    # via defaults-*.sh the revision suffix is absent entirely, so the tarball
    # is named "<pkg>-<version>.<arch>.tar.gz".  The content-addressed store
    # path (under TARS/<arch>/store/<h2>/<hash>/) is unaffected — that path
    # always uses the package hash, not the version-revision label.
    dieOnError(execute(self._upload_script(spec)), "Unable to upload tarball.")

  def upload_shell_command(self, spec):
    """Return an inline shell command that uploads *spec*'s tarball and symlinks.

    Used by --pipeline Makeflow .upload rules so that the upload runs as a
    separate Makeflow target, concurrently with downstream package builds.
    Returns None when no write store is configured.
    """
    if not self.writeStore:
      return None
    # Emit the script as a single shell -c '...' invocation so Makeflow can
    # embed it directly in the Makeflow file without a wrapper script.
    script = self._upload_script(spec).replace("'", "'\\''")
    return "bash -e -c '{}'".format(script)

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    """Try to fetch a source archive from the rsync remote store.

    Returns True if the file was successfully retrieved, False otherwise.
    """
    remote_path = _source_remote_path(url_checksum, filename)
    os.makedirs(dest_dir, exist_ok=True)
    err = execute('rsync -vW "{remote}/{path}" "{dest}/" 2>/dev/null'.format(
      remote=self.remoteStore,
      path=remote_path,
      dest=dest_dir,
    ))
    return not err and os.path.exists(os.path.join(dest_dir, filename))

  def upload_source(self, local_path, url_checksum, filename) -> None:
    """Upload a source archive to the rsync write store."""
    if not self.writeStore:
      return
    remote_dir = "SOURCES/cache/{}/{}".format(url_checksum[:2], url_checksum)
    err = execute('rsync -avW --ignore-existing "{src}" "{remote}/{path}/"'.format(
      src=local_path,
      remote=self.writeStore,
      path=remote_dir,
    ))
    dieOnError(err, "Unable to upload source archive to store.")


class CVMFSRemoteSync:
  """ Sync packages build directory from CVMFS or similar
      FS based deployment. The tarball will be created on the fly with a single
      symlink to the remote store in it, so that unpacking really
      means unpacking the symlink to the wanted package.
  """

  def __init__(self, remoteStore, writeStore, architecture, workdir) -> None:
    self.remoteStore = re.sub("^cvmfs://", "", remoteStore)
    # We do not support uploading directly to CVMFS, for obvious
    # reasons.
    assert(writeStore is None)
    self.writeStore = None
    self.architecture = architecture
    self.workdir = workdir

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    info("Downloading tarball for %s@%s-%s, if available", spec["package"], spec["version"], spec["revision"])
    # If we already have a tarball with any equivalent hash, don't check S3.
    for pkg_hash in spec["remote_hashes"] + spec["local_hashes"]:
      store_path = resolve_store_path(arch, pkg_hash)
      pattern = os.path.join(self.workdir, store_path, "%s-*.tar.gz" % spec["package"])
      # Use os.path.isfile() to skip dangling symlinks that glob would otherwise return.
      if any(os.path.isfile(t) for t in glob.glob(pattern)):
        info("Reusing existing tarball for %s@%s", spec["package"], pkg_hash)
        return
    info("Could not find prebuilt tarball for %s@%s-%s, will be rebuilt",
         spec["package"], spec["version"], spec["revision"])

  def fetch_symlinks(self, spec) -> None:
    # When using CVMFS, we create the symlinks grass by reading the .
    info("Fetching available build hashes for %s, from %s", spec["package"], self.remoteStore)
    arch = effective_arch(spec, self.architecture)
    links_path = resolve_links_path(arch, spec["package"])
    os.makedirs(os.path.join(self.workdir, links_path), exist_ok=True)

    cvmfs_architecture = re.sub(r"slc(\d+)_x86-64", r"el\1-x86_64", self.architecture)
    err = execute(r"""\
    set -x
    # Exit without error in case we do not have any package published
    test -d "{remote_store}/{cvmfs_architecture}/Packages/{package}" || exit 0
    mkdir -p "{workDir}/{links_path}"
    for install_path in $(find "{remote_store}/{cvmfs_architecture}/Packages/{package}" -type d -mindepth 1 -maxdepth 1); do
      full_version="${{install_path##*/}}"
      tarball={package}-$full_version.{architecture}.tar.gz
      pkg_hash=$(cat "${{install_path}}/.build-hash" || jq -r '.package.hash' <${{install_path}}/.meta.json)
      if [ "X$pkg_hash" = X ]; then
        continue
      fi
      ln -sf ../../{architecture}/store/${{pkg_hash:0:2}}/$pkg_hash/$tarball "{workDir}/{links_path}/$tarball"
      # Create the dummy tarball, if it does not exists
      test -f "{workDir}/{architecture}/store/${{pkg_hash:0:2}}/$pkg_hash/$tarball" && continue
      mkdir -p "{workDir}/INSTALLROOT/$pkg_hash/{architecture}/{package}"
      find "{remote_store}/{cvmfs_architecture}/Packages/{package}/$full_version" ! -name etc -maxdepth 1 -mindepth 1 -exec ln -sf {{}} "{workDir}/INSTALLROOT/$pkg_hash/{architecture}/{package}/" \\;
      cp -fr "{remote_store}/{cvmfs_architecture}/Packages/{package}/$full_version/etc" "{workDir}/INSTALLROOT/$pkg_hash/{architecture}/{package}/etc"
      mkdir -p "{workDir}/TARS/{architecture}/store/${{pkg_hash:0:2}}/$pkg_hash"
      tar -C "{workDir}/INSTALLROOT/$pkg_hash" -czf "{workDir}/TARS/{architecture}/store/${{pkg_hash:0:2}}/$pkg_hash/$tarball" .
      rm -rf "{workDir}/INSTALLROOT/$pkg_hash"
    done
    """.format(
      workDir=self.workdir,
      architecture=arch,
      cvmfs_architecture=cvmfs_architecture,
      package=spec["package"],
      remote_store=self.remoteStore,
      links_path=links_path,
    ))
    print(f"fetch_symlink: maybe something wrong? {err}")

  def upload_symlinks_and_tarball(self, spec) -> None:
    dieOnError(True, "CVMFS backend does not support uploading directly")

  def upload_shell_command(self, spec):
    """Return None: CVMFS backend is read-only."""
    return None

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    """Try to fetch a source archive from the CVMFS filesystem mount.

    The CVMFS remote store is a read-only filesystem path; we attempt a
    plain file copy from the mirrored SOURCES/cache subtree.
    """
    remote_path = os.path.join(self.remoteStore,
                               _source_remote_path(url_checksum, filename))
    dest = os.path.join(dest_dir, filename)
    if not os.path.exists(remote_path):
      return False
    os.makedirs(dest_dir, exist_ok=True)
    import shutil
    try:
      shutil.copy2(remote_path, dest)
      return True
    except OSError:
      return False

  def upload_source(self, local_path, url_checksum, filename) -> None:
    pass  # CVMFS backend does not support uploading directly


class S3RemoteSync:
  """Sync package build directory from and to S3 using s3cmd.

  s3cmd must be installed separately in order for this to work.
  """

  def __init__(self, remoteStore, writeStore, architecture, workdir) -> None:
    self.remoteStore = re.sub("^s3://", "", remoteStore)
    self.writeStore = re.sub("^s3://", "", writeStore)
    self.architecture = architecture
    self.workdir = workdir

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    info("Downloading tarball for %s@%s, if available", spec["package"], spec["version"])
    debug("Updating remote store for package %s with hashes %s",
          spec["package"], ", ".join(spec["remote_hashes"]))
    err = execute("""\
    for storePath in {storePaths}; do
      # For the first store path that contains tarballs, fetch them, and skip
      # any possible later tarballs (we only need one).
      if [ -n "$(s3cmd ls -s -v --host s3.cern.ch --host-bucket {b}.s3.cern.ch \
                       "s3://{b}/$storePath/")" ]; then
        s3cmd --no-check-md5 sync -s -v --host s3.cern.ch --host-bucket {b}.s3.cern.ch \
              "s3://{b}/$storePath/" "{workDir}/$storePath/" 2>&1 || :
        break
      fi
    done
    """.format(
      workDir=self.workdir,
      b=self.remoteStore,
      storePaths=" ".join(resolve_store_path(arch, pkg_hash)
                          for pkg_hash in spec["remote_hashes"]),
    ))
    dieOnError(err, "Unable to fetch tarball from specified store.")

  def fetch_symlinks(self, spec) -> None:
    err = execute("""\
    mkdir -p "{workDir}/{linksPath}"
    find "{workDir}/{linksPath}" -type l -delete
    curl -sL "https://s3.cern.ch/swift/v1/{b}/{linksPath}.manifest" |
      while IFS='\t' read -r symlink target; do
        ln -sf "../../${{target#../../}}" "{workDir}/{linksPath}/$symlink" || true
      done
    for x in $(curl -sL "https://s3.cern.ch/swift/v1/{b}/?prefix={linksPath}/"); do
      # Skip already existing symlinks -- these were from the manifest.
      # (We delete leftover symlinks from previous runs above.)
      [ -L "{workDir}/{linksPath}/$(basename "$x")" ] && continue
      ln -sf "$(curl -sL "https://s3.cern.ch/swift/v1/{b}/$x" | sed -r 's,^(\\.\\./\\.\\./)?,../../,')" \
         "{workDir}/{linksPath}/$(basename "$x")" || true
    done
    """.format(
      b=self.remoteStore,
      linksPath=resolve_links_path(effective_arch(spec, self.architecture), spec["package"]),
      workDir=self.workdir,
    ))
    dieOnError(err, "Unable to fetch symlinks from specified store.")

  def _upload_script(self, spec) -> str:
    arch = effective_arch(spec, self.architecture)
    return """\
    set -e
    put () {{
      s3cmd put -s -v --host s3.cern.ch --host-bucket {bucket}.s3.cern.ch "$@" 2>&1
    }}
    tarball={package}-{ver_rev}.{eff_arch}.tar.gz
    cd {workdir}

    # First, upload "main" symlink, to reserve this revision number, in case
    # the below steps fail.
    readlink "{links_path}/$tarball" | sed 's|^\\.\\./\\.\\./||' |
      put - "s3://{bucket}/{links_path}/$tarball"

    # Then, upload dist symlink trees -- these must be in place before the main
    # tarball.
    find TARS/{build_arch}/{{dist,dist-direct,dist-runtime}}/{package}/{package}-{ver_rev}/ \
         -type l | while read -r link; do
      hashedurl=$(readlink "$link" | sed 's|.*/\\.\\./TARS|TARS|')
      echo "$hashedurl" |
        put --skip-existing -q -P \\
            --add-header="x-amz-website-redirect-location:\
https://s3.cern.ch/swift/v1/{bucket}/$hashedurl" \\
            - "s3://{bucket}/$link" 2>&1
    done

    # Finally, upload the tarball.
    put "{store_path}/$tarball" s3://{bucket}/{store_path}/
    """.format(
      workdir=self.workdir,
      bucket=self.remoteStore,
      store_path=resolve_store_path(arch, spec["hash"]),
      links_path=resolve_links_path(arch, spec["package"]),
      eff_arch=arch,
      build_arch=self.architecture,
      package=spec["package"],
      ver_rev=ver_rev(spec),
    )

  def upload_symlinks_and_tarball(self, spec) -> None:
    if not self.writeStore:
      return
    # ver_rev(spec) is used here (not "{version}-{revision}") for the same
    # reason as in RsyncRemoteSync: the tarball filename and dist-symlink
    # directory must match what build_template.sh wrote to disk.  If
    # force_revision was set to "" the label has no revision suffix at all.
    dieOnError(execute(self._upload_script(spec)), "Unable to upload tarball.")

  def upload_shell_command(self, spec) -> "str | None":
    """Return an inline shell command that uploads this package's artifacts.

    Returns None if there is no writable store configured.
    Used by the Makeflow .upload rule when --pipeline is active.
    """
    if not self.writeStore:
      return None
    script = self._upload_script(spec)
    escaped = script.replace("'", "'\\''")
    return "bash -e -c '{script}'".format(script=escaped)

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    """Try to fetch a source archive from the S3 (s3cmd) remote store.

    Returns True if the file was successfully retrieved, False otherwise.
    """
    remote_path = _source_remote_path(url_checksum, filename)
    dest = os.path.join(dest_dir, filename)
    os.makedirs(dest_dir, exist_ok=True)
    err = execute("""\
    s3cmd get -s --no-check-md5 --host s3.cern.ch --host-bucket {b}.s3.cern.ch \
          "s3://{b}/{path}" "{dest}" 2>/dev/null
    """.format(b=self.remoteStore, path=remote_path, dest=dest))
    return not err and os.path.exists(dest)

  def upload_source(self, local_path, url_checksum, filename) -> None:
    """Upload a source archive to the S3 (s3cmd) write store."""
    if not self.writeStore:
      return
    remote_path = _source_remote_path(url_checksum, filename)
    err = execute("""\
    s3cmd put -s -v --host s3.cern.ch --host-bucket {b}.s3.cern.ch \
          --skip-existing "{src}" "s3://{b}/{path}" 2>&1
    """.format(b=self.writeStore, src=local_path, path=remote_path))
    dieOnError(err, "Unable to upload source archive to store.")


class Boto3RemoteSync:
  """Sync package build directory from and to S3 using boto3.

  As boto3 doesn't support Python 2, this class can only be used under Python
  3. boto3 is only imported at __init__ time, so if this class is never
  instantiated, boto3 doesn't have to be installed.

  This class has the advantage over S3RemoteSync that it uses the same
  connection to S3 every time, while s3cmd must establish a new connection each
  time.
  """

  def __init__(self, remoteStore, writeStore, architecture, workdir) -> None:
    self._remote_url = remoteStore   # original URL (with b3:// prefix) for upload_shell_command
    self._write_url = writeStore     # original URL (with b3:// prefix) for upload_shell_command
    self.remoteStore = re.sub("^b3://", "", remoteStore)
    self.writeStore = re.sub("^b3://", "", writeStore)
    self.architecture = architecture
    self.workdir = workdir
    self._s3_init()

  def _s3_init(self) -> None:
    # This is a separate method so that we can patch it out for unit tests.
    # Import boto3 here, so that if we don't use this remote store, we don't
    # have to install it in the first place.
    try:
      import boto3
      from botocore.config import Config
    except ImportError:
      error("boto3 must be installed to use %s", Boto3RemoteSync)
      sys.exit(1)

    # Connection settings from the environment (resolve_and_export_s3_config sets
    # them): endpoint defaults to CERN S3; region and addressing style support
    # non-CERN buckets (MinIO usually needs addressing_style='path').
    endpoint = os.environ.get("S3_ENDPOINT_URL") or DEFAULT_S3_ENDPOINT
    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    addressing_style = os.environ.get("S3_ADDRESSING_STYLE")
    config_kwargs = {"s3": {"addressing_style": addressing_style}} if addressing_style else {}
    try:
      try:
        config = Config(
          request_checksum_calculation='WHEN_REQUIRED',
          response_checksum_validation='WHEN_REQUIRED',
          **config_kwargs,
        )
      except TypeError:
        # Older boto3 versions don't support the checksum parameters (<1.36.0);
        # still honour the addressing style if one was requested.
        config = Config(**config_kwargs) if config_kwargs else None
      client_kwargs = {
        "endpoint_url": endpoint,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
      }
      # Temporary/STS credentials (e.g. from ~/.bits/s3keys) carry a session token.
      if os.environ.get("AWS_SESSION_TOKEN"):
        client_kwargs["aws_session_token"] = os.environ["AWS_SESSION_TOKEN"]
      if region:
        client_kwargs["region_name"] = region
      if config:
        client_kwargs["config"] = config
      self.s3 = boto3.client("s3", **client_kwargs)
    except KeyError:
      error("set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (via a CI/CD "
            "variable, the gitlab-runner `environment`, or --s3-access-key / "
            "--s3-secret-key) to use an S3 remote store")
      sys.exit(1)

  def _s3_listdir(self, dirname):
    """List keys of items under dirname in the read bucket."""
    pages = self.s3.get_paginator("list_objects_v2") \
                   .paginate(Bucket=self.remoteStore, Delimiter="/",
                             Prefix=dirname.rstrip("/") + "/")
    return (item["Key"] for pg in pages for item in pg.get("Contents", ()))

  def _s3_key_exists(self, key):
    """Return whether the given key exists in the write bucket already."""
    from botocore.exceptions import ClientError
    try:
      self.s3.head_object(Bucket=self.writeStore, Key=key)
    except ClientError as err:
      if err.response["Error"]["Code"] == "404":
        return False
      raise
    return True

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    debug("Updating remote store for package %s with hashes %s", spec["package"],
          ", ".join(spec["remote_hashes"]))

    # If we already have a tarball with any equivalent hash, don't check S3.
    for pkg_hash in spec["remote_hashes"]:
      store_path = resolve_store_path(arch, pkg_hash)
      # Use os.path.isfile() to skip dangling symlinks that glob would otherwise return.
      if any(os.path.isfile(t) for t in glob.glob(
          os.path.join(self.workdir, store_path, "%s-*.tar.gz" % spec["package"]))):
        debug("Reusing existing tarball for %s@%s", spec["package"], pkg_hash)
        return

    for pkg_hash in spec["remote_hashes"]:
      store_path = resolve_store_path(arch, pkg_hash)

      # We don't already have a tarball with the hash that we need, so download
      # the first existing one from the remote, if possible. (Downloading more
      # than one is a waste of time as they should be equivalent and we only
      # ever use one anyway.)
      for tarball in self._s3_listdir(store_path):
        debug("Fetching tarball %s", tarball)
        progress = ProgressPrint("Downloading tarball for %s@%s" %
                                 (spec["package"], spec["version"]), min_interval=5.0)
        progress("[0%%] Starting download of %s", tarball)   # initialise progress bar
        # Create containing directory locally. (exist_ok= is python3-specific.)
        os.makedirs(os.path.join(self.workdir, store_path), exist_ok=True)
        meta = self.s3.head_object(Bucket=self.remoteStore, Key=tarball)
        total_size = int(meta.get("ContentLength", 0))
        self.s3.download_file(
          Bucket=self.remoteStore, Key=tarball,
          Filename=os.path.join(self.workdir, store_path, os.path.basename(tarball)),
          Callback=lambda num_bytes: progress("[%d/%d] bytes transferred", num_bytes, total_size),
        )
        progress.end("done")
        return

    debug("Remote has no tarballs for %s with hashes %s", spec["package"],
          ", ".join(spec["remote_hashes"]))

  def fetch_symlinks(self, spec) -> None:
    from botocore.exceptions import ClientError
    links_path = resolve_links_path(effective_arch(spec, self.architecture), spec["package"])
    os.makedirs(os.path.join(self.workdir, links_path), exist_ok=True)

    # Remove existing symlinks: we'll fetch the ones from the remote next.
    parent = os.path.join(self.workdir, links_path)
    for fname in os.listdir(parent):
      path = os.path.join(parent, fname)
      if os.path.islink(path):
        os.unlink(path)

    # Fetch symlink manifest and create local symlinks to match.
    debug("Fetching symlink manifest")
    n_symlinks = 0
    try:
      manifest = self.s3.get_object(Bucket=self.remoteStore, Key=links_path + ".manifest")
    except ClientError as exc:
      debug("Could not fetch manifest: %s", exc)
    else:
      for line in manifest["Body"].iter_lines():
        link_name, has_sep, target = line.rstrip(b"\n").partition(b"\t")
        if not has_sep:
          debug("Ignoring malformed line in manifest: %r", line)
          continue
        if not target.startswith(b"../../"):
          target = b"../../" + target
        target = os.fsdecode(target)
        link_path = os.path.join(self.workdir, links_path, os.fsdecode(link_name))
        symlink(target, link_path)
        n_symlinks += 1
      debug("Got %d entries in manifest", n_symlinks)

    # Create remote symlinks that aren't in the manifest yet.
    debug("Looking for symlinks not in manifest")
    for link_key in self._s3_listdir(links_path):
      link_path = os.path.join(self.workdir, link_key)
      if os.path.islink(link_path):
        continue
      debug("Fetching leftover symlink %s", link_key)
      resp = self.s3.get_object(Bucket=self.remoteStore, Key=link_key)
      target = os.fsdecode(resp["Body"].read()).rstrip("\n")
      if not target.startswith("../../"):
        target = "../../" + target
      symlink(target, link_path)

  def upload_symlinks_and_tarball(self, spec) -> None:
    if not self.writeStore:
      return

    arch = effective_arch(spec, self.architecture)
    dist_symlinks = {}
    for link_dir in ("dist", "dist-direct", "dist-runtime"):
      # ver_rev(spec) ensures the dist-symlink directory name matches what
      # build_template.sh created; with force_revision="" the name has no
      # revision suffix (e.g. "pkg-1.2.3" instead of "pkg-1.2.3-1").
      link_dir = "TARS/{arch}/{link_dir}/{package}/{package}-{ver_rev}" \
        .format(arch=self.architecture, link_dir=link_dir,
                ver_rev=ver_rev(spec), **spec)

      debug("Comparing dist symlinks against S3 from %s", link_dir)

      symlinks = []
      for fname in os.listdir(os.path.join(self.workdir, link_dir)):
        link_key = os.path.join(link_dir, fname)
        path = os.path.join(self.workdir, link_key)
        if os.path.islink(path):
          hash_path = re.sub(r"^(\.\./)*", "", os.readlink(path))
          symlinks.append((link_key, hash_path))

      # To make sure there are no conflicts, see if anything already exists in
      # our symlink directory.
      symlinks_existing = frozenset(self._s3_listdir(link_dir))

      # If all the symlinks we would upload already exist, skip uploading. We
      # probably just downloaded a prebuilt package earlier, and it already has
      # symlinks available.
      if all(link_key in symlinks_existing for link_key, _ in symlinks):
        debug("All %s symlinks already exist on S3, skipping upload", link_dir)
        continue

      # Excluding our own symlinks (above), if there is anything in our link_dir
      # on the remote, something else is uploading symlinks (or already has)!
      dieOnError(symlinks_existing,
                 "Conflicts detected in %s on S3; aborting: %s" %
                 (link_dir, ", ".join(sorted(symlinks_existing))))

      dist_symlinks[link_dir] = symlinks

    # ver_rev(spec) keeps the filename consistent with what build_template.sh
    # wrote: PACKAGE_WITH_REV=$PKGNAME-$VERREV.$EFFECTIVE_ARCHITECTURE.tar.gz.
    # `arch` is already effective_arch(spec, self.architecture) — i.e. "shared"
    # for shared packages, else the build arch — matching EFFECTIVE_ARCHITECTURE
    # and the store/link paths below, exactly like the rsync backend's eff_arch.
    # The fields are passed explicitly: a bare **spec collides with the
    # architecture= keyword whenever the spec carries an "architecture" key
    # (shared packages, or any recipe that sets the field), raising
    # "TypeError: got multiple values for keyword argument 'architecture'".
    # The content-addressed store key (store/<h2>/<hash>/) is unaffected; it
    # always uses the package hash rather than the version-revision label.
    tarball = "{package}-{ver_rev}.{architecture}.tar.gz".format(
        package=spec["package"], ver_rev=ver_rev(spec), architecture=arch)
    tar_path = os.path.join(resolve_store_path(arch, spec["hash"]),
                            tarball)
    link_path = os.path.join(resolve_links_path(arch, spec["package"]),
                             tarball)
    tar_exists = self._s3_key_exists(tar_path)
    link_exists = self._s3_key_exists(link_path)
    if tar_exists and link_exists:
      debug("%s exists on S3 already, not uploading", tarball)
      return
    dieOnError(tar_exists or link_exists,
               "%s already exists on S3 but %s does not, aborting!" %
               (tar_path if tar_exists else link_path,
                link_path if tar_exists else tar_path))

    debug("Uploading tarball and symlinks for %s %s-%s (%s) to S3",
          spec["package"], spec["version"], spec["revision"], spec["hash"])

    # Upload the smaller file first, so that any parallel uploads are more
    # likely to find it and fail.
    try:
      os.readlink(os.path.join(self.workdir, link_path))
    except FileNotFoundError:
      # ver_rev(spec) keeps the symlink target consistent with the on-disk
      # tarball name created by build_template.sh (which uses $_VERREV).
      os.symlink(
        os.path.join('../..', arch, 'store', spec["hash"][:2], spec["hash"],
                     f"{spec['package']}-{ver_rev(spec)}.{arch}.tar.gz"),
        os.path.join(self.workdir, link_path)
      )

    self.s3.put_object(Bucket=self.writeStore, Key=link_path,
                       Body=os.readlink(os.path.join(self.workdir, link_path))
                              .lstrip("./").encode("utf-8"))

    # Second, upload dist symlinks. These should be in place before the main
    # tarball, to avoid races in the publisher.
    start_time = time.time()
    total_symlinks = 0

    # Limit concurrency to avoid overwhelming S3 with too many simultaneous requests
    max_workers = min(32, (len(dist_symlinks) * 10) or 1)

    def _upload_single_symlink(link_key, hash_path):
      self.s3.put_object(Bucket=self.writeStore,
                         Key=link_key,
                         Body=os.fsencode(hash_path),
                         ACL="public-read",
                         WebsiteRedirectLocation=hash_path)
      return link_key

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
      future_to_info = {}
      for link_dir, symlinks in dist_symlinks.items():
        for link_key, hash_path in symlinks:
          future = executor.submit(_upload_single_symlink, link_key, hash_path)
          future_to_info[future] = (link_dir, link_key)
          total_symlinks += 1

      dir_counts = {link_dir: 0 for link_dir in dist_symlinks.keys()}
      for future in as_completed(future_to_info):
        link_dir, link_key = future_to_info[future]
        try:
          future.result()
          dir_counts[link_dir] += 1
        except Exception as e:
          error("Failed to upload symlink %s: %s", link_key, e)
          raise

      for link_dir, count in dir_counts.items():
        if count > 0:
          debug("Uploaded %d dist symlinks to S3 from %s", count, link_dir)

    end_time = time.time()
    debug("Uploaded %d dist symlinks in %.2f seconds",
          total_symlinks, end_time - start_time)

    self.s3.upload_file(Bucket=self.writeStore, Key=tar_path,
                        Filename=os.path.join(self.workdir, tar_path))

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    """Try to fetch a source archive from the boto3/S3 remote store.

    Returns True if the file was successfully retrieved, False otherwise.
    """
    from botocore.exceptions import ClientError
    remote_key = _source_remote_path(url_checksum, filename)
    dest = os.path.join(dest_dir, filename)
    os.makedirs(dest_dir, exist_ok=True)
    try:
      self.s3.download_file(Bucket=self.remoteStore, Key=remote_key, Filename=dest)
    except ClientError as exc:
      code = exc.response["Error"]["Code"]
      if code in ("404", "NoSuchKey"):
        debug("Source archive %s not found in remote store", filename)
        return False
      raise
    return True

  def upload_source(self, local_path, url_checksum, filename) -> None:
    """Upload a source archive to the boto3/S3 write store."""
    if not self.writeStore:
      return
    remote_key = _source_remote_path(url_checksum, filename)
    if self._s3_key_exists(remote_key):
      debug("Source archive %s already in remote store, skipping upload", filename)
      return
    debug("Uploading source archive %s to S3 (%s)", filename, remote_key)
    self.s3.upload_file(Bucket=self.writeStore, Key=remote_key,
                        Filename=local_path)

  def upload_shell_command(self, spec) -> "str | None":
    """Return a shell command that uploads this package's artifacts via upload_cmd.py.

    Returns None if there is no writable store configured.
    Used by the Makeflow .upload rule when --pipeline is active.
    The actual upload logic lives in bits_helpers/upload_cmd.py, which reads
    PKGNAME/PKGVERSION/PKGREVISION/PKGHASH from the environment and accepts
    the store URLs as CLI arguments.
    """
    if not self.writeStore:
      return None
    return (
      "python3 -m bits_helpers.upload_cmd"
      " --remote-store {remote}"
      " --write-store {write}"
      " --work-dir {workdir}"
      " --architecture {arch}"
    ).format(
      remote=self._remote_url,
      write=self._write_url,
      workdir=self.workdir,
      arch=self.architecture,
    )
