# ADR-0003: S3 upload authorization and trusted reuse

Status: **Proposed / under discussion** — no implementation decision yet.
Date: 2026-07-03

## Context

The S3 store is the reuse tier below CVMFS: content-addressed tarballs that any
build can pull instead of recompiling. Two problems need a design:

1. **Upload authorization** — who may write to the store, without putting a
   long-lived S3 secret on developer laptops.
2. **Trusted reuse** — how a consumer knows a recalled tarball is authentic, not
   a swap by whoever can write the bucket.

The client-side trust primitives are already built and committed, and are
independent of whichever server-side option we pick:

- `bits_helpers/trust.py` — Ed25519 manifest sign/verify, `trusted_index()`.
- `bits build` flags `--sign-manifest`, `--trust-manifest`,
  `--require-signed-reuse` (all opt-in, default off).
- `bits login` + `bits_helpers/auth.py` — personal bits-token in
  `~/.bits/config` (600) → short-lived scoped creds in `~/.bits/session` (600);
  CI-injected `AWS_*` env always wins. Session token threads through
  `configure_s3_env` and the boto3 client.

## Constraints (established during discussion)

- **Testbed is pure Docker, outbound-only** — no inbound connectivity, no
  bastion host, **not** Kubernetes. This rules out GitLab Agent / KAS (which is
  k8s-only). Any server component must establish the connection outbound.
- **S3 (Ceph RGW) scoping unknown** — unsure whether RGW can issue per-user
  credentials scoped by prefix, or STS `AssumeRoleWithWebIdentity` with GitLab
  as OIDC provider. **This is the highest-leverage open question**: if either
  exists, the data-plane broker collapses and laptops can get real short-lived
  scoped credentials directly.
- Assume for now a **single shared S3 write credential** → it must never touch a
  laptop.
- Wants: **ad-hoc** signing/publish (not coupled to git-push pipelines) and
  **direct-from-laptop** uploads. Central signing key must not leak.

Hard networking truth: an outbound-only container with no bastion **cannot**
serve synchronous requests. Any solution is asynchronous, relaying over a
rendezvous both sides reach outbound — only **GitLab** or **S3** qualify here.

## Options

### A. GitLab Runner (pipeline) as signer/broker

Signing/publish is a CI job. Central Ed25519 key + shared S3 credential live as
Protected + Masked CI/CD variables. A `build` job leaves the tarball in the
persistent workDir; a `sign-publish` job (holding the secrets) verifies
`tarball_sha256`, countersigns the manifest, uploads to S3.

- **Runners**: reuse existing runners for builds; add **one dedicated,
  protected, tagged runner for signing** so the key is never exposed to jobs
  that also run untrusted MR/fork code. That signing runner **can be a GitLab
  Runner registered from the testbed container** — runners poll GitLab
  *outbound*, so pure-Docker/outbound-only works (this is the non-k8s analog of
  KAS).
- **Busy slots**: jobs queue as `pending` until a slot frees (up to timeout),
  they don't fail. A dedicated fast signing runner avoids head-of-line blocking
  behind long builds. No true priority queue in CE; tag-routing is the
  equivalent.
- **Ad-hoc**: trigger via the pipeline API (PAT), not only git push; `bits`
  polls the job for the `.sig`.
- **Laptop-origin artifacts**: build on runner (rebuild, heavy) or push the
  tarball to GitLab's Generic Package Registry with the PAT (authenticated write
  the laptop already has) and let the sign job pull it → laptop→GitLab→S3 double
  hop.
- Pros: off-the-shelf worker, GitLab owns identity + queue + secret gating.
  Cons: pipeline-shaped even when API-triggered; double hop for laptop artifacts.

### B. S3-rendezvous broker (no runner, no pipeline)

Broker = pure Docker, outbound-only, holds shared S3 credential + central key.
Loops over an S3 control prefix. Data plane uses **presigned PUT URLs** (one per
content-addressed key; computed offline from the shared secret) so the laptop
uploads bytes directly to S3 with no credential on it.

Flow: laptop writes a **dev-key-signed** request (+ ephemeral X25519 pubkey) to
`ctrl/requests/<uuid>` (write-only, auto-expiring prefix, or a shipped
write-only-to-requests token) → broker verifies dev-key signature (identity) and
area authorization (key→area policy or outbound e-group check), mints presigned
PUT URLs, **seals** the response to the ephemeral key, writes
`ctrl/responses/<uuid>` → laptop uploads tarballs directly → broker re-verifies
`tarball_sha256`, promotes/copies to the common area if needed, countersigns,
writes `.sig`.

- Trust bootstrap: developer public keys committed in git; broker loads them.
  No secret ever sits in S3.
- Pros: meets every constraint — pure-Docker outbound-only, single shared cred
  never leaves broker, central key in *your* container, ad-hoc, pipeline-free,
  genuinely direct-from-laptop. Cons: bespoke daemon you maintain; control
  bucket needs an anonymous/expiring request prefix + sealed responses.

### C. Per-area split (foundation, blocked on RGW capability)

- Personal area: laptop uploads with its **own** backend-scoped credential
  (scoped write = proof of identity) and signs its own manifests with its own
  registered key. No central worker. **Requires RGW per-user scoped creds.**
- Common/shared area: promotion through A or B; central key countersigns.

Collapses most of A/B if RGW supports per-user scoping or STS/OIDC.

## Decision

Deferred. User is evaluating A vs B (leaning against pipeline coupling, which
favors B). Blocked on confirming RGW STS/OIDC or per-user scoped-credential
support with the storage team.

## Interim posture (in effect now)

Until a mechanism is chosen, the store runs open and permissive:

- **Upload**: allowed for anyone holding valid AWS/S3 keys (existing boto3
  behavior). No console broker, no scoped creds required.
- **Download**: open to everyone (public read store).
- **Reuse**: manifests are **trusted even if unsigned** — signing and
  verification (`--sign-manifest`, `--trust-manifest`,
  `--require-signed-reuse`) are opt-in and off by default, so reuse works
  without any signature.

## Open questions to resolve before implementation

1. Does CERN RGW support STS `AssumeRoleWithWebIdentity` (GitLab OIDC) or
   per-user credentials scoped by prefix? (Decides whether C is viable and
   whether the data-plane broker is even needed.)
2. Option A or B? (Driven by the pipeline-coupling vs maintain-a-daemon
   tradeoff.)
3. Where do developer public keys live and how are they registered per area?
