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
`GET /ops/worker` tells admins when the worker last passed (its heartbeat file)
and which of its steps fail since when, from those events; the errors only to
overall admins.

Each pass first **reconciles** builds (`orch/reconcile.py`), trusting GitLab and
S3 rather than what jobs report: it lists the build project's pipelines updated
since the last pass (`BITS_ORCH_LOOKBACK_DAYS`, default 7, on an empty
database), reads a new pipeline's variables once, decides each build pipeline's
outcome from its CI jobs (bits' progress status can hold a pipeline "running" or
fail it late), and reads a pipeline's builds from the S3 build index (written
once a build's tarballs and BOMs are in the store, while the job may still be
publishing to CVMFS) in
`BITS_ORCH_STORE` (`b3://<bucket>`). GitLab is read with `BITS_FORGE_READ_TOKEN`
(read_api) when set. `GET /ops/builds` and `GET /ops/builds/{pipeline}` serve
what it recorded: to admins for their communities, and to any signed-in user,
read-only, for the community named (`?community=`; without who approved a
certification, a build without its variables and events). A build job may
send a hint (`POST /ops/ci/published|failed {pipeline}`) with a CI ID token for
the audience `BITS_ORCH_CI_AUDIENCE` (default `bits-orchestrator`), from the
build project on a protected ref; the worker then looks at that pipeline on
its next pass.

**The build queue** (`orch/build_queue.py`, with `BITS_ORCH_QUEUE=1`): the
console queues its builds (`POST /ops/queue {community, ref?, builds[]}`, each
checked as `/ops/build` checks one) instead of starting them, and the worker
starts them, oldest first, at most one per build host, so a long build never
waits in GitLab (where its pending jobs count against the project's job limit)
nor shares a host. The hosts are the build project's runners in GitLab (its own
and its groups', not the instance's shared ones): one tagged `bits-host-<name>` is
host `<name>`, of the architecture of its `bits-build-<arch>` tag (none: it takes
only builds pinned to it). Their tags are read from each runner's details
(`GET /runners/:id`) every 5 minutes, which the ops token must be allowed to see
(a Maintainer of the project, or of the group for a group runner); a runner it
cannot see is left out and logged. While GitLab cannot be read, the hosts it last
gave are kept. A build whose host or architecture is not among them waits an hour
(a runner registered again comes back), then is refused. A build pinned to
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
builds (to whom `/ops/builds` shows them; who queued them, to admins), the hosts
(to admins) and the pause (GitLab refusing pipelines: why, until when), `POST /ops/queue/{id}/cancel` takes one out before it
starts. A Retry of a pipeline restarts it at once, outside the queue. With
`certify_groups` the answer carries a passkey challenge over the builds that
certify (by digest of ref, name and variables); `POST /ops/queue/approve
{request_id, assertion}` records it as the batch's standing approval (one run
per build, 7 days), and each such build, as it starts, takes a run that becomes
the console pre-approval of its pipeline (24 hours), which the worker's
certification needs. Without a passkey the builds are queued all the same
(`approval_error` says why). Revoking the batch's approval revokes the
pre-approvals it gave.

**Build hosts** (`orch/hosts.py`): an overall admin acts on a host as an
action of no community: `host.pause` and `host.resume` set `paused` on the
runners tagged `bits-host-<name>` the build project uses, not the instance's
shared ones (`PUT /runners/:id`, as the ops token's Maintainer; a group's runner
is paused for the whole group), and `host.refresh` starts the builder-image refresh
pipeline pinned to the host (`REFRESH_IMAGES_JOB`). `host.prune {host,
min_free_gb}` starts bits-console's `prune-host` job there (`HOST_PRUNE_JOB`,
`BITS_PRUNE_MIN_FREE_GB`): `bits prune --disk-pressure-only`, architecture by
architecture, until sw has that much free. Such a pipeline starts when the
host's runner takes it: not while it is paused. The app asks for the free space
saved for the host (`PUT /ops/hosts/{host}/prune-target {min_free_gb}`, overall
admins; 200 GiB when none is; `GET /ops/hosts/prune-targets`, admins).

**Phases** (`orch/phases.py`): for each build pipeline finished in the last week,
what its build job spent its time on, for the console's timeline: queued (from
the build queue's request, else the pipeline's creation), setup, build, and per
package prepub's work and the gateway's commit. They come from the job's
`bits-phases.txt` and prepub's measurement records (`measurements-<pipeline>.json`),
both kept as job artifacts, else from the job's own times; a retried pipeline is
looked at again. A failed job's `failed_in` says where it stopped, from the
marks of the steps it passed (`build_ok`, `publish_start`, `finish_start`):
`build`, `certify` (after the build, before the publish), `publish`, `finish`
(after the publish step), or `''` (not known). Without the `build_ok` mark (an
older bits-console) every failure reads `build`. `GET /ops/builds` returns them
as `phases: {host, segments, failed_in}`; `host` is the build host a pinned
build ran on (`BITS_RUNNER_TAG=bits-host-<name>`), else its runner's description.

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
`/ops/signatures` serve the record, limited to the caller's communities
(`/ops/failures` also to any signed-in user for the community named; an
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
as soon as the build is in the store (its pipeline running or succeeded), not
after its CVMFS publish; a certification stands if that publish fails later,
since the store content it vouches for is unchanged. Only the build's console pre-approval approves it: the
worker requests a `certify` action already approved by it, and nobody can
approve one in the console. It runs on a thread of its own: the build's BOMs
from the store, the certified set at its latest version as the baseline, bits'
merge and store check, and per architecture the sign path's checks (the
approver an admin of every group, the key policy, none being a refusal). The
bytes are kept before they are signed, so a rerun signs the same bytes again;
signatures are verified, and written through the store writer
(`BITS_STORE_WRITER_AGENT_SOCKET`, a security-proxy instance only the worker
reaches), at most `BITS_ORCH_CERTIFY_DAILY_SIGNS` a day. Manifests are valid
`BITS_ORCH_CERTIFY_VALID_DAYS` (30). `GET /ops/certify` (overall admins: with
the reasons, and why a build still signing is retried; community admins: their
pipelines' builds) and `/ops/certifications/summary` say whether the worker owns
the certified set (`owns_set`).

**The canary** (`orch/canary.py`, M8a until M8b): the worker writes its
manifests under `CANARY/` (`BITS_ORCH_CANARY_PREFIX`; production reads
`MANIFESTS/`), and each is compared with the CI's certification of the same
build (`match`, `mismatch`, or `rebased` when the set was at another commit;
`unmatched` and `missing` for one side alone): `canary` in
`/ops/certifications/summary` and on the rows of `GET /ops/certify`. M8b
deletes it.

**The worker owns the certified set** (M8b, `BITS_ORCH_OWNS_SET=1`; unset it
to go back to M8a): the set is no longer read from bits-manifests but kept by
the worker, which writes the production manifests (`BITS_ORCH_MANIFEST_PREFIX`,
default `MANIFESTS/common-manifest`) and is the only signer; the backend's
`/sign` routes are gone. Each architecture's manifests are numbered
(`sequence`, after the one in the store, which is never overwritten by a lower
number), so build nodes refuse an older one put back. `orch/mirror.py` commits
every version of the set to bits-manifests (`BITS_FORGE_MIRROR_TOKEN`, or the
credentials proxy's `gitlab-mirror` route), and `MANIFESTS/orchestrator.json`
in the store tells bits clients that the worker owns the set (`bits certify`
then stops after the pre-approval and `bits sign` refuses the store). A
community admin takes builds out of the set with `decertify` (the requesting
group's BOMs of a build, every architecture or one; other groups that certify
it are named in the result) and `decertify-package` (each of a group's BOMs
replaced by itself without the package; `bits compliance --enforce` asks for
these). The worker then requests `withdraw` itself: an architecture's manifest
re-signed with fewer entries, never more. A decision and its architectures are one
transaction; a certification overtaken by a withdrawal is made again; a
half-written manifest is repaired on the next pass (written in full: recorded;
a withdrawal's: its signature put back; a certification's: the previous
manifest back and a re-sign from the set). Every signature is an event, and the
daily limit counts them. With an outside copy (`BITS_ORCH_AUDIT_URL`), the
worker signs only while it is in step (checked within
`BITS_ORCH_SIGN_OUTSIDE_MINUTES`, 10); `python -m
console_backend.orch.outside recover --by <you> --mirror <bits-manifests checkout>`, with
the worker stopped, brings a restored database up to the outside copy's events.

**The CVMFS status document** (`orch/cvmfs_status.py`, with
`BITS_ORCH_CVMFS_STATUS=1`): the worker writes the console's `cvmfs-status.json`
(the build project's generic package registry), one record per CVMFS publish of
a successful pipeline, as the per-build CI job did. It is the only writer.

**SBOM scans** (`orch/sbom_scan.py`, with `BITS_ORCH_SCANNER_URL`, the
bits-services sbom-scanner): each published build's `sbom.cdx.json` is matched
with grype, again whenever the scanner's vulnerability database is newer than
that of the build's last good scan, and when a newer pipeline publishes the
build. The counts by severity are kept in the database, the vulnerabilities
beside it (`sbom-scans/`, rebuilt by scanning again). A scan also indexes the
SBOM: which builds contain a package, what matched, and how many components a
scanner can identify (a CPE, or a purl of an ecosystem it knows). Findings of
components the stack does not ship (host tools, what only they use, system
packages only the build needs) are left out of the counts. `GET
/ops/builds/{pipeline}/sbom?format=cyclonedx|spdx` serves a build's SBOM as
published to any signed-in user, with its findings only for the admins of its
community; `GET /ops/sbom/scans`, `/ops/sbom/packages?name=`, `/ops/sbom/names`
and `/ops/sbom/vulnerabilities/{id}` serve the admins' communities.

**Risk over time** (`orch/sbom_history.py`): once a day the worker records each
release's open findings by severity and their risk (grype's), from its newest
scanned build; a release is a community's top-level package, whose compiler and
build-type variants share their sources. A day with a build but no record
(before the first record) gets that build's counts as they are when recorded. `GET /ops/sbom/history?community=&days=`
serves it with today's point computed afresh (the Vulnerabilities page's chart).

**The help chat** (`orch/chat.py`, `orch/chat_docs.py`, with `BITS_CHAT_URL`, the
address of Ollama on this host, e.g. `http://127.0.0.1:11434`, or from a container
`http://host.docker.internal:11434` (bits-services' DEPLOYMENT.md), and `BITS_CHAT_MODEL`,
default `gemma4:12b`): any signed-in user asks how to use bits (`POST /ops/chat
{question, community?, history?}`) or why a build failed (`{pipeline}`), and gets the
answer as server-sent events: the documentation sections used (`sources`), a reviewed
cause when a signature matched (`cause`), the text, then `done` or `error`. The model
gets no tools. The backend sends it the best docs sections and, for a build, what the
orchestrator recorded of it (outcome, where it stopped, failed jobs, the redacted
excerpts, the issue), how it differs from the last successful build of the same package
and platform (version, CI commit, build host, followed heads and, for admins, its build
variables), and for a failed build the end of its job logs and of `bits-build.log` or
`bits-publish.log`, read from GitLab when asked (kept 10 minutes) and redacted, all
marked as data never to be followed. The worker's `chat_docs` step
reads the docs (`BITS_CHAT_DOCS`: a GitHub repository `owner/repo` or file
`owner/repo/path`, a GitLab project `https://host/group/project` or a raw `…/file.md`;
default every markdown file of the bits repositories on GitHub, each on its default
branch, and the site's `console.docs`, at CERN bits-console and bits-manifests) every
`BITS_CHAT_DOCS_REFRESH_S` (3600), when they changed, into a full-text index
(SQLite FTS5) in this database. Limits: one question at a time per user,
`BITS_CHAT_PER_MINUTE` (6), `BITS_CHAT_PARALLEL` (2) answers at once and
`BITS_CHAT_QUEUE` (4) waiting; per answer `BITS_CHAT_CONTEXT` (16384 tokens, best set
as Ollama's `OLLAMA_CONTEXT_LENGTH` too), `BITS_CHAT_ANSWER_TOKENS` (1500) and
`BITS_CHAT_TIMEOUT_S` (90); a thinking model thinks first only to explain a failure
(`BITS_CHAT_EXPLAIN_THINK`, on, when the model can and the context is 8192 or more;
twice the time) or with `BITS_CHAT_THINK=1`. Nothing of a chat is kept except a vote (`POST
/ops/chat/feedback`), with its question, answer and sources, for 90 days (migration 24;
`GET /ops/chat/feedback` for overall admins; 50 a user a day; the worker's `chat_votes`
step expires them, chat on or off). `GET /ops/chat` says whether it is on and whether
Ollama has the model. `chat-eval/run.py` measures the search on `chat-eval/cases.yaml` and,
with `--model` (and `--judge`), the answers.

**Nightly schedules** (`orch/schedules.py`, ADR bits 0007): a schedule builds
one package at one version, with one defaults chain and recipe repositories, on
several platforms, on given weekdays at a time of the bits instance's time zone
(site `console.time_zone`; UTC, at CERN Europe/Zurich). Its `variants` (ADR bits
0009) are defaults chains put before its own: a night builds every cell of
platforms × variants (`gcc15::opt` with `dev3` builds `gcc15::opt::dev3`), at
most 64, and a run is one cell's (`schedule_runs.variant`; migration 22). A time skipped in spring
runs at the first minute after it, one repeated in autumn once. Its
`content_sha256` covers what is built and where it goes, not when or whether, so
a standing approval bound to `schedule:<id>@<sha256>` outlives a new time but not
a new platform or variant (a schedule without variants keeps the hash it had). `GET /ops/schedules?community=` lists them, with their next run,
to any signed-in user (the approval in force to admins); a community's admins
create, change (`PUT` with the `revision` it changes; 409 when another change came
first), pause, resume and delete them, each change an event with before and after.
With the build queue on, the worker's `schedules` step (`orch/nightlies.py`) fires
each schedule whose time has come since it last fired: one queued build per
platform, with the variables the GitLab fan-out sends, resolved from the
console's `config/platforms.yaml`, `config/monitoring.yaml` and the community's
`ui-config.yaml`, and one `schedule_runs` row each. Its defaults are the
schedule's with the community's `defaults_overlay` last, as the console's Build
dialog sends them, and go as `DEFAULTS` too, with which the build job resolves its
CVMFS target; `--defaults` in the extra arguments is refused. Its night's weekday
goes as `--day` (the local date of the scheduled time; for Run now, today's), so a
nightly layout's `{day}` is the night's whenever the queue starts it; it wins over
a `day:` in the defaults. A night missed while the
worker was down fires on its return within 6 hours, older ones are `not_run`; a
platform whose previous night still waits or builds `skipped`. A certifying
schedule's builds certify as the approver of its standing approval in force (its
pipeline's pre-approval takes one run when it starts); without one they build and
certify nothing ("built, not certified: no standing approval").
`/approvals/standing/request` takes `schedule:<id>` with the schedule's
`content_sha256` and `certify_groups` as they are now. The `nightly_results` step
keeps each run up to date from its queued build and pipeline: `running`, `passed`,
`failed` (where: the failed job's package and job), `not_run` (canceled, refused,
or no free build host 12 hours after it fired: then taken out of the queue); a
failed run's pipeline is looked at again for 3 days (a retry in GitLab).
`GET /ops/nightlies?community=&nights=` is the matrix (a row per schedule, with its
cells; a cell per platform (and variant) and night, its latest attempt; to whom `/ops/builds` shows builds);
**Commit CI** (`orch/watches.py`, ADR bits 0009; migration 23): a schedule may
follow branches (`watch`, `url@branch;…`, at most 8; not in its content hash: it
says when, not what). Every `BITS_ORCH_WATCH_S` (120 s, at least 30) the worker's
`watches` step reads each branch's head with git's protocol v2 over HTTPS
(`ls-refs` on `<url>[.git]/git-upload-pack`: no git binary, no API quota; public
repositories on public addresses, no redirects, no credentials, 64 KiB and 15 s
at most an answer). A branch first seen, or seen again after it was gone, is not
built. Once a head has moved and every moved head has held still for 5 minutes
(or the first has waited 30), and none of the schedule's commit runs is queued or
running, it fires a commit run of every cell (`fired_by` `commit`, `commits` the
heads when it fired, JSON; each build reads its branches when it starts): nothing
is cancelled, and commits seen meanwhile are built together. Nightlies and commit
runs do not hold each other. A commit run certifies nothing and publishes nothing
to CVMFS. `GET /ops/schedules` shows the heads as `watching`; the matrix keeps
commit runs out of the nightly cells and gives a schedule's latest 10 fires (of 30
days) as `commits` rows.
`POST /ops/schedules/{id}/run {platforms?, cells?}`, by its community's admins, is
Run now (every cell) or Run again (the cells of some platforms, or some cells by
their matrix name, `<platform> <variant>`), today's night, each cell's next attempt.

**Triage** (`orch/vuln_triage.py`): a community admin's rule says a
vulnerability does not matter there, for one version of a package, every
version, every package, or every vulnerability of a package (`vuln_id` `*`):
`not_affected` (with a CycloneDX justification), `false_positive` or
`will_not_fix`, with a reason and an optional expiry. Covered findings leave the
effective counts and are served in the SBOM with the rule as their `analysis`.
`GET|POST /ops/sbom/triage`, `POST /ops/sbom/triage/{id}/revoke`; adding and
revoking are events.

**Upgrade plans and licences** (`orch/sbom_plan.py`, `orch/sbom_licences.py`),
from the scans' index, per release line (a community's build of one release on
one platform, its newest build): `GET /ops/sbom/plan` gives the versions that
fix the open findings in waves (known exploited, within the major version,
major; system packages apart), ordered by the risk removed per package rebuilt,
and one version per package converged across lines, with pinned versions as
exceptions. `GET /ops/sbom/licences[/{build_id}]` gives licence conflicts along
runtime paths and unknown licences (accepted as triage rules on
`licence:<kind>`), copyleft reach and restricted packages;
`/ops/sbom/dependencies[/{build_id}]` the most depended-on, build-only and
unneeded packages, and what changed since the line's previous build.

**Credentials** (`credproxy.py`, M6): with `BITS_CRED_PROXY_AGENT_SOCKET`, the
backend and the worker hold no GitLab token or store key. They call GitLab and
the store through the credentials proxy (bits-services `credentials-proxy`, a
second security-proxy instance), sending its gate token where the real token
went (`PRIVATE-TOKEN`, or the S3 access key); the proxy adds the real one on the
way out. Routes: `gitlab-ops`, `gitlab-read`, `gitlab-groups`, `gitlab-mirror`, `s3`. Without
the socket, `BITS_FORGE_OPS_TOKEN`, `BITS_FORGE_READ_TOKEN`,
`BITS_ADMIN_RESOLVE_TOKEN` and the S3 keys are used as before. `BITS_ORCH_STORE_ANONYMOUS=1` reads a public
store with unsigned requests (at `BITS_ORCH_S3_ENDPOINT`, https).

**The outside copy** (`orch/outside.py`, M4): with `BITS_ORCH_AUDIT_URL` and
`BITS_ORCH_AUDIT_TOKEN`, the worker sends the event log to the audit-log service
(bits-services `audit-log/`; on the same host for now, on another node later),
which keeps only what links onto its chain. Each pass it checks the service's head against its own event of that
seq; a difference (a rewritten database, or one restored from an older copy)
stops the sending, is recorded as `outside.diverged` and shown by `GET
/ops/outside`. `python -m console_backend.orch.outside verify` compares the two
copies event by event; `status` and `clear --by <user>` handle the alarm.
