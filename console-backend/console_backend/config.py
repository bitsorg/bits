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

        # GitLab API for identity / authorization (forge.py).
        self.gitlab_api_url = e.get("GITLAB_API_URL") or e.get("CI_API_V4_URL", "")
        # Community-admin policy: a file path or inline text in the ADMINS format
        # that bits' `certify --admins` uses (same source of truth).
        self.admin_policy_source = e.get("BITS_ADMINS_POLICY", "")
        # A dedicated READ-ONLY GitLab token used to resolve `&group` admin refs,
        # so the admin set is deterministic and not observer-dependent. Falls back
        # to the caller's token if unset (dev).
        self.admin_resolve_token = e.get("BITS_ADMIN_RESOLVE_TOKEN", "")

        # The GitLab Pages frontend origin (scheme+host) allowed to call this API
        # cross-origin (CORS). Humans authenticate by sending the GitLab token their
        # browser already holds as a bearer; this backend verifies it (identity.py)
        # instead of running its own OAuth/session.
        self.frontend_origin = e.get("BITS_FRONTEND_ORIGIN", "")

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

    def sign_proxy_configured(self) -> bool:
        return bool(self.sign_proxy_url)

    def webauthn_configured(self) -> bool:
        # credentials_path is required: without it enrolments are held in memory
        # and lost on restart — dangerous for a factor that gates signing.
        return bool(self.rp_id and (self.rp_origin or self.rp_origins)
                    and self.credentials_path)
