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
The shared secret is read from (in priority order):

1. The ``--prepub-token`` CLI argument.
2. The ``PREPUB_API_TOKEN`` environment variable.

It is used one of two ways, matching the server's ``auth_mode``:

* **signed** (default) — the secret stays here and each request carries an
  ``X-Bits-Auth`` MAC bound to its method, URI, fields and payload
  (:mod:`bits_helpers.httpsig`, ADR-0008 D3). Observing a request yields
  nothing reusable.
* **bearer** (``--prepub-bearer-auth``) — the legacy header, for a server
  still running ``auth_mode: bearer``. The secret travels on every request.

Signed and bearer are mutually exclusive: sending both would put the secret on
the wire anyway.

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

from bits_helpers import httpsig
from bits_helpers.log import debug, error, info


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 10      # seconds between status polls
DEFAULT_TIMEOUT       = 1800    # 30 minutes total poll budget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(no_verify_tls: bool = False) -> requests.Session:
    """Return a requests.Session with retry back-off and no credential.

    The credential is deliberately NOT set here. A signature covers one
    request — its method, URI, field set and payload digest — so it cannot be
    a session-wide header the way a bearer token was; each call site signs its
    own request via :func:`_auth_headers`.
    """
    session = requests.Session()
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


def _auth_headers(
    token:         str,
    method:        str,
    uri:           str,
    fields:        Optional[dict] = None,
    body_hash:     str = httpsig.NO_BODY,
    bearer_auth:   bool = False,
    no_verify_tls: bool = False,
) -> dict:
    """Build the auth header for one request.

    The TLS caveat applies only to the bearer: an unverified connection can be
    intercepted, and a captured token grants publish rights until it is
    rotated. A signature has no such exposure — the secret never leaves this
    process and the MAC is useless for any other request — so signing over an
    unverified connection is refused only for the bearer path.
    """
    if not token:
        return {}
    if bearer_auth:
        if no_verify_tls:
            error("prepub: refusing to send the Bearer token over a "
                  "TLS-verification-disabled connection "
                  "(--prepub-no-verify-tls); requests will be anonymous. "
                  "Drop --prepub-bearer-auth to sign instead, which does not "
                  "put the secret on the wire.")
            return {}
        return {"Authorization": f"Bearer {token}"}
    return {httpsig.HEADER_NAME: httpsig.sign(token, method, uri, fields, body_hash)}


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
    bearer_auth:   bool = False,
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
    bearer_auth:
        Send the legacy ``Authorization: Bearer`` header instead of signing.
        Only for a server running ``auth_mode: bearer``; the secret then
        travels on every request.

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

    session = _make_session(no_verify_tls)

    # The signed field set must be EXACTLY what the server parses, or the
    # digests differ and the request is refused. tar_sha256 is what ties the
    # signature to the payload, so a signed submission must carry it — which
    # this client already did, for integrity.
    signed_fields = {"repo": repo, "path": path, "tar_sha256": digest}
    if webhook_url:
        signed_fields["webhook_url"] = webhook_url

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

            headers = _auth_headers(
                token, "POST", "/api/v1/jobs",
                fields=signed_fields, body_hash=digest,
                bearer_auth=bearer_auth, no_verify_tls=no_verify_tls)
            resp = session.post(url, files=fields, headers=headers, timeout=300)
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
    bearer_auth:   bool = False,
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
    bearer_auth:
        Send the legacy bearer header instead of signing.

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
    uri        = f"/api/v1/jobs/{job_id}"
    session    = _make_session(no_verify_tls)
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
            # Signed per attempt, not once: a signature is single-use, so a
            # reused one would be rejected as a replay on the second poll.
            resp = session.get(url, timeout=30, headers=_auth_headers(
                token, "GET", uri,
                bearer_auth=bearer_auth, no_verify_tls=no_verify_tls))
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
