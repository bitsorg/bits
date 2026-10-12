# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Who may act for a community, for the API (at request and approval time) and
the worker (again, when it runs an action). Same admin policy as the rest of the
backend: an admin of the community, or an overall admin; an action with no
community is for overall admins only."""

from .. import authz

try:
    from bits_helpers import forge as _forge
except Exception:  # pragma: no cover - bits_helpers is on the path in every image
    _forge = None


def person_allowed(settings, user, community, token=None):
    resolved = authz.resolved_admin_policy(settings, token)
    if community:
        return authz.is_admin_for(user, community, resolved)
    overall, _ = authz.admin_groups(user, resolved)
    return overall


def unresolvable(settings):
    """Why the worker could not check authority, or "". It has no user token, so
    an admin policy naming GitLab groups (&group lines, or BITS_ADMINS_GROUP)
    needs BITS_ADMIN_RESOLVE_TOKEN; without it every action would fail at run
    time as "no longer authorized"."""
    from .. import credproxy
    if credproxy.has(settings, "groups"):
        return ""
    if settings.admins_group:
        return ("BITS_ADMINS_GROUP is set but neither BITS_ADMIN_RESOLVE_TOKEN nor"
                " BITS_CRED_PROXY_AGENT_SOCKET is")
    if _forge is not None and _forge.admin_policy_grouprefs(authz.load_policy(settings)):
        return ("the admin policy names GitLab groups but neither BITS_ADMIN_RESOLVE_TOKEN"
                " nor BITS_CRED_PROXY_AGENT_SOCKET is set")
    return ""


def checker(settings):
    """authorize(actor, community) for the worker, which holds no user token: the
    policy is resolved with BITS_ADMIN_RESOLVE_TOKEN. Only a person's authority is
    checked against the policy. The worker's own actions are decided by its code.
    Schedules and CI get their approval rules (standing approvals, CI identity) in
    later milestones; until then they are not authorized."""
    def authorize(actor, community):
        if actor.kind == "person":
            return person_allowed(settings, actor.id, community)
        return actor.kind == "worker"
    return authorize
