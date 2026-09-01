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
        # GitLab API for identity / authorization (forge.py), wired in B2.
        self.gitlab_api_url = e.get("GITLAB_API_URL") or e.get("CI_API_V4_URL", "")
        # OIDC confidential client id, wired in B2.
        self.oidc_client_id = e.get("BITS_CONSOLE_OIDC_CLIENT_ID", "")

    def sign_proxy_configured(self) -> bool:
        return bool(self.sign_proxy_url)
