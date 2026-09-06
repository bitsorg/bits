# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Community-admin authorization, reusing bits_helpers.forge's policy model.

The policy is bits' ADMINS format (``* @root`` overall, ``lcg @alice`` per-group,
``& <gitlab-group>`` resolved via the API). bits-admins are represented as an
overall entry (``*``), typically a ``&<bits-admin-group>`` ref, so this is the
single source of truth — no separate role lookup.
"""

import re
import time

from bits_helpers import forge

# GitLab group/project access-token bots (e.g. group_355494_bot_<hash>). These are
# service accounts, never interactive users — so a resolve-token bot that is a
# member of the admin group must NOT be counted as an admin.
_BOT_RE = re.compile(r"^(group|project)_\d+_bot_")


def _humans(users):
    return {u for u in (users or set()) if not _BOT_RE.match(u)}

_CACHE = {}          # key -> (resolved_policy, expiry); service-token path only
_STALE = {}          # key -> (last good resolved_policy, expiry); served if a refresh fails
_CACHE_TTL = 60
# How long a last-good policy may be served while GitLab is unreachable. Long
# enough to ride out a blip, bounded so a revoked admin can't keep access through
# a prolonged outage — past this the backend fails closed (deny).
_STALE_TTL = 900


def load_policy(settings) -> dict:
    src = settings.admin_policy_source
    return forge.load_admin_policy(src) if src else {}


def _merge(*policies) -> dict:
    """Union the member sets of several {group: {users}} policies."""
    out = {}
    for pol in policies:
        for group, members in (pol or {}).items():
            out.setdefault(group, set()).update(members)
    return out


def tree_policy(settings) -> dict:
    """Derive the policy from the bits-admins GROUP TREE by convention: the root
    group's DIRECT members are overall admins ('*'); each direct subgroup is a
    community (its path, lowercased) whose DIRECT members are that community's
    admins. Direct (not inherited) on both, so the root can't absorb a parent
    group's members, and overall coverage comes from the '*' bucket rather than
    inheritance. A subgroup named like a reserved policy group (e.g. 'common')
    simply populates that group — legitimate in the ADMINS format.
    Empty unless both admins_group and a resolve token are set. Raises on API
    failure so the caller can serve a stale policy rather than lock everyone out."""
    root, token = settings.admins_group, settings.admin_resolve_token
    if not (root and token):
        return {}
    api = settings.gitlab_api_url
    pol = {"*": _humans(forge.gitlab_group_members(api, token, root, inherited=False))}
    for sub in forge.gitlab_subgroups(api, token, root):
        name = str(sub.get("path", "")).strip().lower()
        if name:
            pol[name] = _humans(forge.gitlab_group_members(api, token, sub["id"], inherited=False))
    return pol


def resolve_policy(policy, settings, user_token) -> dict:
    """Expand any ``&group`` refs to usernames. Prefers a dedicated read-only
    service token (deterministic, not observer-dependent) and falls back to the
    caller's token. A pure username list needs no API call."""
    if not policy:
        return {}
    if not forge.admin_policy_grouprefs(policy):
        return policy   # already literal usernames — no token/network
    token = settings.admin_resolve_token or user_token
    resolver = forge.make_group_resolver(settings.gitlab_api_url, token)
    return forge.resolve_admin_policy(policy, resolver)


def resolved_admin_policy(settings, user_token) -> dict:
    """Cached load+resolve used per request. With a service (resolve) token the
    result is deterministic and shared across users, so it is cached and, on a
    later GitLab failure, the last good policy is served stale rather than locking
    everyone out. Without one, per-user resolution runs uncached (dev/no-token)."""
    # Caching + the group tree BOTH require the service token: only then is the
    # result deterministic and shareable across users. Gating on admins_group too
    # would cache a per-user (&ref-via-caller-token) resolution and leak it — and
    # the tree contributes nothing without the token anyway.
    if settings.admin_resolve_token:
        key = (settings.admin_policy_source, settings.admins_group)
        hit = _CACHE.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        try:
            # Explicit policy (supplement/override) merged with the group tree.
            merged = _merge(resolve_policy(load_policy(settings), settings, user_token),
                            tree_policy(settings))
        except Exception:
            st = _STALE.get(key)   # GitLab blip mid-refresh — serve the last good set
            if st and st[1] > time.time():
                return st[0]
            raise                  # cold start, or stale too old: fail closed (deny)
        _CACHE[key] = (merged, time.time() + _CACHE_TTL)
        _STALE[key] = (merged, time.time() + _STALE_TTL)
        return merged
    return resolve_policy(load_policy(settings), settings, user_token)


def is_admin_for(user, group, resolved_policy) -> bool:
    """True if *user* is an overall admin or an admin of *group*."""
    return forge.approved_for_group([user], resolved_policy, group)


def admin_groups(user, resolved_policy):
    """Return ``(overall_admin, [groups])`` the *user* administers. Overall admin
    (``*``) implicitly covers every group."""
    u = str(user).lower()
    overall = u in {str(m).lower() for m in resolved_policy.get("*", set())}
    groups = sorted(g for g, members in resolved_policy.items()
                    if g != "*" and u in {str(m).lower() for m in members})
    return overall, groups
