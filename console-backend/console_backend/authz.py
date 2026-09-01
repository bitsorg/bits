# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Community-admin authorization, reusing bits_helpers.forge's policy model.

The policy is bits' ADMINS format (``* @root`` overall, ``lcg @alice`` per-group,
``& <gitlab-group>`` resolved via the API). bits-admins are represented as an
overall entry (``*``), typically a ``&<bits-admin-group>`` ref, so this is the
single source of truth — no separate role lookup.
"""

import time

from bits_helpers import forge

_CACHE = {}          # policy source -> (resolved_policy, expiry); service-token only
_CACHE_TTL = 60


def load_policy(settings) -> dict:
    src = settings.admin_policy_source
    return forge.load_admin_policy(src) if src else {}


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
    """Cached load+resolve used per request. Only the shared service-token
    resolution is cached (a short TTL, to spare the file read + GitLab paging);
    per-user resolutions are never shared between users."""
    if settings.admin_resolve_token:
        key = settings.admin_policy_source
        hit = _CACHE.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        resolved = resolve_policy(load_policy(settings), settings, user_token)
        _CACHE[key] = (resolved, time.time() + _CACHE_TTL)
        return resolved
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
