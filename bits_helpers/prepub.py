# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits_helpers.prepub — HTTP client for the cvmfs-prepub REST API.

This module is used by ``bits publish --prepub-url`` to submit a tar archive
to a running cvmfs-prepub service and wait for the publish to complete.

API contract (cvmfs-prepub ≥ 0.1.0)
-------------------------------------
* ``POST /api/v1/jobs``  (multipart/form-data)
  Fields:
    ``repo``        — CVMFS repository name, e.g. ``software.cern.ch``
    ``path``        — lease sub-path relative to the repo root,
                      e.g. ``atlas/24.0`` (no leading slash)
    ``tar``         — the .tar or .tar.gz file (binary field)
    ``tar_sha256``  — (optional) hex SHA-256 of the tar; verified server-side
    ``webhook_url`` — (optional) URL to POST on terminal state
  Returns: ``202 Accepted`` + JSON ``{"job_id": "<uuid>"}``

* ``GET /api/v1/jobs/<id>``
  Returns: JSON ``{"job_id": "...", "state": "...", "error": "..."}``
  Terminal states: ``published``, ``failed``, ``aborted``

Authentication
--------------
Pass the bearer token in the ``Authorization: Bearer <token>`` header.
The token is read from (in priority order):

1. The ``--prepub-token`` CLI argument.
2. The ``PREPUB_API_TOKEN`` environment variable.

Derivation of repo and sub-path from ``--cvmfs-target``
--------------------------------------------------------
A CVMFS target path of the form ``/cvmfs/<repo>/<subpath>`` is split into
``repo = <repo>`` and ``path = <subpath>`` automatically.  Both can be
overridden explicitly with ``--prepub-repo`` and ``--prepub-path``.
"""

import hashlib
import io
import os
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter, Retry

from bits_helpers.log import debug, error, info


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 10      # seconds between status polls
DEFAULT_TIMEOUT       = 1800    # 30 minutes total poll budget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(token: str, no_verify_tls: bool = False) -> requests.Session:
    """Return a requests.Session with auth header and retry back-off.

    The Bearer token is NEVER attached when TLS verification is disabled: an
    unverified connection can be intercepted, and a captured token grants
    publish rights. --prepub-no-verify-tls therefore only works against an
    endpoint that does not require authentication (e.g. a local test
    instance); requests needing the token must use a verified connection.
    """
    session = requests.Session()
    if token and no_verify_tls:
        error("prepub: refusing to send the Bearer token over a "
              "TLS-verification-disabled connection (--prepub-no-verify-tls); "
              "requests will be anonymous. Use a verified endpoint for "
              "authenticated publishing.")
        token = ""
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    session.verify = not no_verify_tls
    # Retry on transient server errors and connection resets, but NOT on
    # the polling GET (status 200) — only on 5xx and connection failures.
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://",  HTTPAdapter(max_retries=retry))
    return session


def sha256_file(path: str) -> str:
    """Return the lowercase hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cvmfs_repo_and_path(cvmfs_target: str):
    """Decompose ``/cvmfs/<repo>/<subpath>`` into ``(repo, subpath)``.

    Returns ``(repo, subpath)`` where *subpath* has no leading slash.
    Raises ``ValueError`` when *cvmfs_target* does not start with ``/cvmfs/``
    or contains only the repo component with no sub-path.

    Examples::

        /cvmfs/software.cern.ch/lcg/releases  →  ("software.cern.ch", "lcg/releases")
        /cvmfs/atlas.cern.ch                  →  ValueError (no sub-path)
    """
    if not cvmfs_target.startswith("/cvmfs/"):
        raise ValueError(
            f"--cvmfs-target {cvmfs_target!r} does not start with /cvmfs/; "
            "cannot derive repository name.  Pass --prepub-repo and --prepub-path explicitly."
        )
    rest = cvmfs_target[len("/cvmfs/"):]  # e.g. "software.cern.ch/lcg/releases"
    if "/" not in rest:
        raise ValueError(
            f"--cvmfs-target {cvmfs_target!r} has no sub-path after the repository name; "
            "pass --prepub-path explicitly."
        )
    repo, _, subpath = rest.partition("/")
    subpath = subpath.lstrip("/")
    if not subpath:
        raise ValueError(
            f"--cvmfs-target {cvmfs_target!r} has an empty sub-path; "
            "pass --prepub-path explicitly."
        )
    return repo, subpath


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_token(cli_token: Optional[str]) -> str:
    """Return the bearer token from *cli_token* or ``PREPUB_API_TOKEN`` env var.

    Returns an empty string when neither is set (dev/no-auth mode).
    """
    if cli_token:
        return cli_token
    return os.environ.get("PREPUB_API_TOKEN", "")


def submit_job(
    prepub_url:    str,
    token:         str,
    repo:          str,
    path:          str,
    tar_path:      str,
    webhook_url:   Optional[str] = None,
    no_verify_tls: bool = False,
) -> str:
    """Submit a tar archive to cvmfs-prepub and return the assigned job ID.

    Parameters
    ----------
    prepub_url:
        Base URL of the cvmfs-prepub API (no trailing slash), e.g.
        ``https://prepub.example.org:8080``.
    token:
        Bearer token for authentication.  Pass ``""`` for dev mode.
    repo:
        CVMFS repository name, e.g. ``software.cern.ch``.
    path:
        Lease sub-path relative to the repo root, e.g. ``atlas/24.0``.
        Must not have a leading or trailing slash.
    tar_path:
        Local filesystem path to the .tar or .tar.gz file to upload.
    webhook_url:
        Optional URL that cvmfs-prepub should POST to when the job reaches
        a terminal state.
    no_verify_tls:
        Disable TLS certificate verification (dev/self-signed certs only).

    Returns
    -------
    str
        The UUID job ID assigned by the server.

    Raises
    ------
    SystemExit
        On HTTP errors, network failures, or malformed server responses.
    """
    url = f"{prepub_url.rstrip('/')}/api/v1/jobs"
    digest = sha256_file(tar_path)
    tar_size = os.path.getsize(tar_path)

    info("Submitting %s (%d MiB) to cvmfs-prepub …",
         os.path.basename(tar_path), tar_size >> 20)
    debug("  POST %s  repo=%s  path=%s  sha256=%s", url, repo, path, digest)

    session = _make_session(token, no_verify_tls)
    try:
        with open(tar_path, "rb") as tar_fh:
            fields = {
                "repo":       (None, repo),
                "path":       (None, path),
                "tar":        (os.path.basename(tar_path), tar_fh, "application/octet-stream"),
                "tar_sha256": (None, digest),
            }
            if webhook_url:
                fields["webhook_url"] = (None, webhook_url)

            resp = session.post(url, files=fields, timeout=300)
    except requests.RequestException as exc:
        error("Failed to connect to cvmfs-prepub at %s: %s", prepub_url, exc)
        raise SystemExit(1) from exc

    if resp.status_code != 202:
        error(
            "cvmfs-prepub rejected the job submission (HTTP %d):\n%s",
            resp.status_code,
            resp.text[:2000],
        )
        raise SystemExit(1)

    try:
        job_id = resp.json()["job_id"]
    except (ValueError, KeyError) as exc:
        error("Unexpected response from cvmfs-prepub: %s\n%s", exc, resp.text[:500])
        raise SystemExit(1) from exc

    info("Job submitted: %s", job_id)
    return job_id


def poll_job(
    prepub_url:    str,
    token:         str,
    job_id:        str,
    poll_interval: int  = DEFAULT_POLL_INTERVAL,
    timeout:       int  = DEFAULT_TIMEOUT,
    no_verify_tls: bool = False,
) -> str:
    """Poll ``GET /api/v1/jobs/<id>`` until the job reaches a terminal state.

    Parameters
    ----------
    prepub_url:
        Base URL of the cvmfs-prepub API.
    token:
        Bearer token for authentication.
    job_id:
        UUID returned by :func:`submit_job`.
    poll_interval:
        Seconds between status polls.  Default: 10.
    timeout:
        Maximum total seconds to wait.  Default: 1800 (30 min).
    no_verify_tls:
        Disable TLS certificate verification.

    Returns
    -------
    str
        The terminal state string: ``"published"``, ``"failed"``, or
        ``"aborted"``.

    Raises
    ------
    SystemExit
        When the job fails, is aborted, or the timeout is exceeded.
    """
    url = f"{prepub_url.rstrip('/')}/api/v1/jobs/{job_id}"
    session    = _make_session(token, no_verify_tls)
    deadline   = time.monotonic() + timeout
    attempt    = 0
    last_state = ""

    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error("Timeout waiting for job %s after %ds", job_id, timeout)
            raise SystemExit(1)

        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as exc:
            # Transient network issue — log and keep waiting.
            debug("Poll attempt %d failed (network): %s", attempt, exc)
            time.sleep(min(poll_interval, remaining))
            continue

        if resp.status_code != 200:
            debug("Poll attempt %d: HTTP %d", attempt, resp.status_code)
            time.sleep(min(poll_interval, remaining))
            continue

        try:
            data  = resp.json()
            state = data.get("state", "")
        except ValueError as exc:
            debug("Poll attempt %d: malformed JSON (%s)", attempt, exc)
            time.sleep(min(poll_interval, remaining))
            continue

        if state != last_state:
            info("[%d] Job %s: %s", attempt, job_id, state)
            last_state = state

        if state == "published":
            info("Job %s published successfully.", job_id)
            return state

        if state in ("failed", "aborted"):
            srv_error = data.get("error", "")
            msg = f"Job {job_id} reached terminal state '{state}'"
            if srv_error:
                msg += f": {srv_error}"
            error("%s", msg)
            raise SystemExit(1)

        time.sleep(min(poll_interval, remaining))
