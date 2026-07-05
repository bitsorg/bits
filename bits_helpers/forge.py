# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forge abstraction for the manifests-repo certification gate (ADR-0004 §2, P4).

Certification is a merge-request approval by a group admin: the forge (GitLab
first, GitHub later) is the identity + approval authority. This module keeps the
forge behind a tiny interface so the certification gate is forge-agnostic and
unit-testable, and the GitLab specifics live in one place.

The approval check is deliberately defence-in-depth: GitLab's own CODEOWNERS +
required-approval rules already enforce group-scoped approval before an MR can
merge; this re-verifies, on the protected branch, that a listed group admin
actually approved before anything is signed.
"""

import os
import re


def load_admins(source) -> set:
    """Parse group-admin usernames from a file path or text.

    One username per line; ``#`` comments and blank lines ignored; a leading
    ``@`` and surrounding whitespace are stripped. Case is normalised to lower.
    Also tolerates CODEOWNERS-style lines (``/path @a @b``) by taking every
    ``@handle`` on the line.
    """
    if isinstance(source, str) and os.path.isfile(source):
        with open(source) as fh:
            text = fh.read()
    else:
        text = source or ""
    admins = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        handles = re.findall(r"@([A-Za-z0-9._-]+)", line)
        if handles:
            admins.update(h.lower() for h in handles)
        else:
            admins.add(line.lower())
    return admins


def approved_by(approvers, admins) -> bool:
    """True if at least one approver is a listed group admin (case-insensitive)."""
    a = {str(x).lower() for x in (approvers or [])}
    return bool(a & {str(x).lower() for x in (admins or [])})


class Forge:
    """Minimal forge interface: who approved the change under certification."""

    def list_approvers(self):
        """Return the set of usernames that approved the merge request."""
        raise NotImplementedError

    def context(self) -> str:
        """Human-readable identifier of what's being certified (for logs)."""
        return "unknown"


class StaticForge(Forge):
    """A forge whose approvers are supplied directly (tests, offline gates)."""

    def __init__(self, approvers, ctx="static"):
        self._approvers = set(approvers or [])
        self._ctx = ctx

    def list_approvers(self):
        return set(self._approvers)

    def context(self):
        return self._ctx


class GitLabForge(Forge):
    """Reads merge-request approvals from the GitLab API.

    Needs a token with ``api`` read scope (a group-admin/bot PAT); the CI
    ``JOB-TOKEN`` cannot read approvals. Reads nothing else about the MR.
    """

    def __init__(self, api_url, project_id, mr_iid, token, commit_sha=None,
                 timeout=15):
        self.api_url = api_url.rstrip("/")
        self.project_id = str(project_id)
        self.mr_iid = str(mr_iid) if mr_iid else None
        self.commit_sha = commit_sha
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None):
        """Build from GitLab CI variables, or return None if it can't identify an MR.

        Works both in a merge-request pipeline (``CI_MERGE_REQUEST_IID``) and in
        the post-merge branch pipeline, where the MR is resolved from the merged
        commit (``CI_COMMIT_SHA``) — the realistic place the signing job runs.
        """
        env = env if env is not None else os.environ
        api = env.get("CI_API_V4_URL")
        pid = env.get("CI_PROJECT_ID")
        iid = env.get("CI_MERGE_REQUEST_IID") or env.get("CI_OPEN_MERGE_REQUESTS")
        sha = env.get("CI_COMMIT_SHA")
        token = (env.get("BITS_FORGE_TOKEN") or env.get("GITLAB_TOKEN")
                 or env.get("CI_JOB_TOKEN"))
        if not (api and pid and token and (iid or sha)):
            return None
        # CI_OPEN_MERGE_REQUESTS is "group/proj!iid,…"; take the first iid.
        if iid and "!" in str(iid):
            iid = str(iid).split(",")[0].split("!")[-1]
        return cls(api, pid, iid, token, commit_sha=sha)

    def _get(self, path):
        import requests
        resp = requests.get("%s/projects/%s/%s" % (self.api_url, self.project_id, path),
                            headers={"PRIVATE-TOKEN": self.token}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _resolve_iid(self):
        """Return the MR iid, resolving it from the merged commit if needed."""
        if self.mr_iid:
            return self.mr_iid
        if not self.commit_sha:
            return None
        mrs = self._get("repository/commits/%s/merge_requests" % self.commit_sha) or []
        merged = [m for m in mrs if (m or {}).get("state") == "merged"]
        chosen = (merged or mrs)
        if chosen:
            self.mr_iid = str(chosen[0].get("iid"))
        return self.mr_iid

    def list_approvers(self):
        iid = self._resolve_iid()
        if not iid:
            return set()
        data = self._get("merge_requests/%s/approvals" % iid)
        approvers = set()
        for a in data.get("approved_by", []) or []:
            user = (a or {}).get("user") or {}
            name = user.get("username")
            if name:
                approvers.add(name)
        return approvers

    def context(self):
        if self.mr_iid:
            return "%s MR !%s" % (self.project_id, self.mr_iid)
        return "%s commit %s" % (self.project_id, (self.commit_sha or "?")[:12])


def forge_from_env(env=None):
    """Return the forge for the current CI environment, or None if none applies."""
    return GitLabForge.from_env(env)


def verify_approval(forge, admins):
    """Return ``(ok, approvers)`` — ok iff a listed admin approved via *forge*."""
    approvers = forge.list_approvers()
    return approved_by(approvers, admins), approvers
