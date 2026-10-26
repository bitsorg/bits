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

## Part of bits

The backend signs, certifies and reads the store with the same `bits_helpers`
code as the bits CLI (`trust`, `certify`, `forge`, `sync`, `cvmfs_release`), so
the formats cannot drift apart. It is therefore not released on its own: it is
deployed from the bits commit that bits-console pins as a submodule, and it
uses only the public functions of those modules (no `_private` helpers).

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

**The build queue** (`orch/build_queue.py`, with `BITS_ORCH_QUEUE=1`): the
console queues its builds (`POST /ops/queue {community, ref?, builds[]}`, each
checked as `/ops/build` checks one) instead of starting them, and the worker
starts them, oldest first, at most one per build host, so a long build never
waits in GitLab (where its pending jobs count against the project's job limit)
nor shares a host. The hosts are the build project's `config/build-hosts.yaml`
(`BITS_ORCH_BUILD_HOSTS`): `name`, `tag` (`bits-host-<name>`), `arch`, and
`runner` when the GitLab runner's description is not the name. A build pinned to
a host (`BITS_RUNNER_TAG=bits-host-<name>`) waits for it; one routed by
architecture (`bits-build-<arch>`) takes the first free host of that arch and is
pinned to it. A host is busy while a build pipeline runs on it, the queue's or
not, and takes nothing while GitLab does not see its runner online (switched off,
asleep, paused: `GET /projects/:id/runners`, which needs the worker's token to be
a Maintainer's; without it every host counts as online): its builds wait in the
queue, as long as it takes, not in GitLab, which drops a job no runner can take
within hours. A build GitLab drops all the same before any runner took it is
queued again, its pre-approval given back to the batch (a retry of the dropped
pipeline would build it twice, and certify nothing). A host's runner is the one
with its unique tag (`bits-host-<name>`), whatever its description; a host no
runner carries the tag of is logged once. GitLab refusing a pipeline (its job limit) leaves the build in its place;
refusing one build five times refuses it. `GET /ops/queue` lists the waiting
builds, the hosts and the pause (GitLab refusing pipelines: why, until when), `POST /ops/queue/{id}/cancel` takes one out before it
starts. A Retry of a pipeline restarts it at once, outside the queue. With
`certify_groups` the answer carries a passkey challenge over the builds that
certify (by digest of ref, name and variables); `POST /ops/queue/approve
{request_id, assertion}` records it as the batch's standing approval (one run
per build, 7 days), and each such build, as it starts, takes a run that becomes
the console pre-approval of its pipeline (24 hours), which the worker's
certification needs. Without a passkey the builds are queued all the same
(`approval_error` says why). Revoking the batch's approval revokes the
pre-approvals it gave.

**Phases** (`orch/phases.py`): for each build pipeline finished in the last week,
what its build job spent its time on, for the console's timeline: queued (from
the build queue's request, else the pipeline's creation), setup, build, and per
package prepub's work and the gateway's commit. They come from the job's
`bits-phases.txt` and prepub's measurement records (`measurements-<pipeline>.json`),
both kept as job artifacts, else from the job's own times; a retried pipeline is
looked at again. `GET /ops/builds` returns them as `phases: {host, segments}`.

**Trace** (`orch/trace.py`, with `BITS_ORCH_PREPUB_URLS`, the prepub nodes,
comma-separated): which build published each CVMFS path, read from every prepub's
measurement records (`GET /api/v1/measurements`), which prepub keeps for every
build, so the index reaches back before the orchestrator; a recent build is read
again while its records still come. `GET /ops/trace?repo=&path=&build_id=`
answers `bits trace`: the publishes of the longest published path above *path*,
and for each publishing pipeline and each pipeline of the build *build_id* (from
the package's `.meta.json`) who requested it (`BITS_CERTIFIER`, or the queue's
requester) and its passkey pre-approval with its event in the audit chain; for a
caller who does not administer the pipeline's community, the pipeline id only.

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

**Approvals** are kept in the same database when `BITS_ORCH_DB` is set
(`orch/approvals.py`): an approved pre-approval (console or CLI) is stored with
the passkey assertion that approved it, survives a restart, expires 24 hours
after approval, and its signs are counted atomically (one per architecture, at
most 16); pending ones wait in memory for their passkey, 10 minutes. Without the
database, they stay in memory as before. A **standing approval**
(`POST /approvals/standing/request` then `/approve`, with a passkey) lets a
schedule's builds be certified without a person each time: bound to the
schedule file's sha256 and its groups, for 1-100 runs or up to 30 days. `GET
/approvals` lists the approvals of the caller's communities, and `POST
/approvals/{id}/revoke` (by its approver, an admin of all its groups, or an
overall admin; no passkey) ends one at once. Every grant, use and revocation is an event. With `BITS_ORCH_BACKUP_DIR`
the worker copies the database there once a day and keeps a week.

**Certification in shadow** (`orch/shadow.py`, M4), when
`BITS_ORCH_MANIFESTS_PROJECT` names bits-manifests: for every main pipeline whose
certify job signed, the worker computes the same manifests itself, with bits'
own merge and store check (`certify.build_by_arch`), from the tree at that commit,
the architectures the push changed (worked out as the CI job does) and the store
(`BITS_ORCH_STORE`). It compares them with what the job signed (its artifacts, or
the store's copy while it still carries that commit), once their signature
verifies; timestamps and `certified_by` aside, they must be equal. Each
architecture is a sign run and a `shadow.match` or `shadow.mismatch` event; `GET
/ops/certifications` and `/ops/certifications/summary?days=` show them to overall
admins. It signs, commits and writes nothing, runs on a thread of its own, and
remembers the sha256 of store objects so only new ones are hashed.

**The certified set** (`orch/certified.py`, M5): the worker keeps its own record
of which BOMs are certified, so that bits-manifests can be retired. Each
certified BOM's exact bytes are kept in the database, named by their sha256;
the set is a sequence of versions, for now one per bits-manifests main pipeline
(the first imports the whole tree), each adding and removing BOMs as that
commit did. Every addition and removal is an event (`certified.added`,
`certified.removed`) with the path, group, architecture, build and sha256. Each
BOM is also matched to its build's copy in the store, ignoring the
`certified_by` certification adds (`same`, `differs`, `absent`). The shadow
computes every certification from the set as well (sign runs of mode `set`);
the two must agree. `GET /ops/certified` lists the set, `GET
/ops/certified/bom/{sha256}` returns one BOM.

**The worker's own certification, replayed** (`orch/replay.py`, M8a, first step):
for each certification the shadow compares, the worker also works out what it
would have certified for that build on its own terms, and compares it with what
the CI signed (sign runs of mode `replay`, events `replay.*`): the build the push
added BOMs for (console builds only); its console pre-approval, in force when the
CI signed (no pre-approval, no certification); the build's own BOMs from the
store (the keys of its build index), at the merge request's paths; its certify
group from the community's `ui-config.yaml` at the pipeline's commit; the
pre-approver an admin of every group in the result; the key policy (none is a
refusal); the store's objects as the shadow checks them. A refusal or a build it has nothing
to certify from is a sign run too (`refused`, `missing`). It signs nothing.
`/ops/certifications/summary` counts them under `replay`.

**Worker certification** (`orch/certifier.py`, M8a on; on with `BITS_ORCH_CERTIFY=1`):
the worker certifies each new build that asked for it (`CERTIFY=true`) itself,
minutes after it. Only the build's console pre-approval approves it: the
worker requests a `certify` action already approved by it, and nobody can
approve one in the console. It runs on a thread of its own: the build's BOMs
from the store, the certified set at its latest version as the baseline, bits'
merge and store check, and per architecture the sign path's checks (the
approver an admin of every group, the key policy, none being a refusal). The
bytes are kept before they are signed, so a rerun signs the same bytes again;
signatures are verified, and written through the store writer
(`BITS_STORE_WRITER_AGENT_SOCKET`, a security-proxy instance only the worker
reaches), at most `BITS_ORCH_CERTIFY_DAILY_SIGNS` a day. Manifests are valid
`BITS_ORCH_CERTIFY_VALID_DAYS` (30). `GET /ops/certify`, and `certify` in
`/ops/certifications/summary`.

**The canary** (`orch/canary.py`, M8a until M8b): the worker writes its
manifests under `CANARY/` (`BITS_ORCH_CANARY_PREFIX`; production reads
`MANIFESTS/`), and each is compared with the CI's certification of the same
build (`match`, `mismatch`, or `rebased` when the set was at another commit;
`unmatched` and `missing` for one side alone): `canary` in
`/ops/certifications/summary` and on the rows of `GET /ops/certify`. M8b
deletes it.

**Credentials** (`credproxy.py`, M6): with `BITS_CRED_PROXY_AGENT_SOCKET`, the
backend and the worker hold no GitLab token or store key. They call GitLab and
the store through the credentials proxy (bits-services `credentials-proxy`, a
second security-proxy instance), sending its gate token where the real token
went (`PRIVATE-TOKEN`, or the S3 access key); the proxy adds the real one on the
way out. Routes: `gitlab-ops`, `gitlab-read`, `gitlab-groups`, `s3`. Without
the socket, `BITS_FORGE_OPS_TOKEN`, `BITS_FORGE_READ_TOKEN`,
`BITS_ADMIN_RESOLVE_TOKEN` and the S3 keys are used as before.

**The outside copy** (`orch/outside.py`, M4): with `BITS_ORCH_AUDIT_URL` and
`BITS_ORCH_AUDIT_TOKEN`, the worker sends the event log to the audit-log service
(bits-services `audit-log/`; on the same host for now, on another node later),
which keeps only what links onto its chain. Each pass it checks the service's head against its own event of that
seq; a difference (a rewritten database, or one restored from an older copy)
stops the sending, is recorded as `outside.diverged` and shown by `GET
/ops/outside`. `python -m console_backend.orch.outside verify` compares the two
copies event by event; `status` and `clear --by <user>` handle the alarm.
