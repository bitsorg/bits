# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the backend and the worker get their credentials (M6).

With BITS_CRED_PROXY_AGENT_SOCKET set, they hold none: every call to GitLab or
the store goes through the credentials proxy (a second security-proxy instance,
bits-services `credentials-proxy`), which adds the real token, or re-signs the
S3 request, on the way out. All a service holds is the proxy's gate token for a
route, read from the proxy's agent socket; it rotates daily and is useless
anywhere but at the proxy. The real secrets are pushed into the proxy's memory
by `make unlock`.

Routes, by what they are for (their names in the proxy's config):

    gitlab-ops     pipeline actions, CI variables, pipeline variables
    gitlab-read    pipelines, jobs, logs, artifacts, repository archives
    gitlab-groups  admin group membership (the admin policy)
    s3             the store

A GitLab route is matched by its PRIVATE-TOKEN header, so a client keeps
sending `PRIVATE-TOKEN: <token>` exactly as it does to GitLab: only the URL (the
proxy's) and the token (the gate token) change.

Without the socket, each falls back to its environment variable as before, so
the move is one credential at a time.
"""

import os
import threading
import time
from urllib.parse import urlsplit

from . import signproxy

# Short: asking the agent socket is a local round trip, and a restarted proxy
# (new port, new gate tokens) is then followed within seconds.
CACHE_SECONDS = 5
ROUTES = {"ops": "gitlab-ops", "read": "gitlab-read", "groups": "gitlab-groups",
          "mirror": "gitlab-mirror"}

_cache = {}
_lock = threading.Lock()


class Unavailable(OSError):
    """The credentials proxy cannot be reached. An OSError, so callers treat it
    like a network failure and try again."""


class NoRoute(Unavailable):
    """The proxy's config has no such route: that credential is not provided."""


def proxied(settings, kind=None):
    if kind == "mirror":
        return bool(getattr(settings, "store_writer_agent_socket", ""))
    return bool(getattr(settings, "cred_proxy_agent_socket", ""))


def _instance(settings, kind):
    """(socket, host) of the proxy holding *kind*: the mirror's token is in the
    store writer, which only the worker reaches (a backend that could push to
    bits-manifests could stage BOMs for the way back)."""
    if kind == "mirror":
        return settings.store_writer_agent_socket, settings.store_writer_host
    return None, None


def endpoint(settings, route, refresh=False, socket=None, host=None):
    """``(base_url, gate_token)`` of a route of the credentials proxy (or of
    another instance: *socket* and *host*, e.g. the store writer)."""
    path = socket or settings.cred_proxy_agent_socket
    host = host or settings.cred_proxy_host
    key = (path, route, host)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and hit[0] > now:
        return hit[1], hit[2]
    if not os.path.exists(path):
        raise Unavailable("credentials proxy agent socket %s is not there" % path)
    try:
        info = signproxy._ask_agent(path, route)
    except RuntimeError as e:
        msg = str(e).replace("sign proxy", "credentials proxy")
        raise (NoRoute if "unknown service" in msg else Unavailable)(msg)
    base = "http://%s:%d" % (host, info["port"])
    with _lock:
        _cache[key] = (now + CACHE_SECONDS, base, info["token"])
    return base, info["token"]


def _env_token(settings, kind):
    if kind == "ops":
        return settings.forge_ops_token
    if kind == "read":
        return settings.forge_read_token or settings.forge_ops_token
    if kind == "groups":
        return settings.admin_resolve_token
    if kind == "mirror":
        return settings.forge_mirror_token
    raise ValueError("unknown GitLab credential %r" % kind)


def gitlab(settings, kind):
    """``(api_url, token)`` for GitLab calls of *kind* (ops, read, groups):
    through the proxy when it is configured, else straight to GitLab with the
    environment's token. ('', '') when neither is configured."""
    if proxied(settings, kind):
        socket, host = _instance(settings, kind)
        base, token = endpoint(settings, ROUTES[kind], socket=socket, host=host)
        path = urlsplit(settings.gitlab_api_url or "").path.rstrip("/") or "/api/v4"
        return base + path, token
    token = _env_token(settings, kind)
    return (settings.gitlab_api_url, token) if token else ("", "")


def segment(settings, value):
    """A path segment for a client that %-encodes it itself (bits_helpers'
    forge functions, which quote a group path's / as %2F): through the proxy,
    encoded once more beforehand, since the proxy decodes the path once before
    it forwards it. Unchanged without the proxy."""
    if not proxied(settings):
        return value
    from urllib.parse import quote
    return quote(str(value), safe="")


def has(settings, kind):
    """Whether a credential of *kind* is configured. Through the proxy: whether
    its config has the route (a proxy that cannot be reached counts as having
    it, so its calls fail as an outage, not as "not configured")."""
    if not proxied(settings, kind):
        return bool(_env_token(settings, kind))
    try:
        socket, host = _instance(settings, kind)
        endpoint(settings, ROUTES[kind], socket=socket, host=host)
    except NoRoute:
        return False
    except Unavailable:
        pass
    return True


def s3_writer(settings):
    """``(client, new)``: a boto3 S3 client through the store writer's s3 route
    (BITS_STORE_WRITER_AGENT_SOCKET), the worker's only way to write the store."""
    if not settings.store_writer_agent_socket:
        raise Unavailable("BITS_STORE_WRITER_AGENT_SOCKET is not set")
    base, token = endpoint(settings, "s3", socket=settings.store_writer_agent_socket,
                           host=settings.store_writer_host)
    return _s3(settings, "writer", base, token)


def s3_client(settings):
    """``(client, new)``: a boto3 S3 client through the proxy's s3 route. The
    gate token is its access key, the proxy re-signs each request with the real
    keys; path-style addressing, since the proxy picks the keypair by the
    bucket in the path. A new client (*new* True) whenever the proxy's port or
    gate token changes. The backend can reach this route too: give the store a
    keypair that can only read, until a signing step needs more."""
    base, token = endpoint(settings, "s3")
    return _s3(settings, "s3", base, token)


def _s3(settings, name, base, token):
    key = (base, token)
    with _lock:
        hit = _clients.get(name)
        if hit is not None and hit[0] == key:
            return hit[1], False
        import boto3
        from botocore.config import Config
        # Its own session: the default one is not safe to share between threads.
        client = boto3.session.Session().client(
            "s3", endpoint_url=base, aws_access_key_id=token,
            aws_secret_access_key="the-proxy-signs", region_name=settings.orch_s3_region,
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4",
                          retries={"max_attempts": 3},
                          # As bits' own S3 client (sync.py): no checksum headers
                          # the store does not ask for.
                          request_checksum_calculation="WHEN_REQUIRED",
                          response_checksum_validation="WHEN_REQUIRED"))
        _clients[name] = (key, client)
        return client, True


_clients = {}
