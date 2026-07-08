# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""progress.py — best-effort build-progress reporting via a GitLab commit status.

bits knows the build plan (total package count) and sees each package start, so it
*reports* progress rather than anyone having to parse the job log. As each package
begins, a GitLab commit status is updated with a numeric ``coverage`` (the percent
for a progress bar) and a ``description`` like ``12/27 · ROOT``. bits-console then
reads one small statuses call per running pipeline — no log download, immune to
``--debug`` log verbosity.

Active only when bits runs under gitlab-runner (``GITLAB_CI=true``) with the
``CI_*`` variables present. Every call is best-effort: network/credential failures
are swallowed so a build is never broken by progress reporting.

The commit-status *context* is per-pipeline (``bits-build-progress/<pipeline id>``)
so concurrent pipelines on the same commit SHA do not overwrite each other. The
terminal status (success/failed) is posted by the CI job's after_script, which
always runs and knows the final job result.
"""

import json
import os
import threading
import urllib.error
import urllib.request

from bits_helpers.log import debug, warning

_lock = threading.Lock()
_state = {
    "ready":   None,   # None = not yet probed; True/False after _probe()
    "total":   0,
    "done":    0,
    "url":     None,
    "headers": None,
    "context": None,
    "pipeline_id": None,
    "ref":     None,
}


def _probe():
    """Resolve the CI environment once. Returns True if posting is possible."""
    if _state["ready"] is not None:
        return _state["ready"]
    env = os.environ
    ready = env.get("GITLAB_CI") == "true" and all(
        env.get(k) for k in ("CI_API_V4_URL", "CI_PROJECT_ID",
                              "CI_COMMIT_SHA", "CI_PIPELINE_ID"))
    if ready:
        _state["url"] = "{}/projects/{}/statuses/{}".format(
            env["CI_API_V4_URL"], env["CI_PROJECT_ID"], env["CI_COMMIT_SHA"])
        headers = {"Content-Type": "application/json"}
        # Commit-status creation needs a token with 'api' scope and >= Developer
        # access (BITS_STATUS_TOKEN). The CI job token is rejected with 403, so
        # without BITS_STATUS_TOKEN progress reporting is effectively off.
        if env.get("BITS_STATUS_TOKEN"):
            headers["PRIVATE-TOKEN"] = env["BITS_STATUS_TOKEN"]
        else:
            headers["JOB-TOKEN"] = env.get("CI_JOB_TOKEN", "")
        _state["headers"] = headers
        _state["context"] = "bits-build-progress/{}".format(env["CI_PIPELINE_ID"])
        _state["pipeline_id"] = int(env["CI_PIPELINE_ID"])
        _state["ref"] = env.get("CI_COMMIT_REF_NAME") or None
    _state["ready"] = ready
    return ready


def _do_post(headers, body):
    """POST once. Return (None, "") on success, else (status_code, response_body).

    The response body carries GitLab's own reason for a 4xx (e.g. an invalid
    state transition, a protected-ref rule, or a token restriction) — far more
    useful than the bare status code, so we capture and surface it.
    """
    req = urllib.request.Request(
        _state["url"], data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
        return (None, "")
    except urllib.error.HTTPError as exc:            # never break a build over this
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:400]
        except Exception:
            detail = ""
        return (exc.code, detail)
    except Exception as exc:
        return (-1, str(exc))


def _post(state, coverage, description):
    if not _probe():
        return
    body = {
        "state":       state,
        "name":        _state["context"],
        "description": description[:255],
        "coverage":    coverage,
        "pipeline_id": _state["pipeline_id"],
    }
    if _state["ref"]:
        body["ref"] = _state["ref"]

    code, detail = _do_post(_state["headers"], body)
    if code is None:
        return                                       # posted

    # Progress reporting is best-effort. On a permission failure, fall back to the
    # CI job token once (harmless — some setups accept it), then give up for the
    # run — but surface GitLab's own reason ONCE so a real cause isn't hidden.
    if code in (401, 403):
        jt = os.environ.get("CI_JOB_TOKEN")
        used_status_token = "PRIVATE-TOKEN" in _state["headers"]
        if used_status_token and jt:
            c2, _d2 = _do_post({"Content-Type": "application/json", "JOB-TOKEN": jt}, body)
            if c2 is None:
                _state["headers"] = {"Content-Type": "application/json", "JOB-TOKEN": jt}
                return                               # job token worked; keep using it
        _state["ready"] = False                      # stop trying this run
        if not _state.get("warned"):
            _state["warned"] = True
            warning("progress: commit-status POST to %s returned HTTP %s%s; "
                    "build-progress reporting disabled for this run",
                    _state["url"], code,
                    (" — GitLab: %s" % detail) if detail else "")
    else:
        debug("progress: commit-status post failed: HTTP %s %s", code, detail)


def set_total(total):
    """Record how many packages will be built and reset the counter."""
    with _lock:
        _state["total"] = int(total or 0)
        _state["done"] = 0


def tick(package):
    """Mark another package as started and push the updated progress status."""
    if not _probe():
        return
    with _lock:
        _state["done"] += 1
        done = _state["done"]
        total = _state["total"] or done
    pct = max(0, min(100, round(done * 100.0 / total))) if total else 0
    _post("running", pct, "{}/{} · {}".format(done, total, package))


def finish(success=True):
    """Post a terminal status. Normally called from the CI after_script."""
    with _lock:
        total = _state["total"] or _state["done"]
    _post("success" if success else "failed", 100, "{0}/{0} done".format(total))
