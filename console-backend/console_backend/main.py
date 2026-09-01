# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-console backend — relying party and the ONLY client of the security-proxy.

Stage B skeleton (B1): health + config wiring, and a check that ``bits_helpers``
(reused for authz in B2 and signing in B3) is importable. GitLab OIDC login (B2),
the sign endpoint (B3) and the CI-identity path (B4) layer on top.
"""

from fastapi import FastAPI

from . import config

try:  # bits_helpers is provided by the surrounding bits repo (on PYTHONPATH).
    from bits_helpers import forge, trust  # noqa: F401  (used by B2/B3)
    _BITS_HELPERS = True
except Exception:  # pragma: no cover - environment check only
    _BITS_HELPERS = False

settings = config.Settings()
app = FastAPI(title="bits-console backend", version="0.1.0")


@app.get("/healthz")
def healthz():
    """Liveness + wiring status. No secrets in the response."""
    return {
        "status": "ok",
        "bits_helpers": _BITS_HELPERS,
        "sign_proxy_configured": settings.sign_proxy_configured(),
    }
