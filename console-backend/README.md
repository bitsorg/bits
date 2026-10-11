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
`/sign/approve` (human passkey approval), `/sign/cli/*` (cross-device),
`/webauthn/*` (enrolment + bits-admin grant), and `/gh/{path}` (cached,
authenticated GitHub read proxy for package browsing — see `BITS_GITHUB_TOKEN`,
`BITS_CATALOG_OWNERS`, `BITS_CATALOG_TTL`).

## Run / test (dev)

    # from bits/console-backend, with the bits repo on the path for bits_helpers:
    PYTHONPATH=.:.. python3 -m unittest discover -s tests
    PYTHONPATH=.:.. uvicorn console_backend.main:app --port 8080   # /healthz

## Orchestrator

`console_backend/orch/` is the bits orchestrator: a worker that records builds,
runs actions, and (in later milestones) signs per architecture, starts
schedules and collects failures. It runs as its own process from the same
image, beside this API, and shares one SQLite database with it:

    BITS_ORCH_DB=/orch/orch.db python3 -m console_backend.orch.worker

The worker owns the database: it takes a lock beside it (a second worker
exits), applies the numbered migrations, and runs one pass every
`BITS_ORCH_INTERVAL` seconds (default 30), touching `<db>.heartbeat` after each.
The API only writes requests and decisions, and answers 503 until the worker
has created and migrated the database. The worker has no user token, so when
the admin policy names GitLab groups (`&group` lines or `BITS_ADMINS_GROUP`) it
needs `BITS_ADMIN_RESOLVE_TOKEN`, and refuses to start without it.

Every change of state is an **action**, of a kind with a JSON schema
(`GET /ops/actions/catalogue`). A person who administers the action's community
requests it (`POST /ops/actions`); kinds marked `requester` are approved by
that request, `approver` kinds need `POST /ops/actions/{id}/approve`. An
`advisor` actor can only propose. The worker re-checks the approver's authority
when it runs an action, retries transient failures with backoff (at most 5
attempts), and runs one action at a time per subject.

Everything that happens is an **event** (`GET /ops/events?after=&wait=`, overall
admins): an append-only log whose hash chain is the audit trail.
