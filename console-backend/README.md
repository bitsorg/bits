# bits-console backend

The relying party for manifest signing and the **only** client of the
security-proxy. A Python/FastAPI service that reuses `bits_helpers` (`trust` for
signing via the proxy, `forge`/authz for GitLab group policy). Runs on its **own
origin** (e.g. `bits.cern.ch`), reached by the GitLab Pages frontend
(`bits-console.web.cern.ch`); holds **no** signing key.

Auth is **stateless**: the bits-console SPA logs the user in with GitLab (OAuth
PKCE in the browser) and calls this API with `Authorization: Bearer <gitlab-token>`.
The backend verifies that token against GitLab (`GET /user`, `identity.py`) — there
is no server-side OAuth or session here. CI callers send a GitLab CI **ID token**
on the same header. The Pages frontend is a different origin, allowed via CORS
(`BITS_FRONTEND_ORIGIN`); the WebAuthn `rp_id`/origin are the **Pages** host, not
this backend's.

Endpoints: `/healthz`, `/me`, `/sign` (CI single-shot), `/sign/request` +
`/sign/approve` (human passkey approval), `/sign/cli/*` (cross-device), and
`/webauthn/*` (enrolment + bits-admin grant).

## Run / test (dev)

    # from bits/console-backend, with the bits repo on the path for bits_helpers:
    PYTHONPATH=.:.. python3 -m unittest discover -s tests
    PYTHONPATH=.:.. uvicorn console_backend.main:app --port 8080   # /healthz
