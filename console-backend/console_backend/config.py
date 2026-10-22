# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Environment-driven settings for the bits-console backend."""

import os


class Settings:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        # Security-proxy sign route (the Stage A client is reused by B3).
        self.sign_proxy_url = e.get("BITS_SIGN_PROXY_URL", "")
        # The gate token is read from the environment at sign time, never logged.
        self.sign_proxy_token_env = "BITS_SIGN_PROXY_TOKEN"
        # Preferred: the proxy's agent socket (read-only mount), which hands out the
        # CURRENT port and gate token — the proxy picks a random port per start and
        # rotates its token secret, so a static URL/token goes stale (signproxy.py).
        self.sign_proxy_agent_socket = e.get("BITS_SIGN_PROXY_AGENT_SOCKET", "")
        self.sign_proxy_host = e.get("BITS_SIGN_PROXY_HOST", "security-proxy")
        self.sign_proxy_route = e.get("BITS_SIGN_PROXY_ROUTE", "bits-manifest-sign")
        self.sign_proxy_prefix = e.get("BITS_SIGN_PROXY_PREFIX", "/sign/bits")

        # GitLab API for identity / authorization (forge.py).
        self.gitlab_api_url = e.get("GITLAB_API_URL") or e.get("CI_API_V4_URL", "")
        # Community-admin policy: a file path or inline text in the ADMINS format
        # that bits' `certify --admins` uses (same source of truth).
        self.admin_policy_source = e.get("BITS_ADMINS_POLICY", "")
        # A dedicated READ-ONLY GitLab token used to resolve `&group` admin refs,
        # so the admin set is deterministic and not observer-dependent. Falls back
        # to the caller's token if unset (dev).
        self.admin_resolve_token = e.get("BITS_ADMIN_RESOLVE_TOKEN", "")
        # Convention: a GitLab group whose members are OVERALL admins and whose
        # subgroups are per-community admins (subgroup path = community name). Set
        # this and the whole policy is derived from the group tree — no per-community
        # lines. Needs admin_resolve_token to read membership. BITS_ADMINS_POLICY
        # still merges on top as an override/supplement.
        self.admins_group = e.get("BITS_ADMINS_GROUP", "")

        # The GitLab Pages frontend origin (scheme+host) allowed to call this API
        # cross-origin (CORS). Humans authenticate by sending the GitLab token their
        # browser already holds as a bearer; this backend verifies it (identity.py)
        # instead of running its own OAuth/session.
        self.frontend_origin = e.get("BITS_FRONTEND_ORIGIN", "")

        # Catalog read cache (Phase C): a shared read-only GitHub token so the
        # console's package/recipe reads come from one authenticated upstream
        # (5000/hr) + a TTL cache, instead of each browser's 60/hr. Owners is the
        # allowlist of GitHub owners the /gh proxy may fetch (comma-separated);
        # empty = any owner (dev only).
        self.github_token = e.get("BITS_GITHUB_TOKEN", "")
        self.catalog_owners = [o.strip() for o in e.get("BITS_CATALOG_OWNERS", "").split(",") if o.strip()]
        self.catalog_ttl = int(e.get("BITS_CATALOG_TTL", "300") or "300")

        # Ops proxy (Phase 1): a forge (project) access token the backend uses to
        # ACTUATE operations (trigger a build; later cancel/retry/delete) on the
        # shared build project, gated by the caller's identity + authz. In the
        # backend env for now; custody moves into the security-proxy later.
        self.forge_ops_token = e.get("BITS_FORGE_OPS_TOKEN", "")
        self.forge_project = e.get("BITS_FORGE_PROJECT", "")   # numeric id or path
        # Refs the ops endpoint may build. Pinned (default: main) so a community
        # admin cannot trigger an arbitrary ref — whose committed CI config would
        # run with the project's protected credentials — to reach another community.
        self.forge_refs = {r.strip() for r in e.get("BITS_FORGE_REFS", "main").split(",") if r.strip()} or {"main"}

        # Phase 2: the backend is a GitLab OIDC client. On /auth/login it runs the
        # OIDC code flow (confidential client, with a secret), verifies the id_token,
        # and issues its OWN session — so the browser holds no GitLab token. Reuses
        # oidc_issuer + jwks_url above. login_redirect is THIS backend's callback;
        # frontend_post_login is where we hand the session back to the SPA.
        self.oidc_login_client_id = e.get("BITS_OIDC_LOGIN_CLIENT_ID", "")
        self.oidc_login_client_secret = e.get("BITS_OIDC_LOGIN_CLIENT_SECRET", "")
        self.oidc_login_redirect = e.get("BITS_OIDC_LOGIN_REDIRECT", "")   # e.g. https://bits.cern.ch/auth/callback
        self.oidc_token_url = e.get("BITS_OIDC_TOKEN_URL", "")             # e.g. https://gitlab.cern.ch/oauth/token
        self.oidc_authorize_url = e.get("BITS_OIDC_AUTHORIZE_URL", "")     # e.g. https://gitlab.cern.ch/oauth/authorize
        self.session_ttl = int(e.get("BITS_SESSION_TTL", "86400") or "86400")   # seconds; 24h default
        # OIDC max_age on login: force IdP re-authentication only when the SSO session
        # is OLDER than this. Defaults to the session TTL, so re-login after the
        # session lapses re-prompts, but nothing is forced within the TTL. Set 0 to
        # never force (re-auth stays silent while the CERN SSO session is alive).
        self.login_max_age = int(e.get("BITS_LOGIN_MAX_AGE") or self.session_ttl)
        # Allowed frontend origins we may hand a fresh session back to (redirect
        # target after login). Comma-separated; the post-login redirect must match.
        self.login_return_allow = [o.strip() for o in e.get("BITS_LOGIN_RETURN_ALLOW", "").split(",") if o.strip()]

        # GitLab CI ID token (OIDC workload identity) verification (B4).
        self.oidc_issuer = e.get("BITS_OIDC_ISSUER", "")            # e.g. https://gitlab.cern.ch
        self.oidc_ci_audience = e.get("BITS_OIDC_CI_AUDIENCE", "")  # required 'aud' claim
        self.jwks_url = e.get("BITS_OIDC_JWKS_URL", "")             # GitLab JWKS endpoint
        # Which CI projects may sign which groups (ADMINS-like text: "<project> <g>...";
        # "*" = any group). A file path or inline text.
        self.ci_signers_source = e.get("BITS_CI_SIGNERS", "")

        # WebAuthn RP (Stage C) — the per-operation 2nd-factor approval. rp_id is a
        # registrable domain the ceremony's page origin is under. Set it to a shared
        # parent (e.g. cern.ch) so the SAME passkey works both on the Pages frontend
        # (enrolment) and on this backend's own approve page (cross-device approval,
        # no SSO). A per-host rp_id would scope the credential to one origin only.
        self.rp_id = e.get("BITS_WEBAUTHN_RP_ID", "")          # e.g. cern.ch
        self.rp_name = e.get("BITS_WEBAUTHN_RP_NAME", "bits-console")
        self.rp_origin = e.get("BITS_WEBAUTHN_ORIGIN", "")     # e.g. https://bits-console.web.cern.ch
        # Every origin the ceremony may run on — enrolment on the Pages frontend AND
        # approval on this backend. All are accepted at verification. Comma-separated;
        # falls back to the single rp_origin when unset (single-origin deployments).
        _origins = e.get("BITS_WEBAUTHN_ORIGINS", "")
        self.rp_origins = [o.strip() for o in _origins.split(",") if o.strip()]
        self.credentials_path = e.get("BITS_WEBAUTHN_CREDENTIALS", "")  # JSON store path
        # Require user verification (biometric/PIN, not just presence). ON by
        # default; set 0 only for test authenticators that cannot do UV.
        self.webauthn_require_uv = e.get("BITS_WEBAUTHN_REQUIRE_UV", "1") != "0"
        # Enrolment authority (C6): a first passkey needs a bits-admin grant; a
        # further passkey needs step-up with an existing one. ON by default; set 0
        # only for dev/transition (reverts to session-only self-enrolment).
        self.enrollment_authority = e.get("BITS_ENROLLMENT_AUTHORITY", "1") != "0"
        # Require WebAuthn approval for ALL human signing (mandatory 2nd factor).
        # OFF by default so you can roll out: enable WebAuthn, have admins enrol,
        # then set 1 to enforce — otherwise a never-enrolled admin signs single-shot.
        self.webauthn_required = e.get("BITS_WEBAUTHN_REQUIRED", "0") == "1"

        # The orchestrator (orch/): its SQLite database, shared by the worker (which
        # migrates and owns it) and this API (requests, approvals). Empty = the
        # /ops/actions and /ops/events endpoints answer 503.
        self.orch_db = e.get("BITS_ORCH_DB", "")
        self.orch_interval = float(e.get("BITS_ORCH_INTERVAL", "30") or "30")
        # Read-only (read_api) GitLab token the worker reads pipelines, jobs and
        # job logs with; falls back to the ops token when unset.
        self.forge_read_token = e.get("BITS_FORGE_READ_TOKEN", "")
        # M8b: the token that commits the orchestrator's set to bits-manifests
        # (the mirror); through the credentials proxy's gitlab-mirror route
        # when BITS_CRED_PROXY_AGENT_SOCKET is set.
        self.forge_mirror_token = e.get("BITS_FORGE_MIRROR_TOKEN", "")
        # How far back the worker looks for pipelines when it starts with an empty
        # database (days); older history is the failure backfill's job.
        self.orch_lookback_days = float(e.get("BITS_ORCH_LOOKBACK_DAYS", "7") or "7")
        # The store the builds' BOMs and build index are in (b3://<bucket>; a
        # trailing ::rw is ignored: the worker only reads). Empty = builds are
        # recorded from GitLab only, without their BOMs.
        self.orch_store = e.get("BITS_ORCH_STORE", "")
        # The store is publicly readable: read it with no credentials at all, at
        # BITS_ORCH_S3_ENDPOINT (else bits' BITS_S3_ENDPOINT_URL; https). Until M8 the worker only reads, so it then
        # needs neither keys nor the credentials proxy's s3 route.
        self.orch_store_anonymous = (e.get("BITS_ORCH_STORE_ANONYMOUS", "") or "").strip().lower() \
            in ("1", "true", "yes")
        self.orch_s3_endpoint = (e.get("BITS_ORCH_S3_ENDPOINT") or e.get("BITS_S3_ENDPOINT_URL")
                                 or "https://s3.cern.ch")
        # The audience of the CI ID tokens build jobs send with their hints
        # (/ops/ci/*): its own, so a hint token can never sign anything.
        self.orch_ci_audience = e.get("BITS_ORCH_CI_AUDIENCE", "bits-orchestrator")
        # Failure backfill (M2): how many days of older failed pipelines to read
        # from GitLab, a page per pass, once. 0 = off.
        self.orch_backfill_days = float(e.get("BITS_ORCH_BACKFILL_DAYS", "0") or "0")
        # The worker keeps the console's CVMFS status document (cvmfs-status.json
        # in the build project's package registry), in place of a CI job per build.
        self.orch_cvmfs_status = (e.get("BITS_ORCH_CVMFS_STATUS", "") or "").strip().lower() \
            in ("1", "true", "yes")
        # The build queue: the console's builds wait here and the worker starts
        # them one per build host (the hosts file of the build project).
        self.orch_queue = (e.get("BITS_ORCH_QUEUE", "") or "").strip().lower() \
            in ("1", "true", "yes")
        self.orch_build_hosts = e.get("BITS_ORCH_BUILD_HOSTS", "config/build-hosts.yaml") \
            or "config/build-hosts.yaml"
        # prepub nodes whose measurement records say which build published each
        # CVMFS path (trace.py, for `bits trace`); none: no such index.
        self.orch_prepub_urls = [u.strip() for u in (e.get("BITS_ORCH_PREPUB_URLS") or "").split(",")
                                 if u.strip()]
        # A daily copy of the orchestrator database (approvals, failures, the
        # audit chain cannot be rebuilt): a directory, ideally on another disk.
        # Empty = no copies.
        self.orch_backup_dir = e.get("BITS_ORCH_BACKUP_DIR", "")
        # Certification in shadow (M4): the bits-manifests project whose main
        # pipelines certify (path or id; empty = off). The worker recomputes each
        # certification its certify job signed and compares; it signs nothing.
        # Read with the read token; the store is BITS_ORCH_STORE.
        self.orch_manifests_project = e.get("BITS_ORCH_MANIFESTS_PROJECT", "")
        self.orch_manifests_branch = e.get("BITS_ORCH_MANIFESTS_BRANCH", "main") or "main"
        self.orch_certify_job = e.get("BITS_ORCH_CERTIFY_JOB", "certify-and-publish")
        self.orch_manifest_prefix = e.get("BITS_ORCH_MANIFEST_PREFIX",
                                          "MANIFESTS/common-manifest")
        # M8b, the switch: the worker owns the certified set (no longer read from
        # bits-manifests, which it mirrors), writes the production manifests
        # (BITS_ORCH_MANIFEST_PREFIX, without expiry) and is the only signer;
        # the backend's /sign routes are gone. Unset again = back to M8a.
        self.orch_owns_set = (e.get("BITS_ORCH_OWNS_SET", "") or "").strip().lower() in (
            "1", "true", "yes")
        # Certifications older than this when the worker first looks are not compared.
        self.orch_shadow_lookback_days = float(e.get("BITS_ORCH_SHADOW_LOOKBACK_DAYS", "3") or "3")
        # The architecture bits sign assumes for a package without one (its
        # --architecture); empty = such packages count as absent from the store.
        self.orch_default_arch = e.get("BITS_ORCH_DEFAULT_ARCH", "")
        # The outside copy of the audit chain (M4): the audit-log service (on
        # this host for now, http://audit-log:8443 on an internal network; later
        # on another node, over https). Empty URL = not shipped. TOKEN may be a
        # file path.
        self.orch_audit_url = e.get("BITS_ORCH_AUDIT_URL", "")
        self.orch_audit_token = e.get("BITS_ORCH_AUDIT_TOKEN", "")
        self.orch_audit_cafile = e.get("BITS_ORCH_AUDIT_CAFILE", "")
        # The worker signs only while the outside copy was found in step within
        # this many minutes (and is not diverged).
        self.orch_sign_outside_minutes = float(e.get("BITS_ORCH_SIGN_OUTSIDE_MINUTES", "10")
                                               or "10")
        # The credentials proxy (M6): a second security-proxy instance holding the
        # GitLab tokens and the store's keys, which the backend and the worker
        # reach with gate tokens from its agent socket. Set, it replaces
        # BITS_FORGE_OPS_TOKEN, BITS_FORGE_READ_TOKEN, BITS_ADMIN_RESOLVE_TOKEN and
        # the worker's S3 keys; unset, those are used as before.
        self.cred_proxy_agent_socket = e.get("BITS_CRED_PROXY_AGENT_SOCKET", "")
        self.cred_proxy_host = e.get("BITS_CRED_PROXY_HOST", "credentials-proxy") or "credentials-proxy"
        # The store writer (M8a): a security-proxy instance only the worker
        # reaches, whose s3 route holds the keypair the worker writes its
        # manifests with. Not the credentials proxy, which the backend can reach too.
        self.store_writer_agent_socket = e.get("BITS_STORE_WRITER_AGENT_SOCKET", "")
        self.store_writer_host = e.get("BITS_STORE_WRITER_HOST", "store-writer") or "store-writer"
        # The worker certifies builds (M8a on): it signs through the signer proxy
        # and writes the result through the store writer. Off unless
        # BITS_ORCH_CERTIFY is set.
        self.orch_certify = (e.get("BITS_ORCH_CERTIFY", "") or "").strip().lower() in ("1", "true", "yes")
        self.orch_certify_valid_days = float(e.get("BITS_ORCH_CERTIFY_VALID_DAYS", "30") or "30")
        if not (0 < self.orch_certify_valid_days <= 3650):
            # 0 would mean "never expires" to bits: not until consumers check `sequence`.
            raise ValueError("BITS_ORCH_CERTIFY_VALID_DAYS must be above 0 and at most 3650")
        # At most this many signatures a day: past it, certifying pauses.
        self.orch_certify_daily_signs = int(e.get("BITS_ORCH_CERTIFY_DAILY_SIGNS", "100") or "100")
        # The canary (M8a, until M8b): where the worker writes its manifests,
        # which production does not read, and their comparison with the CI's. A
        # worker certification with no CI certification of its build after this
        # many days is 'unmatched'; a CI certification with none after this
        # many, 'missing'.
        self.orch_canary_prefix = (e.get("BITS_ORCH_CANARY_PREFIX", "CANARY/common-manifest")
                                   or "CANARY/common-manifest").strip("/")
        self.orch_canary_unmatched_days = float(e.get("BITS_ORCH_CANARY_UNMATCHED_DAYS", "7") or "7")
        self.orch_canary_missing_days = float(e.get("BITS_ORCH_CANARY_MISSING_DAYS", "1") or "1")
        # The region the worker signs S3 requests for (the proxy re-signs with it).
        self.orch_s3_region = e.get("BITS_ORCH_S3_REGION", "us-east-1") or "us-east-1"

    def oidc_login_configured(self) -> bool:
        # Only issuer + client id + redirect are required. The authorize/token/jwks
        # endpoints are DISCOVERED from the issuer's well-known doc (the explicit
        # oidc_*_url vars are optional overrides). client_secret is OPTIONAL too:
        # reusing the existing PUBLIC PKCE app needs none.
        return bool(self.oidc_login_client_id and self.oidc_login_redirect
                    and self.oidc_issuer)

    def sign_proxy_configured(self) -> bool:
        return bool(self.sign_proxy_agent_socket or self.sign_proxy_url)

    def webauthn_configured(self) -> bool:
        # credentials_path is required: without it enrolments are held in memory
        # and lost on restart — dangerous for a factor that gates signing.
        return bool(self.rp_id and (self.rp_origin or self.rp_origins)
                    and self.credentials_path)
