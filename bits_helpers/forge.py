# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forge abstraction for the manifests-repo certification gate.

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

from bits_helpers.log import warning
from bits_helpers import site


def load_admin_policy(source) -> dict:
    """Parse an overall/per-group admin policy from a file path or text.

    Aligns with bits-console's role model (``bits_admins`` = overall/override,
    per-community ``admins`` = group). Line forms:

        @alice                  # overall admin (bare/@handle lines, legacy flat list)
        * @carol                # overall admin (explicit '*' group)
        * &cern-bits-admins     # overall = live members of a GitLab group (resolved via API)
        lcg @dave &alice-egroup # group 'lcg' admins: a user + a GitLab group's members
        common @frank           # group 'common' admins

    Member tokens: ``@user`` / bare word = a literal username (a manual override,
    resolved offline); ``&group`` = a GitLab group path/ID whose members are
    resolved at certify time (see :func:`resolve_admin_policy`). Returns
    ``{"*": {tokens…}, "lcg": {…}, …}`` — usernames lowercased, group refs kept
    as ``"&<ref>"``. A legacy flat file (only ``@handle`` lines) is all-overall.
    """
    if isinstance(source, str) and os.path.isfile(source):
        with open(source) as fh:
            text = fh.read()
    else:
        text = source or ""
    policy = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        # A leading bare word (not @/&) is the group label; else the line is overall.
        if not tokens[0].startswith(("@", "&")):
            group = "*" if tokens[0] == "*" else tokens[0].lower()
            members = tokens[1:]
        else:
            group, members = "*", tokens
        bucket = policy.setdefault(group, set())
        for m in members:
            if m.startswith("&"):
                bucket.add("&" + m[1:])                 # GitLab group ref, resolved later
            else:
                bucket.add(m[1:].lower() if m.startswith("@") else m.lower())
    return policy


def admin_policy_grouprefs(policy) -> set:
    """Return the set of GitLab group refs (without the ``&``) used by *policy*."""
    refs = set()
    for members in (policy or {}).values():
        refs |= {m[1:] for m in members if isinstance(m, str) and m.startswith("&")}
    return refs


def resolve_admin_policy(policy, resolver) -> dict:
    """Expand ``&group`` refs to member usernames via *resolver(ref) -> set|None*.

    Literal usernames are kept as-is (manual override). A ref that fails to
    resolve (``resolver`` returns None) is skipped with a warning rather than
    aborting, so explicit admins keep working through a transient API hiccup.
    """
    cache, out = {}, {}
    for group, members in (policy or {}).items():
        users = set()
        for m in members:
            if isinstance(m, str) and m.startswith("&"):
                ref = m[1:]
                if ref not in cache:
                    cache[ref] = resolver(ref)
                got = cache[ref]
                if got is None:
                    warning("certify: could not resolve GitLab group '%s'; relying "
                            "on explicitly listed admins", ref)
                else:
                    users |= {u.lower() for u in got}
            else:
                users.add(m)
        out[group] = users
    return out


def approved_for_group(approvers, policy, group) -> bool:
    """True if an approver is an overall admin or an admin of *group*.

    Assumes *policy* has been resolved to usernames (see resolve_admin_policy);
    any unresolved ``&group`` token simply won't match an approver username.
    """
    a = {str(x).lower() for x in (approvers or [])}
    grp = str(group).lower() if group else "common"
    return bool(a & (policy.get("*", set()) | policy.get(grp, set())))


def verify_group_approval(forge, policy, groups):
    """Return ``(ok, approvers, unmet)`` for the required *groups*.

    Every group must be approved by one of its admins or an overall admin;
    *unmet* lists the groups that were not. Overall-admin approval covers all.
    """
    approvers = forge.list_approvers()
    unmet = sorted(g for g in groups if not approved_for_group(approvers, policy, g))
    return (not unmet), approvers, unmet


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
                 timeout=15, branch=None):
        self.api_url = api_url.rstrip("/")
        self.project_id = str(project_id)
        self.mr_iid = str(mr_iid) if mr_iid else None
        self.commit_sha = commit_sha
        self.branch = branch
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None):
        """Build from GitLab CI variables, or return None if it can't identify a commit.

        The MR is the merged one of the commit (``CI_COMMIT_SHA``): the post-merge
        branch pipeline the signing job runs in. An open MR's approvals never count.
        """
        env = env if env is not None else os.environ
        api = env.get("CI_API_V4_URL")
        pid = env.get("CI_PROJECT_ID")
        sha = env.get("CI_COMMIT_SHA")
        token = (env.get("BITS_FORGE_TOKEN") or env.get("GITLAB_TOKEN")
                 or env.get("CI_JOB_TOKEN"))
        if not (api and pid and token and sha):
            return None
        return cls(api, pid, None, token, commit_sha=sha, branch=env.get("CI_COMMIT_BRANCH"))

    def _get(self, path):
        import requests
        resp = requests.get("%s/projects/%s/%s" % (self.api_url, self.project_id, path),
                            headers={"PRIVATE-TOKEN": self.token}, timeout=self.timeout)
        _gl_ok(resp)
        return resp.json()

    def _resolve_iid(self):
        """Return the iid of a merged MR: the given one, else the commit's."""
        if self.mr_iid:   # only once merged: approvals of an open MR do not count
            mr = self._get("merge_requests/%s" % self.mr_iid) or {}
            return self.mr_iid if mr.get("state") == "merged" else None
        if not self.commit_sha:
            return None
        mrs = self._get("repository/commits/%s/merge_requests" % self.commit_sha) or []
        # Only the MR that merged this commit (its merge, squash or head commit)
        # into this branch: not an open one, nor an older one that contains it.
        merged = [m for m in mrs if (m or {}).get("state") == "merged"
                  and (not self.branch or m.get("target_branch") == self.branch)
                  and self.commit_sha in (m.get("merge_commit_sha"),
                                          m.get("squash_commit_sha"), m.get("sha"))]
        if merged:
            self.mr_iid = str(merged[0].get("iid"))
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


def gitlab_identify(api_url, token, timeout=15):
    """Resolve the GitLab username that owns *token* via ``GET /user``.

    The token *is* the identity: only its owner can present it, so the returned
    username is an authenticated, unforgeable identity for whoever initiated the
    certification (the model bits-console already uses). Returns the username, or
    None if the token is invalid/unusable.
    """
    import requests
    try:
        resp = requests.get("%s/user" % api_url.rstrip("/"),
                            headers={"PRIVATE-TOKEN": token}, timeout=timeout)
        _gl_ok(resp)
        return (resp.json() or {}).get("username")
    except Exception:
        return None


DEFAULT_GITLAB_TOKEN_FILE = site.get("paths.gitlab_token_file") or "~/.bits/gitlab-token"


def resolve_gitlab_token(explicit=None):
    """Find a GitLab PAT: explicit arg > env > ``~/.bits/gitlab-token``.

    The file holds the token on a line (or ``key = glpat-…``); ``#`` comments are
    ignored. Warns (does not fail) if it is group/other-readable, since it is a
    secret. Returns the token string or None. This is the client-side identity:
    the PAT owner is who ``bits`` acts as when triggering certification.
    """
    if explicit:
        return explicit
    for var in ("BITS_CERTIFIER_TOKEN", "BITS_GITLAB_TOKEN", "GITLAB_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v
    path = os.path.expanduser(os.environ.get("BITS_GITLAB_TOKEN_FILE")
                              or DEFAULT_GITLAB_TOKEN_FILE)
    if not os.path.isfile(path):
        return None
    import stat
    try:
        if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            warning("%s is group/other-readable but holds a GitLab token; run "
                    "`chmod 600 %s`", path, path)
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                return line.split("=", 1)[1].strip().strip('"\'') if "=" in line else line
    except OSError:
        return None
    return None


def gitlab_create_pipeline(api_url, token, project, ref="main", variables=None,
                           timeout=30) -> dict:
    """Create a pipeline on *ref* as the PAT owner. Returns ``{id, web_url}``.

    GitLab records the PAT owner as the pipeline's user, so CI sees them in
    ``GITLAB_USER_LOGIN`` — that is the authenticated certifier identity.
    """
    import requests
    from urllib.parse import quote
    payload = {"ref": ref}
    if variables:
        payload["variables"] = [{"key": k, "value": str(v)} for k, v in variables.items()]
    resp = requests.post(
        "%s/projects/%s/pipeline" % (api_url.rstrip("/"), quote(str(project), safe="")),
        headers={"PRIVATE-TOKEN": token}, json=payload, timeout=timeout)
    _gl_ok(resp)
    data = resp.json() or {}
    return {"id": data.get("id"), "web_url": data.get("web_url")}


def _gl_ok(resp):
    """raise_for_status, but include GitLab's response body (it explains 400s)."""
    if not getattr(resp, "ok", True):
        body = ""
        try:
            body = resp.text[:500]
        except Exception:
            pass
        raise RuntimeError("GitLab %s %s: %s"
                           % (resp.status_code, getattr(resp, "reason", ""), body))
    return resp


# The least GitLab access level (Developer) at which a group member counts.
MEMBER_ACCESS_LEVEL = 30


def gitlab_group_members(api_url, token, group_ref, timeout=15, inherited=True) -> set:
    """Return the set of usernames of the active, unexpired members of a GitLab group
    at Developer level or above (a Guest, Reporter or blocked account does not count).

    *group_ref* is a group path (``cern/bits-admins``) or numeric id. Paginates
    ``GET /groups/<ref>/members/all`` (inherited members included) by default, or
    ``/members`` (DIRECT members only) when ``inherited=False`` — used so a
    subgroup resolves to only its own members and a root group does not absorb a
    parent's. Raises on API/auth failure so the caller can decide.
    """
    import requests
    from urllib.parse import quote
    base = "%s/groups/%s/%s" % (api_url.rstrip("/"),
                                quote(str(group_ref), safe=""),
                                "members/all" if inherited else "members")
    import time
    today = time.strftime("%Y-%m-%d", time.gmtime())
    members, listed, page = set(), 0, 1
    while True:
        resp = requests.get(base, headers={"PRIVATE-TOKEN": token},
                            params={"per_page": 100, "page": page}, timeout=timeout)
        _gl_ok(resp)
        data = resp.json() or []
        for m in data:
            m = m or {}
            u = m.get("username")
            listed += bool(u)
            if u and m.get("state") == "active" \
               and m.get("membership_state", "active") in (None, "active") \
               and (m.get("access_level") or 0) >= MEMBER_ACCESS_LEVEL \
               and not (m.get("expires_at") and str(m["expires_at"])[:10] <= today):
                members.add(u.lower())
        if len(data) < 100:
            if listed and not members:
                warning("GitLab group %s: none of its %d member(s) counts (active, unexpired, "
                        "Developer or above): it counts nobody", group_ref, listed)
            return members
        page += 1


def gitlab_subgroups(api_url, token, group_ref, timeout=15) -> list:
    """Return ``[{"id", "path"}]`` for the DIRECT subgroups of a group. Paginates
    ``GET /groups/<ref>/subgroups``. Used to derive per-community admin groups by
    convention: each subgroup's ``path`` is a community name, its members are that
    community's admins. ``all_available``: without it GitLab lists only subgroups
    the token itself belongs to, so a token from another group saw none.
    Raises on API/auth failure (caller decides)."""
    import requests
    from urllib.parse import quote
    base = "%s/groups/%s/subgroups" % (api_url.rstrip("/"),
                                       quote(str(group_ref), safe=""))
    out, page = [], 1
    while True:
        resp = requests.get(base, headers={"PRIVATE-TOKEN": token},
                            params={"per_page": 100, "page": page, "all_available": "true"},
                            timeout=timeout)
        _gl_ok(resp)
        data = resp.json() or []
        for g in data:
            if isinstance(g, dict) and g.get("id"):
                out.append({"id": g["id"], "path": g.get("path", "")})
        if len(data) < 100:
            return out
        page += 1


def make_group_resolver(api_url, token):
    """A ``resolver(group_ref) -> set|None`` over the GitLab API (None on error)."""
    def _resolver(ref):
        try:
            return gitlab_group_members(api_url, token, ref)
        except Exception:
            return None
    return _resolver


def forge_from_env(env=None):
    """Return the forge for the current CI environment, or None if none applies."""
    return GitLabForge.from_env(env)


