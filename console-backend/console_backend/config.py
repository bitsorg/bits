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

        # GitLab OIDC / OAuth2 (Authorization Code + PKCE), server-side.
        self.oidc_authorize_url = e.get("BITS_OIDC_AUTHORIZE_URL", "")
        self.oidc_token_url = e.get("BITS_OIDC_TOKEN_URL", "")
        self.oidc_client_id = e.get("BITS_CONSOLE_OIDC_CLIENT_ID", "")
        # Optional: present it only for a *confidential* app. PKCE alone (public
        # app) also works because the exchange happens server-side.
        self.oidc_client_secret = e.get("BITS_CONSOLE_OIDC_CLIENT_SECRET", "")
        self.oidc_redirect_uri = e.get("BITS_OIDC_REDIRECT_URI", "")
        self.oidc_scopes = e.get("BITS_OIDC_SCOPES", "read_api")

        # Session cookie. secure=True by default; set BITS_SESSION_COOKIE_SECURE=0
        # only for local http dev.
        self.session_ttl_seconds = int(e.get("BITS_SESSION_TTL", "28800"))
        self.session_cookie_secure = e.get("BITS_SESSION_COOKIE_SECURE", "1") != "0"

    def sign_proxy_configured(self) -> bool:
        return bool(self.sign_proxy_url)

    def oidc_configured(self) -> bool:
        return bool(self.oidc_authorize_url and self.oidc_token_url
                    and self.oidc_client_id and self.oidc_redirect_uri
                    and self.gitlab_api_url)
