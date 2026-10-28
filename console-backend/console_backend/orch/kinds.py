# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The action kinds. M0 has the two pipeline operations the console already
offers, so the whole path (request, approval, run, events) is exercised before
certification depends on it; later milestones add certify, release, renew,
revoke, start-build, schedule changes and signature proposals here."""

import threading

from .. import credproxy, forge_ops
from . import actions

_PIPELINE = {
    "type": "object",
    "properties": {"pipeline": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,19})$",
                                "description": "GitLab pipeline id in the build project"}},
    "required": ["pipeline"],
    "additionalProperties": False,
}


class NotConfigured(actions.Retry):
    """A service an action needs is not configured here: 503 to a caller, and a
    transient failure to the worker (the deployment is fixed, the action runs)."""


class Deps:
    """What runners and scope functions may use. Built per call from the
    settings, so a test or a settings change takes effect immediately."""

    def __init__(self, settings):
        self.settings = settings

    def forge(self):
        f = forge_ops.GitLabForge.from_settings(self.settings)
        if f is None:
            raise NotConfigured("forge not configured (BITS_FORGE_OPS_TOKEN/PROJECT)")
        return f

    def reader(self):
        """GitLab for reading: pipelines, jobs and logs with the read_api token
        when set (else the ops token); a pipeline's variables with the ops token,
        as the backend's own /ops/pipeline/{id}/variables does, because GitLab
        shows them only to a Maintainer and the read token must not be one (a
        Maintainer's read_api token can read the project's CI/CD secrets)."""
        s = self.settings
        read = forge_ops.GitLabForge.from_settings(s, "read")
        if read is None:
            raise NotConfigured("GitLab not configured (GITLAB_API_URL, BITS_FORGE_PROJECT,"
                                " BITS_FORGE_READ_TOKEN or BITS_CRED_PROXY_AGENT_SOCKET)")
        ops = forge_ops.GitLabForge.from_settings(s)
        if ops is not None:
            read.pipeline_variables = ops.pipeline_variables
        return read

    def manifests_reader(self):
        """The bits-manifests project (certification in shadow), read with the
        read token; None when BITS_ORCH_MANIFESTS_PROJECT is not set."""
        s = self.settings
        if not s.orch_manifests_project:
            return None
        read = forge_ops.GitLabForge.from_settings(s, "read", project=s.orch_manifests_project)
        if read is None:
            raise NotConfigured("GitLab not configured (GITLAB_API_URL, BITS_FORGE_READ_TOKEN"
                                " or BITS_CRED_PROXY_AGENT_SOCKET)")
        read._timeout = 60
        return read

    _store = None
    _store_lock = threading.Lock()     # the shadow thread shares it; boto3 clients
                                       # must not be created by two threads at once

    def store(self):
        """(s3 client, bucket) of BITS_ORCH_STORE, or None when it is not set.
        Built once: the client keeps its connection."""
        url = (self.settings.orch_store or "").split("::", 1)[0]
        if not url:
            return None
        if not url.startswith("b3://"):
            raise NotConfigured("BITS_ORCH_STORE must be b3://<bucket>")
        bucket = url[len("b3://"):].split("/", 1)[0]
        if self.settings.orch_store_anonymous:
            # A publicly readable store: unsigned requests, no keys anywhere.
            with self._store_lock:
                key = ("anonymous", self.settings.orch_s3_endpoint, url)
                if self._store is None or self._store[0] != key:
                    client = _anonymous_client(self.settings)
                    _check_bucket(client, bucket, anonymous=True)
                    self._store = (key, client, bucket)
                return self._store[1], self._store[2]
        if credproxy.proxied(self.settings):
            # Through the credentials proxy's s3 route: it holds the keys.
            client, new = credproxy.s3_client(self.settings)
            if new:
                _check_bucket(client, bucket)
            return client, bucket
        with self._store_lock:
            if self._store is None or self._store[0] != url:
                import tempfile
                from bits_helpers.sync import remote_from_url
                # Read only: no write store ("", not None, which the S3 backend cannot take).
                r = remote_from_url(url, "", "", tempfile.gettempdir())
                self._store = (url, r.s3, r.remoteStore)
            return self._store[1], self._store[2]


def _anonymous_client(settings):
    endpoint = settings.orch_s3_endpoint
    if not endpoint.startswith("https://"):
        raise NotConfigured("BITS_ORCH_S3_ENDPOINT must be https (%s)" % endpoint)
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    # Its own session: the default one is not safe to share between threads.
    return boto3.session.Session().client(
        "s3", endpoint_url=endpoint, region_name=settings.orch_s3_region,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"},
                      retries={"max_attempts": 3},
                      request_checksum_calculation="WHEN_REQUIRED",
                      response_checksum_validation="WHEN_REQUIRED"))


def _check_bucket(client, bucket, anonymous=False):
    """A bucket that cannot be read answers 403 or 404 like a missing object
    would: find that out once, not as every object looking absent."""
    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as e:
        status = (getattr(e, "response", None) or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in (403, 404) and anonymous:
            raise NotConfigured("bucket %s cannot be read anonymously (HTTP %s): is it public?"
                                " Else unset BITS_ORCH_STORE_ANONYMOUS." % (bucket, status))
        if status in (403, 404):
            raise NotConfigured("the credentials proxy cannot read bucket %s (HTTP %s): is it in"
                                " the s3 route of its config, with its keypair pushed?"
                                % (bucket, status))
        raise


def _transient(e):
    return e.status == 0 or e.status == 429 or e.status >= 500


def _pipeline_scope(params, deps):
    """A pipeline's community is its own COMMUNITY variable; none means
    overall admins only (as the console's pipeline operations decide today)."""
    return deps.forge().pipeline_community(params["pipeline"]) or ""


def _forge_call(fn, pid):
    try:
        fn(pid)
    except forge_ops.ForgeError as e:
        if _transient(e):
            raise actions.Retry("forge %s: %s" % (e.status, e.message))
        raise RuntimeError("forge %s: %s" % (e.status, e.message))


def _retry(params, deps):
    _forge_call(deps.forge().retry_pipeline, params["pipeline"])
    return {"status": "retried"}


def _cancel(params, deps):
    _forge_call(deps.forge().cancel_pipeline, params["pipeline"])
    return {"status": "canceled"}


actions.register(actions.Kind(
    name="pipeline.retry", summary="Retry the failed jobs of a pipeline",
    params=_PIPELINE, approval="requester",
    subject=lambda p: "pipeline:%s" % p["pipeline"], scope=_pipeline_scope, run=_retry,
    safe_to_rerun=True))

actions.register(actions.Kind(
    name="pipeline.cancel", summary="Cancel a running pipeline",
    params=_PIPELINE, approval="requester",
    subject=lambda p: "pipeline:%s" % p["pipeline"], scope=_pipeline_scope, run=_cancel,
    safe_to_rerun=True))


# Registered by its module (M8a): approved by a build's pre-approval only.
from . import certifier  # noqa: E402,F401
from . import hosts  # noqa: E402,F401
