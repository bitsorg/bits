# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether bits-orchestrator owns a store's certified set, and asking it for changes.

When the orchestrator takes certification over, it puts ``MANIFESTS/orchestrator.json``
in the store (``{"issuer", "manifests", "since"}``). From then on it alone writes and
signs the manifests, and the certified set changes only through it:

* ``bits certify`` uploads the build and asks for its pre-approval, and stops there:
  the orchestrator certifies the build from that pre-approval (no merge request);
* a build leaves the set by a ``decertify`` action requested from bits-console
  (``bits store rm --stale-boms --manifests-dir``, ``bits compliance --enforce``);
* nothing in bits signs or writes the manifests (``bits sign`` refuses).

The marker is not signed. It can only make bits do less (open no merge request,
sign nothing), and only its presence is used: the console a token is sent to never
comes from it.
"""

import json
import os
import time

MARKER = "MANIFESTS/orchestrator.json"


class Unknown(Exception):
    """The store could not say whether it has the marker."""


class RequestError(Exception):
    """bits-console did not take a request."""


def _absent(exc):
    resp = getattr(exc, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code", ""))
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def _not_listed(s3, bucket):
    """Whether a listing shows the marker absent: S3 answers 403, not 404, for a
    missing object to a client that may not list the bucket."""
    try:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=MARKER, MaxKeys=1)
    except Exception:                     # pylint: disable=broad-except
        return False
    return not any(o.get("Key") == MARKER for o in page.get("Contents") or [])


def read(s3, bucket):
    """The marker of the store *bucket*, or None when it has none. A marker that
    is there but unreadable still says the orchestrator owns the set. Raises
    Unknown when the store cannot be asked."""
    try:
        body = s3.get_object(Bucket=bucket, Key=MARKER)["Body"].read()
    except Exception as exc:              # pylint: disable=broad-except
        if _absent(exc) or _not_listed(s3, bucket):
            return None
        raise Unknown("cannot read %s from the store: %s" % (MARKER, exc))
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    doc = doc if isinstance(doc, dict) else {}
    return {"issuer": str(doc.get("issuer") or "bits-orchestrator"),
            "manifests": str(doc.get("manifests") or "MANIFESTS/common-manifest"),
            "since": str(doc.get("since") or "?")}


def s3_of(store_url, work_dir="sw", arch="x86_64"):
    """(s3 client, bucket) of *store_url*, or None for a store that is not S3."""
    from bits_helpers.publish import _normalize_s3_store
    from bits_helpers.sync import remote_from_url
    store = _normalize_s3_store(store_url)
    if not store.startswith(("b3://", "s3://")):
        return None
    try:
        writer = remote_from_url(store, store, arch, work_dir)
    except BaseException as exc:          # remote_from_url may exit on a bad store
        raise Unknown("cannot open the store %s: %s" % (store_url, exc))
    s3 = getattr(writer, "s3", None)
    if s3 is None:
        return None
    return s3, getattr(writer, "remoteStore", None) or getattr(writer, "writeStore", None)


def for_store(store_url, work_dir="sw", arch="x86_64"):
    """The marker of the store at *store_url* (see :func:`read`); None for a store
    that is not S3, which no orchestrator writes."""
    client = s3_of(store_url, work_dir, arch)
    return read(*client) if client else None


def describe(marker):
    return "%s, since %s" % (marker["issuer"], marker["since"])


def console_url(explicit=None, system=None):
    """bits-console's URL: *explicit* (--console), $BITS_CONSOLE_URL, the
    defaults' ``system: console_url:``, then the site's ``console.backend_url``."""
    from bits_helpers import site
    return (explicit or os.environ.get("BITS_CONSOLE_URL") or (system or {}).get("console_url")
            or site.get("console.backend_url"))


class Console:
    """Requests to bits-console as the holder of *token* (a GitLab token, until
    bits-console signs people in itself)."""

    def __init__(self, url, token, cafile=None, insecure=False):
        self.base = url.rstrip("/")
        self.token = token
        self._tls = (cafile, insecure)
        self._call = None

    def check(self):
        """Raise RequestError for a URL a token must not be sent to (not https)."""
        from bits_helpers import sign_console
        try:
            sign_console._check_console_url(self.base, self._tls[1])
        except SystemExit as exc:
            raise RequestError(str(exc))

    def _json(self, path, obj=None):
        if self._call is None:
            from bits_helpers import sign_console
            self.check()                                   # a token is sent
            sign_console._set_tls(*self._tls)
            self._call = sign_console._json_call
        code, body = self._call(self.base + path, obj, bearer=self.token)
        if code not in (200, 201):
            raise RequestError("%s: HTTP %s%s" % (path, code or "unreachable",
                                                  ": %s" % body["detail"] if body.get("detail") else ""))
        return body

    def groups(self):
        """The groups of the certified set the caller administers."""
        return sorted((self._json("/ops/certified?limit=1").get("groups") or {}).keys())

    def decertify_package(self, group, package, reason="", arch=None):
        """Request the decertify-package action: *group*'s BOMs (of *arch*, or every
        architecture) are replaced by themselves without *package*'s entries; its
        manifests are then re-signed without them. Returns the action."""
        params = {"group": group, "package": package}
        if arch:
            params["arch"] = arch
        reason = " ".join(str(reason or "").split())[:500]
        if reason:
            params["reason"] = reason
        return self._json("/ops/actions", {"kind": "decertify-package", "params": params})

    def decertify(self, group, build_id, arch=None, reason=""):
        """Request the decertify action: *group*'s BOMs of *build_id* (of *arch*,
        or every architecture) leave the set; its manifests are then re-signed
        without them. Returns the action."""
        params = {"group": group, "build_id": build_id}
        if arch:
            params["arch"] = arch
        reason = " ".join(str(reason or "").split())[:500]
        if reason:
            params["reason"] = reason
        return self._json("/ops/actions", {"kind": "decertify", "params": params})

    @staticmethod
    def outcome(action):
        """(ok, words) for a decertify *action* as last seen: ok when done, or
        approved and waiting for the worker; words name what is left over."""
        state = action.get("state")
        if state not in ("done", "approved", "running"):
            return False, "%s %s" % (state, action.get("error") or "(see bits-console)")
        result = action.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = None
        result = result if isinstance(result, dict) else {}
        others = result.get("still_certified_by")
        words = state if state == "done" else "%s (the worker runs it next)" % state
        if state == "done" and "boms_replaced" in result:
            words += " (%d BOM(s) without it, %d taken out)" % (
                result["boms_replaced"], result.get("boms_removed", 0))
        if others:
            words += "; still certified by %s, until they decertify it too" % ", ".join(others)
        return True, words

    def settle(self, action, timeout=60):
        """Wait up to *timeout* seconds for *action* to finish; returns it as last seen."""
        deadline = time.monotonic() + timeout
        while action.get("state") not in ("done", "failed", "rejected") \
                and time.monotonic() < deadline:
            time.sleep(2)
            try:
                action = self._json("/ops/actions/%s" % int(action["id"]))
            except RequestError:
                break
        return action
