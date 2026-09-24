# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the security-proxy sign route is, and the gate token to call it with.

Two sources:

- ``BITS_SIGN_PROXY_AGENT_SOCKET`` (preferred): ask the proxy's agent socket, the
  same one ``security-proxy-token`` reads. The proxy binds a random port on every
  start and rotates its gate-token master secret (``secret_rotation_seconds``), so
  a copied URL/token goes stale within a day or on any restart. The answer is
  cached briefly; a failed call re-asks once and retries if the endpoint changed.
- ``BITS_SIGN_PROXY_URL`` + ``BITS_SIGN_PROXY_TOKEN`` (static; dev only): used
  instead of the agent socket when that setting is empty, never as a fallback for
  an agent failure.
"""

import json
import os
import socket
import time

CACHE_SECONDS = 300
_AGENT_TIMEOUT = 5
_cache = {}          # (socket, route, host, prefix) -> (expires_monotonic, url, token)
_now = time.monotonic  # patchable clock (tests)


class Unavailable(Exception):
    """Signing is not configured here (maps to HTTP 503)."""


def check(settings) -> None:
    """Raise :class:`Unavailable` unless a sign proxy is configured and usable
    without contacting it (the static token must be in the environment)."""
    if settings.sign_proxy_agent_socket:
        return
    if not settings.sign_proxy_url:
        raise Unavailable("signing proxy is not configured")
    if not os.environ.get(settings.sign_proxy_token_env):
        raise Unavailable("signing proxy token is not available")


def _ask_agent(path, route) -> dict:
    """``{"port": ..., "token": ...}`` for *route* from the agent socket at *path*."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_AGENT_TIMEOUT)
            s.connect(path)
            s.sendall(route.encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf)
    except (OSError, ValueError) as exc:
        raise RuntimeError("sign proxy agent socket %s: %s" % (path, exc))
    if not isinstance(resp, dict) or not resp.get("token") or not resp.get("port"):
        err = resp.get("error") if isinstance(resp, dict) else None
        raise RuntimeError("sign proxy agent socket %s: no token for route %r%s"
                           % (path, route, " (%s)" % err if err else ""))
    try:
        resp["port"] = int(resp["port"])
    except (TypeError, ValueError):
        raise RuntimeError("sign proxy agent socket %s: bad port %r" % (path, resp["port"]))
    return resp


def endpoint(settings, refresh=False):
    """Return ``(url, token)`` for the sign route. Raises :class:`Unavailable`
    when not configured, RuntimeError when the agent socket cannot answer."""
    check(settings)
    path = settings.sign_proxy_agent_socket
    if not path:
        return settings.sign_proxy_url, os.environ[settings.sign_proxy_token_env]
    key = (path, settings.sign_proxy_route, settings.sign_proxy_host,
           settings.sign_proxy_prefix)
    now = _now()
    hit = _cache.get(key)
    if hit and not refresh and hit[0] > now:
        return hit[1], hit[2]
    info = _ask_agent(path, settings.sign_proxy_route)
    url = "http://%s:%d%s" % (settings.sign_proxy_host, info["port"],
                              settings.sign_proxy_prefix)
    _cache[key] = (now + CACHE_SECONDS, url, info["token"])
    return url, info["token"]


def call(settings, fn, *args):
    """``fn(*args, url, token)`` against the sign route. With the agent socket, a
    failure (RuntimeError from trust's proxy client, or a stray OSError such as a
    read timeout) re-reads the endpoint once and retries only if it changed (new
    port or rotated token). Both proxy calls are safe to repeat: /pubkey is a
    read and Ed25519 signing is deterministic."""
    url, token = endpoint(settings)
    try:
        return fn(*args, url, token)
    except (RuntimeError, OSError):
        if not settings.sign_proxy_agent_socket:
            raise
        fresh = endpoint(settings, refresh=True)
        if fresh == (url, token):
            raise
        return fn(*args, *fresh)
