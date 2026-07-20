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
import time
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

    # Progress reporting is best-effort. On a permission failure, DISABLE it for
    # the run — do NOT retry with the CI job token.
    #
    # GitLab updates an existing commit status only when it matches on
    # (name, USER, sha); otherwise it creates a NEW status. The terminal state is
    # posted by the CI after_script as the BITS_STATUS_TOKEN user. If the running
    # status here were posted as a *different* user (the job token), that terminal
    # POST would not match it — GitLab would add a second, terminal status and the
    # running one would never close, leaving the pipeline stuck "running" (and the
    # job, in GitLab and the console). So we only ever post as the
    # BITS_STATUS_TOKEN user, or not at all. Surface GitLab's reason once.
    if code in (401, 403):
        _state["ready"] = False                      # stop trying this run
        if not _state.get("warned"):
            _state["warned"] = True
            warning("progress: commit-status POST to %s returned HTTP %s%s; "
                    "build-progress reporting disabled for this run (not falling "
                    "back to the job token: that posts as a different user and "
                    "would leave the pipeline stuck 'running')",
                    _state["url"], code,
                    (" — GitLab: %s" % detail) if detail else "")
    else:
        debug("progress: commit-status post failed: HTTP %s %s", code, detail)


def set_total(total):
    """Record how many packages will be built and reset the counter."""
    with _lock:
        _state["total"] = int(total or 0)
        _state["done"] = 0
        _state["posted"] = False
        _state["last_pct"] = None
        _state["last_ts"] = 0.0


def tick(package):
    """Mark another package as started and push the updated progress status.

    GitLab cannot UPDATE a running commit status: the API assigns the new
    coverage/description but then fires the ``run!`` state transition, which is
    invalid from ``running`` — the whole update rolls back with HTTP 400
    ("Cannot transition status via :run from :running"). Posting ``running`` on
    every tick therefore left the console's progress bar stuck at its first
    value until the terminal status jumped it to 100% ("0 or 100, nothing in
    between").

    So after the first post, every update CANCELS the current status (a valid
    transition from running, freeing the (pipeline, name, user, ref) slot) and
    immediately re-posts ``running`` with the new coverage. The re-post creates
    a fresh status row and GitLab marks the canceled one retried in the same
    transaction (``update_older_statuses_retried!``), so the pipeline's
    composite status and the console (which read only the latest row per name)
    are never polluted. A failed cancel is ignored: the follow-up ``running``
    post then either updates a pending status or re-creates the slot, so the
    pair self-heals.

    An update costs two API calls, so ticks are throttled: skip when the
    rounded percent has not moved (unless 30 s passed, to refresh the package
    name in the description) and allow at most one post per 2 s (reused
    packages tick in bursts). The final tick is never skipped.
    """
    if not _probe():
        return
    with _lock:
        _state["done"] += 1
        done = _state["done"]
        total = _state["total"] or done
        pct = max(0, min(100, round(done * 100.0 / total))) if total else 0
        now = time.monotonic()
        last_ts = _state.get("last_ts", 0.0)
        if done != total:
            if _state.get("last_pct") == pct and now - last_ts < 30.0:
                return
            if now - last_ts < 2.0:
                return
        first = not _state.get("posted")
        _state["posted"] = True
        _state["last_pct"] = pct
        _state["last_ts"] = now
    desc = "{}/{} · {}".format(done, total, package)
    if not first:
        _post("canceled", pct, desc)     # free the slot; failure is harmless
    _post("running", pct, desc)


def finish(success=True):
    """Post a terminal status. Normally called from the CI after_script."""
    with _lock:
        total = _state["total"] or _state["done"]
    _post("success" if success else "failed", 100, "{0}/{0} done".format(total))
