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
    "warned":  False,  # True once we've surfaced a post failure (avoid spam)
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
    req = urllib.request.Request(
        _state["url"], data=json.dumps(body).encode(),
        headers=_state["headers"], method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:                       # never break a build over this
        code = getattr(exc, "code", None)          # urllib.error.HTTPError -> HTTP status
        # Surface the first failure at a visible level (even without --debug) so the
        # missing build-progress bar has an explanation in the log, then stop
        # retrying on permission errors to avoid one failed POST per package.
        if not _state["warned"]:
            _state["warned"] = True
            used_status_token = bool(os.environ.get("BITS_STATUS_TOKEN"))
            target = "project %s, commit %s, ref %s" % (
                os.environ.get("CI_PROJECT_ID", "?"),
                (os.environ.get("CI_COMMIT_SHA", "") or "?")[:12],
                _state.get("ref") or os.environ.get("CI_COMMIT_REF_NAME") or "?")
            if code in (401, 403) and not used_status_token:
                # We fell back to the CI job token, which cannot post commit
                # statuses. The fix is to set BITS_STATUS_TOKEN, not to change roles.
                hint = (" -- no BITS_STATUS_TOKEN in this job, so the CI job token was used "
                        "and rejected (job tokens cannot post commit statuses). Set "
                        "BITS_STATUS_TOKEN (api scope, >= Developer) and make sure it is "
                        "exposed to this pipeline's ref")
            elif code in (401, 403):
                # The token authenticated (401 would mean invalid) but is forbidden
                # for THIS target. Scope is fine; the role/ref is the issue.
                hint = ((" -- BITS_STATUS_TOKEN was accepted but forbidden for %s. "
                         "'api' scope is not enough on its own: its user needs >= Developer "
                         "on THIS project, and posting a status on a PROTECTED branch can "
                         "require Maintainer when push/merge is restricted. Check the role on "
                         "this exact project + ref (403 = permission, not scope)") % target)
            elif not os.environ.get("BITS_STATUS_TOKEN"):
                hint = " -- BITS_STATUS_TOKEN is not set (the CI job token cannot post commit statuses)"
            else:
                hint = ""
            warning("progress: build-progress reporting disabled (commit-status POST "
                    "to %s failed: %s%s)", target, ("HTTP %s" % code) if code else exc, hint)
        if code in (401, 403):
            _state["ready"] = False                # permanent for this run; stop trying
        else:
            debug("progress: commit-status post failed: %s", exc)


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
