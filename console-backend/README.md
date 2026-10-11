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

Each pass first **reconciles** builds (`orch/reconcile.py`), trusting GitLab and
S3 rather than what jobs report: it lists the build project's pipelines updated
since the last pass (`BITS_ORCH_LOOKBACK_DAYS`, default 7, on an empty
database), reads a new pipeline's variables once, decides each build pipeline's
outcome from its CI jobs (bits' progress status can hold a pipeline "running" or
fail it late), and reads a finished pipeline's builds from the S3 build index in
`BITS_ORCH_STORE` (`b3://<bucket>`). GitLab is read with `BITS_FORGE_READ_TOKEN`
(read_api) when set. `GET /ops/builds` and `GET /ops/builds/{pipeline}` serve
what it recorded, for the communities the caller administers. A build job may
send a hint (`POST /ops/ci/published|failed {pipeline}`) with a CI ID token for
the audience `BITS_ORCH_CI_AUDIENCE` (default `bits-orchestrator`), from the
build project on a protected ref; the worker then looks at that pipeline on
its next pass.

Every failed job of a failed build pipeline is then **recorded**
(`orch/failures.py`): the end of its log, or of its `bits-build.log` artifact
when GitLab cut the log at its size limit, is parsed for bits' `BUILD FAILED:
<pkg>@<version>` blocks and their error excerpt (else the job's last lines and
GitLab's failure reason), and the excerpt is **redacted** (tokens, auth headers,
URL passwords, key=value secrets, private keys) before it is stored. Failures
with the same package and error lines, once paths, hashes, numbers and times
are taken out, are one **issue**; an active **signature** (a regular expression
over the excerpt and `reason:<failure_reason>`) names its cause and suggested
action. Four signatures are seeded once. `BITS_ORCH_BACKFILL_DAYS` (default 0)
reads that many days of older failed pipelines from GitLab, a page per pass,
once. `GET /ops/issues`, `/ops/issues/{id}`, `/ops/failures` and
`/ops/signatures` serve the record, limited to the caller's communities (an
issue seen in several shows a community admin only their own occurrences,
counts and excerpt). A pipeline whose logs cannot be read is tried again every
10 minutes for a day.
