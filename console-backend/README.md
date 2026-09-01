# bits-console backend

The relying party for manifest signing and the **only** client of the
security-proxy. A Python/FastAPI service that reuses `bits_helpers` (`forge` for
GitLab identity/authorization, `trust`/`certify` for signing via the proxy).
Deployed as a `bits-services` container on `signer-net`; `web.cern.ch` is the
public TLS front. Holds **no** signing key.

Build-out (see `bits-M1-implementation-roadmap-*.md`, Stage B):

- **B1** skeleton — health + config + `bits_helpers` wiring *(this)*.
- **B2** GitLab OIDC confidential client — server-side login + session.
- **B3** sign endpoint (Mode 1) — authenticated + community-admin → `certify`
  via the proxy → signed manifest + audit entry.
- **B4** CI identity — GitLab CI ID token for automated signs.

## Run / test (dev)

    # from bits/console-backend, with the bits repo on the path for bits_helpers:
    PYTHONPATH=.:.. python3 -m unittest discover -s tests
    PYTHONPATH=.:.. uvicorn console_backend.main:app --port 8080   # /healthz
