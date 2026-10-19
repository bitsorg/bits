# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker certifies builds (M8a on): it signs their manifests and writes
them to the store.

For every build that asked for certification (CERTIFY=true) and has a console
pre-approval, the worker certifies it on its own terms, minutes after the
build, signs the result through the signer proxy and writes it through the
store writer. Until M8b it writes under the canary prefix, which production
does not read (CANARY/common-manifest-<arch>.json and .sig), and canary.py
compares it with the bits-manifests CI's certification of the same build.

  * Trigger (`trigger`, a step of the worker's loop): a build recorded after
    certification was turned on, of a pipeline that succeeded with
    CERTIFY=true, certified into bits-manifests (the community's
    certify_manifests_remote). Its console pre-approval in force now approves
    a `certify` action, which the worker requests already approved
    (actions.request_preapproved): nobody approves a certification in the
    console. Without one, the build waits until it was recorded a day ago,
    then is refused.
  * Run (`run`, on the certifier thread, so hashing never delays cancel and
    retry): the approval checked again (this action's, this pipeline's, in
    force); the build's BOMs from its build index; the certified set at its
    latest version as the baseline, the build's BOMs at the paths the CI's
    merge request uses (or at their place in the set, when it holds them
    already); bits' own merge and store check (`certify.build_by_arch`); per
    architecture the checks of the sign path (/sign/preapproved): the approver
    an admin of every group, the signing key allowed for every group by the
    key policy (none is a refusal). The bytes are kept before they are signed:
    a rerun signs the same bytes again (Ed25519 is deterministic), so there is
    one signature per pipeline, build and architecture. Each signature is
    verified against the trust anchor, by the key the policy was checked for,
    before it is written, through the store writer (a security-proxy instance
    only the worker reaches).
"""

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import urlsplit

from . import actions, certified, db, events, outside, replay, shadow

log = logging.getLogger("bits_orchestrator.certifier")
KIND = "certify"
# A build of no pipeline (made with the CLI, certified by its CLI pre-approval)
# and a withdrawal have pipeline 0 in certify_runs; a withdrawal's build is
# "set@<version>", which no bits build id can be.
NO_PIPELINE = 0
WITHDRAWAL_PREFIX = "set@"


def _is_withdrawal(row):
    return row["pipeline_id"] == NO_PIPELINE and str(row["build_id"]).startswith(WITHDRAWAL_PREFIX)
# Re-signing an architecture's manifest without the BOMs the certified set has
# removed: it only takes trust away, so it needs no pre-approval.
WITHDRAW = "withdraw"
# Taking a build's BOMs out of the set the worker owns (M8b); the withdrawals
# then re-sign the manifests without them.
DECERTIFY = "decertify"
DECERTIFY_PACKAGE = "decertify-package"
DEFAULT_REMOTE = "https://gitlab.cern.ch/buncic/bits-manifests"
WAIT_MS = 24 * 3600 * 1000            # how long a build waits for its pre-approval
PER_PASS = 20
WITHDRAW_TRIES = 5                    # withdraw actions per set version, each with its retries
DAY_MS = 24 * 3600 * 1000


class Refused(Exception):
    """The worker will not certify this build (the reason is the message)."""


# --- the rows ------------------------------------------------------------------

def _overtaken(row):
    """A run whose manifest a newer one overtook before it was written: it is
    'signing' without one, to be made again."""
    return row["state"] == "signing" and row["manifest"] is None


def _upsert(conn, pid, bid, arch, state, reason="", **fields):
    now = db.now_ms()
    cols = dict(fields, state=state, reason=reason[:1000], updated_ms=now)
    sets = ", ".join("%s = :%s" % (k, k) for k in cols)
    names = ", ".join(cols)
    conn.execute(
        "INSERT INTO certify_runs (pipeline_id, build_id, arch, created_ms, %s)"
        " VALUES (:pid, :bid, :arch, :now, %s) ON CONFLICT(pipeline_id, build_id, arch)"
        " DO UPDATE SET %s" % (names, ", ".join(":" + k for k in cols), sets),
        dict(cols, pid=int(pid), bid=bid, arch=arch, now=now))


def _decide(conn, pid, bid, state, reason, event=True, archs=False, **fields):
    """The whole build's decision (arch '*'), with an event. With *archs*, its
    architectures still 'signing' end in the same state, in the same transaction:
    one left 'signing' would hold back every withdrawal of its architecture."""
    with db.transaction(conn):
        _upsert(conn, pid, bid, "*", state, reason, **fields)
        if archs:
            conn.execute("UPDATE certify_runs SET state = ?, manifest = NULL, reason = ?,"
                         " updated_ms = ? WHERE pipeline_id = ? AND build_id = ? AND arch != '*'"
                         " AND state = 'signing'", (state, reason[:1000], db.now_ms(), pid, bid))
        if event:
            events.append(conn, "certify." + state, "pipeline:%d" % int(pid), events.WORKER,
                          {"pipeline": int(pid), "build_id": bid,
                           **({"reason": reason[:500]} if reason else {})})


def signed_today(conn, now=None):
    """Signatures the worker made in the last day: each is an event (a run made
    again signs again), and the runs' own count covers those made before."""
    since = (now or db.now_ms()) - DAY_MS
    made = conn.execute("SELECT COUNT(*) FROM events WHERE subject = 'signatures' AND"
                        " ts_ms > ?", (since,)).fetchone()[0]
    runs = conn.execute("SELECT COUNT(*) FROM certify_runs WHERE signed_ms > ?",
                        (since,)).fetchone()[0]
    return max(made, runs)


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _name(value, what):
    """An architecture or group, as it goes into a file name and a store key:
    a plain name, never a path."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise Refused("%s %r is not a plain name" % (what, value))
    return value


def write_prefix(settings):
    """Where the worker writes its manifests: the production manifests' place
    once it owns the set (M8b); before, the canary prefix, which must stay out
    of it. Plain names either way."""
    if settings.orch_owns_set:
        mp = settings.orch_manifest_prefix.strip("/")
        if not mp or not all(_NAME.fullmatch(x) and ".." not in x for x in mp.split("/")):
            raise RuntimeError("BITS_ORCH_MANIFEST_PREFIX %r must be plain names" % mp)
        return mp
    cp = settings.orch_canary_prefix.strip("/")
    top = settings.orch_manifest_prefix.strip("/").split("/")[0]
    parts = cp.split("/")
    if (not cp or parts[0] in (top, "MANIFESTS", "TARS", "BUILDS")
            or not all(_NAME.fullmatch(x) and ".." not in x for x in parts)):
        raise RuntimeError("BITS_ORCH_CANARY_PREFIX %r must be plain names, not under %s/"
                           % (cp, top))
    return cp


def _valid_days(settings):
    """The manifests' expiry in days. Kept once a sequence orders them too
    (M8b): a node that remembers no sequence (new, or its state lost) is
    bounded by it; the worker refreshes each manifest well before (refreshes)."""
    return settings.orch_certify_valid_days or None


def _chain_position(conn):
    """The audit chain's head: which state of it a manifest was made at."""
    r = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return {"seq": r["seq"], "hash": r["hash"]} if r else {"seq": 0, "hash": events.GENESIS}


def _project_of(remote):
    path = urlsplit(remote).path.strip("/")
    return path[:-4] if path.endswith(".git") else path


# --- the trigger (the worker's loop) ---------------------------------------------

def trigger(conn, settings, console):
    """Request a certification for each new build that asked for one. Returns
    how many were requested. A build waiting for a pre-approval is looked at
    again only once one is in force (or its wait is over), without reading
    GitLab before that."""
    if not settings.orch_certify:
        return 0
    write_prefix(settings)
    r = conn.execute("SELECT value FROM kv WHERE key = 'certify.since'").fetchone()
    if r is None:
        with db.transaction(conn):
            conn.execute("INSERT INTO kv (key, value) VALUES ('certify.since', ?)",
                         (str(db.now_ms()),))
        return 0
    since, now = int(r["value"]), db.now_ms()
    base = ("SELECT b.pipeline_id, b.build_id, b.recorded_ms, c.state AS decided, p.*"
            " FROM builds b JOIN pipelines p ON p.id = b.pipeline_id LEFT JOIN certify_runs c ON"
            " c.pipeline_id = b.pipeline_id AND c.build_id = b.build_id AND c.arch = '*'"
            " WHERE p.outcome = 'success' AND b.recorded_ms >= ? AND ")
    rows = conn.execute(base + "c.state IS NULL ORDER BY b.recorded_ms LIMIT ?",
                        (since, PER_PASS)).fetchall()
    rows += conn.execute(
        base + "c.state = 'waiting' AND (b.recorded_ms < ? OR EXISTS (SELECT 1 FROM approvals a"
        " WHERE a.kind = 'preapproval' AND a.via = 'console' AND a.subject ="
        " CAST(b.pipeline_id AS TEXT) AND a.created_ms <= ? AND a.expires_ms > ? AND"
        " (a.revoked_ms IS NULL OR a.revoked_ms > ?))) ORDER BY b.recorded_ms LIMIT ?",
        (since, now - WAIT_MS, now, now, now, PER_PASS)).fetchall()
    from .. import forge_ops
    n = 0
    for a in conn.execute(
            "SELECT * FROM approvals a WHERE a.kind = 'preapproval' AND a.via = 'cli' AND"
            " a.revoked_ms IS NULL AND a.expires_ms > ? AND a.created_ms >= ? AND NOT EXISTS"
            " (SELECT 1 FROM certify_runs c WHERE c.pipeline_id = ? AND c.build_id = a.subject"
            " AND c.arch = '*' AND (c.approval_id >= a.id OR c.state NOT IN ('refused',"
            " 'failed'))) ORDER BY a.id LIMIT ?",
            (now, since, NO_PIPELINE, PER_PASS)).fetchall():
        try:
            n += _consider_cli(conn, settings, a)
        except RuntimeError:
            raise                          # the daily limit: the step fails, visibly
        except Exception as e:
            if shadow.transient(e):
                raise
            log.warning("certify: CLI build %s: %s", a["subject"], e)
            _decide(conn, NO_PIPELINE, a["subject"], "refused", "%s: %s"
                    % (type(e).__name__, str(e)[:500]), approval_id=a["id"])
    for r in rows:
        try:
            n += _consider(conn, settings, console, r)
        except RuntimeError:
            raise                          # the daily limit: the step fails, visibly
        except forge_ops.ForgeError as e:
            if shadow.transient(e) or e.status in (401, 403):
                raise                      # GitLab or its token, not this build
            _hold(conn, r, e)
        except Exception as e:
            if shadow.transient(e):
                raise
            _hold(conn, r, e)
    return n


def _hold(conn, r, e):
    """A build that could not be looked at: it waits (looked at again while its
    pre-approval is in force), refused only once its wait is over."""
    why = "%s: %s" % (type(e).__name__, str(e)[:500])
    log.warning("certify: build %s of pipeline %s: %s", r["build_id"], r["pipeline_id"], why)
    if db.now_ms() - r["recorded_ms"] > WAIT_MS:
        _decide(conn, r["pipeline_id"], r["build_id"], "refused", why)
    else:
        with db.transaction(conn):
            _upsert(conn, r["pipeline_id"], r["build_id"], "*", "waiting", why)


def _consider(conn, settings, console, r):
    pid, bid = r["pipeline_id"], r["build_id"]
    try:
        variables = json.loads(r["variables"] or "{}")
    except ValueError:
        variables = {}
    if str(variables.get("CERTIFY") or "").strip().lower() != "true":
        _decide(conn, pid, bid, "skipped", "certification not requested", event=False)
        return 0
    pre = replay.preapproval_at(conn, pid, db.now_ms())
    if pre is None:
        if db.now_ms() - r["recorded_ms"] > WAIT_MS:
            _decide(conn, pid, bid, "refused", "no console pre-approval of pipeline %s" % pid)
        elif r["decided"] is None:
            with db.transaction(conn):
                _upsert(conn, pid, bid, "*", "waiting", "for a pre-approval")
        return 0
    try:
        community, cfg = replay.community_config(console, r)
    except replay.Refuse as e:
        _decide(conn, pid, bid, "refused", str(e))
        return 0
    remote = str(cfg.get("certify_manifests_remote") or "").strip() or DEFAULT_REMOTE
    project = settings.orch_manifests_project
    # (A numeric project id cannot be compared with a remote's path.)
    if project and not project.isdigit() and _project_of(remote) != project.strip("/"):
        _decide(conn, pid, bid, "skipped", "certified into %s, not %s" % (remote, project))
        return 0
    if signed_today(conn) >= settings.orch_certify_daily_signs:
        log.warning("certify: %d signatures in a day: paused", settings.orch_certify_daily_signs)
        raise RuntimeError("the worker reached its limit of %d signatures a day; paused"
                           % settings.orch_certify_daily_signs)

    def signing(conn, _row):
        _upsert(conn, pid, bid, "*", "signing", "", approval_id=pre["id"],
                approver=pre["approver"])
    actions.request_preapproved(
        conn, KIND, {"pipeline": str(pid), "build_id": bid}, events.WORKER,
        str(r["community"] or community).lower(), pre, "certify:%d:%s" % (pid, bid),
        cause="pipeline:%d" % pid, also=signing)
    return 1


def _consider_cli(conn, settings, a):
    """A build of no pipeline whose CLI pre-approval is in force: certify it,
    into its one approved group."""
    bid = a["subject"]
    groups = json.loads(a["groups"] or "[]")
    if not re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{12}", bid or "") or len(groups) != 1:
        _decide(conn, NO_PIPELINE, bid, "refused", "a CLI pre-approval names one build and one"
                " group (%s, %s)" % (bid, ", ".join(groups)), approval_id=a["id"])
        return 0
    if signed_today(conn) >= settings.orch_certify_daily_signs:
        raise RuntimeError("the worker reached its limit of %d signatures a day; paused"
                           % settings.orch_certify_daily_signs)

    def signing(conn, _row):
        _upsert(conn, NO_PIPELINE, bid, "*", "signing", "", approval_id=a["id"],
                approver=a["approver"])
    actions.request_preapproved(
        conn, KIND, {"build_id": bid}, events.WORKER, groups[0], a,
        "certify:cli:%s:%d" % (bid, a["id"]), cause="build:%s" % bid, also=signing)
    return 1


# --- the run (the certifier thread) -----------------------------------------------

def _approval(conn, params):
    a = conn.execute("SELECT * FROM approvals WHERE id = ?",
                     (params.get("_approval") or 0,)).fetchone()
    now = db.now_ms()
    via, subject = (("console", str(params["pipeline"])) if params.get("pipeline")
                    else ("cli", params["build_id"]))
    what = "pipeline %s" % subject if via == "console" else "build %s" % subject
    if (a is None or a["kind"] != "preapproval" or a["via"] != via or a["subject"] != subject
            or a["approver"] != params.get("_approver")):
        raise Refused("the action's approval is not a %s pre-approval of %s by %s"
                      % (via, what, params.get("_approver")))
    if a["revoked_ms"] is not None or a["expires_ms"] <= now:
        raise Refused("the pre-approval of %s is no longer in force" % what)
    return a


def build_boms(conn, store, pid, bid):
    """{arch: (store key, BOM)}: the build's own BOMs, one per architecture,
    from the keys its build index names."""
    from bits_helpers import certify
    row = conn.execute("SELECT bom_keys FROM builds WHERE pipeline_id = ? AND build_id = ?",
                       (pid, bid)).fetchone()
    if row is None:
        raise Refused("build %s of pipeline %s is not recorded" % (bid, pid))
    s3, bucket = store
    out = {}
    for key in json.loads(row["bom_keys"] or "[]"):
        try:
            doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ValueError:
            raise Refused("store BOM %s is not JSON" % key)
        if not isinstance(doc, dict):
            raise Refused("store BOM %s is not an object" % key)
        try:
            arch = _name(certify.bom_architecture(doc) or "shared", "architecture")
        except certify.CertifyError as e:
            raise Refused("store BOM %s: %s" % (key, e))
        if str(doc.get("source_pipeline_id") or "") != str(pid):
            raise Refused("store BOM %s is of pipeline %s, not %s (a re-publish of another"
                          " build's BOMs)" % (key, doc.get("source_pipeline_id"), pid))
        if arch in out:
            raise Refused("two store BOMs of build %s for %s" % (bid, arch))
        out[arch] = (key, doc)
    if not out:
        raise Refused("build %s of pipeline %s has no BOM in the store" % (bid, pid))
    return out


def _cli_arch(doc):
    """A CLI BOM's architecture, read as the CLI pre-approval reads it."""
    from bits_helpers import certify
    arch = doc.get("effective_architecture")
    return arch if isinstance(arch, str) and arch else (certify.bom_architecture(doc) or "shared")


def cli_boms(store, bid, approval):
    """{arch: (store key, BOM)} of a build of no pipeline: the BOMs of one
    publish run under MANIFESTS/<build_id>/ (<run leaf>.<arch>.json; the
    compliance files beside them are not BOMs) that are exactly what its CLI
    pre-approval bound ((architecture, hash, tarball sha256) of every package).
    Defers while there is no such run (not uploaded yet); Refused otherwise."""
    from bits_helpers import certify
    s3, bucket = store
    group = (json.loads(approval["groups"] or "[]") or [""])[0]
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": "MANIFESTS/%s/" % bid}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            break
    runs = {}                                        # run leaf -> {arch: (key, BOM)}
    for key in sorted(keys):
        try:
            doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ValueError:
            continue
        if (not isinstance(doc, dict) or str(doc.get("build_id") or "") != bid
                or not isinstance(doc.get("packages"), list)):
            continue                                 # an SBOM, a notice: not a BOM
        try:
            arch = _name(_cli_arch(doc), "architecture")
        except (certify.CertifyError, Refused):
            continue
        leaf = key.rsplit("/", 1)[-1]
        if not leaf.endswith("." + arch + ".json"):
            continue
        run = runs.setdefault(leaf[:-len("." + arch + ".json")], {})
        if arch in run:
            raise Refused("two store BOMs of build %s for %s in one run" % (bid, arch))
        run[arch] = (key, doc)
    bound = {(a, h, sha) for a, h, sha, *_rest in json.loads(approval["packages"] or "[]")}
    for leaf in sorted(runs):
        got = {(arch, str(e.get("hash")), certify.norm_sha(e.get("tarball_sha256")))
               for arch, (_k, doc) in runs[leaf].items() for e in doc["packages"]
               if isinstance(e, dict)}
        if got != bound:
            continue
        for arch, (key, doc) in runs[leaf].items():
            other = sorted({e.get("group") for e in doc["packages"] if isinstance(e, dict)
                            and e.get("group") and e.get("group") != group})
            if other:
                raise Refused("store BOM %s puts packages in %s, not the approved %s"
                              % (key, ", ".join(other), group))
        return runs[leaf]
    raise actions.Defer("no publish run of build %s in the store is what was pre-approved"
                        " (yet)" % bid, 5 * 60 * 1000)


def _path(group, bid, arch, key):
    """Where the CI's merge request puts this BOM: manifests/<group>/<build_id>.
    <arch>.<run leaf> (bits certify), the run leaf the one of its store key."""
    leaf = key.rsplit("/", 1)[-1]
    tail = "." + arch + ".json"
    stem = leaf[:-len(tail)] if leaf.endswith(tail) else leaf[:-len(".json")]
    return "%s/%s.%s.%s.json" % (group, bid, arch, stem)


def issuer(store):
    """Whose numbering a manifest belongs to: this orchestrator's, for the store
    it writes (a testbed and a production store number on their own)."""
    return "bits-orchestrator/%s" % store[1]


def next_sequence(conn, settings, store, arch):
    """The number of the architecture's next manifest: one past the higher of
    the last this database gave (including runs never written) and the one in
    the store, so a restored database never gives a number again."""
    mine = conn.execute("SELECT MAX(sequence) FROM certify_runs WHERE arch = ?",
                        (arch,)).fetchone()[0] or 0
    return max(mine, published(settings, store, arch)[0]) + 1


def published(settings, store, arch):
    """(sequence, sha256) of the architecture's manifest in the store: 0 for none
    or an unnumbered one, the sha256 of its bytes (None for none). Raises when
    it cannot tell."""
    s3, bucket = store
    raw = None
    try:
        raw = s3.get_object(Bucket=bucket, Key="%s-%s.json" % (
            write_prefix(settings), arch))["Body"].read()
        doc = json.loads(raw)
    except ValueError:
        # Retried (and seen as a failing step), not a refusal of the build.
        raise RuntimeError("the store's %s manifest is not JSON: no number until it is"
                           " replaced" % arch)
    except Exception as e:
        if not shadow._missing(e):
            raise                     # cannot tell: no number rather than a reused one
        doc = {}
    seq = doc.get("sequence") if isinstance(doc, dict) else None
    return (seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
            hashlib.sha256(raw).hexdigest() if raw is not None else None)


def _decertified(path):
    """The packages (lowercase) a decertify-package took out of the set's BOM at *path*."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return set()
    names = doc.get("decertified_packages") if isinstance(doc, dict) else None
    return {str(n).lower() for n in names} if isinstance(names, list) else set()


def compute(conn, settings, console, store, pid, bid, approver, probe=None, key_id=None,
            approval=None):
    """The rows to sign: [(arch, body bytes, fields)], computed from the set at
    its latest version and the build's store BOMs (a build of no pipeline: those
    its CLI *approval* bound, into its group). Raises Refused."""
    from bits_helpers import certify, trust
    from .. import authz
    if pid == NO_PIPELINE:
        group = _name((json.loads(approval["groups"] or "[]") or [""])[0], "group")
        boms = cli_boms(store, bid, approval)
    else:
        pipeline = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
        if pipeline is None:
            raise Refused("pipeline %s is not recorded" % pid)
        group = _name(replay.certify_group(console, pipeline), "group")
        boms = build_boms(conn, store, pid, bid)
    version, _vpid, commit = certified.current(conn)
    if not version:
        raise Refused("the certified set has not been imported yet")
    live = {(r["build_id"], r["arch"]): r["path"] for r in conn.execute(
        "SELECT build_id, arch, path FROM certified WHERE added_version <= ? AND"
        " (removed_version IS NULL OR removed_version > ?)", (version, version))}
    policy = trust.load_key_policy()
    if policy is None:
        raise Refused("no key policy (key-policy.json): the worker fails closed")
    tmp = tempfile.mkdtemp(prefix="certify-")
    try:
        root = certified.materialize(conn, version, os.path.join(tmp, "set"))
        in_set, set_files = {}, {}
        for arch, (key, doc) in sorted(boms.items()):
            path = live.get((bid, arch)) or _path(group, bid, arch, key)
            if path.split("/")[0] != group:
                raise Refused("the set holds %s's %s BOM under %s, not %s"
                              % (bid, arch, path.split("/")[0], group))
            in_set[arch] = (bid, arch) in live
            dest = os.path.join(root, path)
            if not os.path.realpath(dest).startswith(os.path.realpath(root) + os.sep):
                raise Refused("unsafe BOM path %r" % path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            gone = _decertified(dest) if in_set[arch] else set()
            if gone:
                # Packages decertified from the set's copy stay out of it.
                doc = dict(doc, packages=[e for e in doc.get("packages") or []
                                          if not (isinstance(e, dict) and str(
                                              e.get("package") or "").lower() in gone)])
            text = json.dumps(dict(doc, certified_by=[approver]), indent=1)
            with open(dest, "w") as fh:
                fh.write(text)
            if not in_set[arch]:
                set_files[arch] = (path, text.encode())
        if probe is None:
            s3, bucket = store
            probe = certify.s3_probe(s3, bucket, settings.orch_default_arch,
                                     cache=shadow.DigestCache(conn))
        try:
            results = certify.build_by_arch(root, probe=probe, only_archs=sorted(boms),
                                            valid_days=_valid_days(settings))
        except certify.CertifyError as e:
            if shadow._transient_cause(e):
                raise
            raise Refused(str(e)[:1000])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if key_id is None:
        from .. import signproxy
        key_id = signproxy.call(settings, trust.proxy_pubkey)[0]
    resolved = authz.resolved_admin_policy(settings, None)
    out = []
    for arch, m in results:
        groups = replay._groups({arch: m})
        denied = [g for g in groups if not authz.is_admin_for(approver, g, resolved)]
        if denied:
            raise Refused("%s (who pre-approved %s) is not an admin for %s"
                          % (approver, ("pipeline %s" % pid) if pid else "build %s" % bid,
                             ", ".join(denied)))
        bad = [g for g in groups if not trust.key_authorized(key_id, g, policy)]
        if bad:
            raise Refused("signing key %s is not allowed for %s" % (key_id, ", ".join(bad)))
        seq = next_sequence(conn, settings, store, arch)
        m = dict(m, certified_by=[approver], certified_at=certify.now_iso(), sequence=seq,
                 issuer=issuer(store), event=_chain_position(conn))
        body = json.dumps(m, indent=1, sort_keys=True).encode()
        out.append((arch, body, {
            "set_version": version, "set_commit": commit or "", "in_set": int(in_set.get(arch, False)),
            "boms": json.dumps([[boms[arch][0], hashlib.sha256(json.dumps(
                boms[arch][1], sort_keys=True).encode()).hexdigest()]] if arch in boms else []),
            "sha256": hashlib.sha256(body).hexdigest(), "key_id": key_id, "sequence": seq,
            "set_path": set_files.get(arch, ("", None))[0],
            "set_body": set_files.get(arch, ("", None))[1]}))
    return out


def _newer_in_place(conn, settings, store, row):
    """Whether a manifest newer than *row*'s is in place: the store's numbered
    above it, or as it but other bytes (the number readers enforce; the same
    bytes are this run's own, half written); for a run without a number, one
    written after it. A certification not in the owned set yet is then made
    again from the set as it is now (Defer): the manifest in place lacks it."""
    if row["sequence"] is not None:
        seq, sha = published(settings, store, row["arch"])
        newer = seq > row["sequence"] or (seq == row["sequence"] and sha != row["sha256"])
    else:
        newer = conn.execute("SELECT 1 FROM certify_runs WHERE arch = ? AND written_ms IS NOT"
                             " NULL AND created_ms > ?", (row["arch"], row["created_ms"])
                             ).fetchone() is not None
    if newer and settings.orch_owns_set and row["set_body"] is not None:
        with db.transaction(conn):
            conn.execute("UPDATE certify_runs SET manifest = NULL, updated_ms = ? WHERE id = ?",
                         (db.now_ms(), row["id"]))
        raise actions.Defer("a newer %s manifest is in place: made again" % row["arch"], 0)
    return newer


def _sign_and_write(conn, settings, store, row, sign=None, writer=None):
    """Sign the stored bytes (once: a rerun reuses the kept signature) and
    write them. The groups' checks are made again on the bytes being signed."""
    from bits_helpers import trust
    from .. import authz
    arch = _name(row["arch"], "architecture")
    body = bytes(row["manifest"])
    _newer_in_place(conn, settings, store, row)          # not signed in vain
    if row["signature"]:
        env = json.loads(row["signature"])
    else:
        policy = trust.load_key_policy()
        if policy is None:
            raise Refused("no key policy (key-policy.json): the worker fails closed")
        groups = replay._groups({arch: json.loads(body)})
        if _is_withdrawal(row):
            why = removal_only(settings, store, arch, body, conn)
            if why:
                raise Refused("not signed without a pre-approval: %s" % why)
        else:
            resolved = authz.resolved_admin_policy(settings, None)
            denied = [g for g in groups if not authz.is_admin_for(row["approver"], g, resolved)]
            if denied:
                raise Refused("%s is no longer an admin for %s"
                              % (row["approver"], ", ".join(denied)))
        bad = [g for g in groups if not trust.key_authorized(row["key_id"], g, policy)]
        if bad:
            raise Refused("signing key %s is not allowed for %s" % (row["key_id"], ", ".join(bad)))
        if signed_today(conn) >= settings.orch_certify_daily_signs:
            raise actions.Defer("the worker reached its limit of %d signatures a day"
                                % settings.orch_certify_daily_signs, 3600 * 1000)
        hold = outside.sign_hold(conn, settings)
        if hold:
            raise actions.Defer(hold, 5 * 60 * 1000)
        if sign is None:
            from .. import signproxy
            env = signproxy.call(settings, trust.sign_bytes_via_proxy, body)
        else:
            env = sign(body)
        kid = trust.verify_bytes(body, env, trust.load_trusted_keys())
        if kid is None or kid != row["key_id"]:
            raise Refused("the signature over the %s manifest does not verify by key %s"
                          % (arch, row["key_id"]))
        # Kept at once: a failed write retries with this signature, not a new one.
        with db.transaction(conn):
            conn.execute("UPDATE certify_runs SET signature = ?, signed_ms = ?, updated_ms = ?"
                         " WHERE id = ?", (json.dumps(env), db.now_ms(), db.now_ms(), row["id"]))
            events.append(conn, "certify.signature", "signatures", events.WORKER,
                          {"arch": arch, "build_id": row["build_id"], "sha256": row["sha256"],
                           "key_id": kid, "sequence": row["sequence"]})
    newer = _newer_in_place(conn, settings, store, row)
    written = None
    if not newer:
        if writer is None:
            from .. import credproxy
            writer = credproxy.s3_writer(settings)[0]
        bucket = store[1]
        key = "%s-%s.json" % (write_prefix(settings), arch)
        # The manifest first, then its signature. In between, a reader sees the
        # new manifest with the old signature, which does not verify: it fails
        # closed, never trusts other bytes.
        writer.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        writer.put_object(Bucket=bucket, Key=key + ".sig", Body=json.dumps(env).encode(),
                          ContentType="application/json")
        written = db.now_ms()
    with db.transaction(conn):
        conn.execute("UPDATE certify_runs SET state = 'signed', written_ms = ?, reason = ?,"
                     " updated_ms = ? WHERE id = ?",
                     (written, "" if written else "not written: a newer build's manifest is in place",
                      db.now_ms(), row["id"]))
        if settings.orch_owns_set and row["set_path"] and row["set_body"] is not None:
            # The set the worker owns holds what it signed, architecture by
            # architecture (M8b).
            certified.change(conn, add={row["set_path"]: bytes(row["set_body"])},
                             cause="certify:%s:%s" % (row["pipeline_id"] or "cli",
                                                      row["build_id"]))
            conn.execute("UPDATE certify_runs SET set_body = NULL WHERE id = ?", (row["id"],))
        events.append(conn, "certify.signed", "pipeline:%d" % row["pipeline_id"], events.WORKER,
                      {"pipeline": row["pipeline_id"], "build_id": row["build_id"],
                       "arch": arch, "sha256": row["sha256"], "key_id": row["key_id"],
                       "written": bool(written)})
    return env


def run(params, deps):
    """The certify action, on the certifier thread (*deps* has its connection).
    Tests give *deps* sign/writer/probe/key_id stand-ins."""
    conn, settings = deps.conn, deps.settings
    sign, writer = getattr(deps, "sign", None), getattr(deps, "writer", None)
    probe, key_id = getattr(deps, "probe", None), getattr(deps, "key_id", None)
    pid, bid = int(params.get("pipeline") or NO_PIPELINE), params["build_id"]
    try:
        try:
            a = _approval(conn, params)
            since = json.loads((conn.execute("SELECT value FROM kv WHERE key ="
                                             " 'certify.decertified'").fetchone() or ["{}"])[0])
            if bid in since and a["created_ms"] < since[bid]:
                raise Refused("build %s was decertified after the database copy it was"
                              " recovered from: %s" % (bid, "a new pre-approval certifies it"
                                                       if pid == NO_PIPELINE else
                                                       "a new build certifies it"))
            rows = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND build_id = ?"
                                " AND arch != '*' AND approval_id = ?", (pid, bid, a["id"])).fetchall()
            if not rows or any(_overtaken(r) for r in rows):
                if signed_today(conn) >= settings.orch_certify_daily_signs:
                    raise actions.Defer("the worker reached its limit of %d signatures a day"
                                        % settings.orch_certify_daily_signs, 3600 * 1000)
                todo = compute(conn, settings, deps.reader(), deps.store(), pid, bid,
                               a["approver"], probe=probe, key_id=key_id, approval=a)
                keep = {r["arch"] for r in rows if not _overtaken(r)}
                with db.transaction(conn):
                    for arch, body, fields in todo:
                        if arch in keep:
                            continue
                        # New bytes: no signature of an earlier run of this arch.
                        _upsert(conn, pid, bid, arch, "signing", "", manifest=body,
                                approval_id=a["id"], approver=a["approver"], signature=None,
                                written_ms=None, **fields)
                rows = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND"
                                    " build_id = ? AND arch != '*' AND approval_id = ?",
                                    (pid, bid, a["id"])).fetchall()
            for row in rows:
                if _overtaken(row):                  # made again without this arch
                    with db.transaction(conn):
                        conn.execute("UPDATE certify_runs SET state = 'skipped', reason = ?,"
                                     " updated_ms = ? WHERE id = ?",
                                     ("not made again: the build has no such BOM now",
                                      db.now_ms(), row["id"]))
                elif row["state"] == "signing":
                    _sign_and_write(conn, settings, deps.store(), row, sign, writer)
        except Exception as e:
            if isinstance(e, (Refused, actions.Defer)):
                raise
            from .. import signproxy
            # The store, GitLab, a proxy (an unreachable socket is an OSError,
            # a failed proxy call a RuntimeError) or signing not configured yet.
            if (shadow.transient(e) or shadow._transient_cause(e)
                    or isinstance(e, (OSError, RuntimeError, signproxy.Unavailable))):
                raise actions.Retry("%s: %s" % (type(e).__name__, str(e)[:300]))
            raise
    except Refused as e:
        _decide(conn, pid, bid, "refused", str(e), archs=True)
        raise
    archs = sorted(r["arch"] for r in conn.execute(
        "SELECT arch FROM certify_runs WHERE pipeline_id = ? AND build_id = ? AND arch != '*'",
        (pid, bid)))
    with db.transaction(conn):
        _upsert(conn, pid, bid, "*", "signed", "")
    return {"archs": archs}


# --- withdrawals ----------------------------------------------------------------------
#
# A BOM removed from the certified set stays trusted while the architecture's
# signed manifest still lists it. The worker re-signs that manifest from the set
# without it. Its certify_runs row has no pipeline and the build "set@<version>".


# What may differ between two copies of one entry: which build recorded it.
_PROVENANCE = ("built_by", "completed_at", "bits_version", "bits_dist_hash")


def _ident(e):
    """An entry as a withdrawal must keep it: every field but its provenance
    (a re-signed entry may not change its group, licence, redistributable, ...)."""
    return json.dumps({k: v for k, v in e.items() if k not in _PROVENANCE}, sort_keys=True)


def _but_group(e):
    return json.dumps({k: v for k, v in e.items() if k not in _PROVENANCE and k != "group"},
                      sort_keys=True)


def _certified_by_group(conn, arch, e):
    """Whether a BOM live in the set lists *e*'s object for *e*'s group (the
    entry's own group, else its BOM's, as the merge reads it)."""
    group = str(e.get("group") or "").lower()
    for r in conn.execute("SELECT c.grp, b.body FROM certified c JOIN boms b ON"
                          " b.sha256 = c.bom_sha256 WHERE c.removed_version IS NULL AND"
                          " c.arch = ?", (arch,)):
        try:
            entries = json.loads(r["body"]).get("packages") or []
        except (ValueError, AttributeError):
            continue
        if any(isinstance(x, dict) and x.get("hash") == e.get("hash")
               and x.get("tarball_sha256") == e.get("tarball_sha256")
               and str(x.get("group") or r["grp"]).lower() == group for x in entries):
            return True
    return False


def removal_only(settings, store, arch, body, conn=None):
    """Why *body* may not be signed without a pre-approval, or None: every entry
    must already be in the architecture's manifest in the store (verified), with
    the same group, and its sequence must follow that one's. With *conn*, an
    entry that only changed group is also kept when that group's own BOM in the
    set certifies it (one group stopped vouching for an object another one
    certified too: the merge then names the other)."""
    from bits_helpers import trust
    s3, bucket = store
    key = "%s-%s.json" % (write_prefix(settings), arch)
    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        env = json.loads(s3.get_object(Bucket=bucket, Key=key + ".sig")["Body"].read())
    except ValueError:
        return "the store's %s signature is not JSON" % arch
    except Exception as e:
        if shadow._missing(e):
            return "the store has no %s manifest to withdraw from" % arch
        raise
    if trust.verify_bytes(raw, env, trust.load_trusted_keys()) is None:
        return "the store's %s manifest does not verify" % arch
    old, new = json.loads(raw), json.loads(body)
    olds = [e for e in old.get("packages") or [] if isinstance(e, dict)]
    have, regroup = {_ident(e) for e in olds}, {_but_group(e) for e in olds}
    extra = [str(e.get("package") or e.get("hash")) for e in new.get("packages") or []
             if not isinstance(e, dict) or (_ident(e) not in have and not (
                 conn is not None and _but_group(e) in regroup
                 and _certified_by_group(conn, arch, e)))]
    if extra:
        return ("it would add %d entr(y/ies) (%s), or change more than the group of one"
                % (len(extra), ", ".join(extra[:5])))
    before = old.get("sequence") if isinstance(old.get("sequence"), int) else 0
    if not isinstance(new.get("sequence"), int) or new["sequence"] <= before:
        return "its sequence does not follow the store's %d" % before
    return None


def withdrawals(conn, settings, now=None):
    """Request a withdrawal for each architecture whose last written manifest
    was made from a set version that BOMs of it have since left. Returns how
    many were requested."""
    version, _pid, _commit = certified.current(conn)
    if not version:
        return 0
    now = now or db.now_ms()
    n = 0
    for r in conn.execute("SELECT arch, MAX(set_version) AS sv FROM certify_runs WHERE"
                          " written_ms IS NOT NULL AND arch != '*' GROUP BY arch").fetchall():
        arch, sv = r["arch"], r["sv"]
        if sv is None or sv >= version or conn.execute(
                "SELECT 1 FROM certified WHERE arch = ? AND removed_version > ? AND"
                " removed_version <= ? LIMIT 1", (arch, sv, version)).fetchone() is None:
            continue
        # Re-signing from the set would drop a build being certified now, or one
        # certified a moment ago that is not in the set yet: wait for those (a
        # build not in the set a day later was not taken in, and is dropped).
        if conn.execute(
                "SELECT 1 FROM certify_runs c WHERE c.arch = ? AND NOT (c.pipeline_id = ? AND"
                " c.build_id LIKE 'set@%') AND (c.state = 'signing' OR (c.written_ms > ? AND NOT EXISTS (SELECT 1 FROM"
                " certified s WHERE s.build_id = c.build_id AND s.arch = c.arch))) LIMIT 1",
                (arch, NO_PIPELINE, now - WAIT_MS)).fetchone():
            continue
        # Asked for again when it failed (a few times): a removal must not stay trusted.
        failed = conn.execute("SELECT COUNT(*) FROM actions WHERE kind = ? AND subject = ? AND"
                              " state = 'failed'", (WITHDRAW, "withdraw:%s:%d" % (arch, version))
                              ).fetchone()[0]
        if failed >= WITHDRAW_TRIES:
            continue
        _row, created = actions.request(
            conn, WITHDRAW, {"arch": arch, "set_version": str(version)}, events.WORKER, "",
            True, idempotency_key="withdraw:%s:%d%s" % (arch, version,
                                                         ":%d" % failed if failed else ""),
            cause="set:%d" % version)
        n += int(created)
    if settings.orch_owns_set:
        n += refreshes(conn, settings, version, now)
    return n


def refreshes(conn, settings, version, now):
    """M8b: re-sign each architecture of the set whose manifest in place is not
    the worker's since the switch, or older than a third of the manifests'
    lifetime, so none expires while the set still vouches for it."""
    r = conn.execute("SELECT value FROM kv WHERE key = 'owns_set.since_ms'").fetchone()
    if r is None:
        with db.transaction(conn):
            conn.execute("INSERT INTO kv (key, value) VALUES ('owns_set.since_ms', ?)",
                         (str(now),))
        since = now
    else:
        since = int(r["value"])
    stale = now - int((_valid_days(settings) or 30) * DAY_MS / 3)
    day = time.strftime("%Y%m%d", time.gmtime(now / 1000))
    n = 0
    for a in conn.execute("SELECT DISTINCT arch FROM certified WHERE removed_version IS NULL"
                          " AND arch != ''").fetchall():
        arch = a["arch"]
        last = conn.execute("SELECT MAX(written_ms) FROM certify_runs WHERE arch = ? AND"
                            " written_ms >= ?", (arch, since)).fetchone()[0]
        if last is not None and last > stale:
            continue
        if conn.execute("SELECT 1 FROM certify_runs WHERE arch = ? AND state = 'signing'"
                        " LIMIT 1", (arch,)).fetchone():
            continue                               # a certification will write it anyway
        _row, created = actions.request(
            conn, WITHDRAW, {"arch": arch, "set_version": str(version), "refresh": day},
            events.WORKER, "", True, idempotency_key="refresh:%s:%s" % (arch, day),
            cause="refresh")
        n += int(created)
    return n


def compute_withdrawal(conn, settings, store, arch, version, probe=None, key_id=None):
    """The manifest of *arch* made from the set at *version* alone, and its
    certify_runs fields."""
    from bits_helpers import certify, trust
    r = conn.execute("SELECT commit_sha FROM set_versions WHERE version = ?",
                     (version,)).fetchone()
    if r is None:
        raise Refused("set version %d is not recorded" % version)
    tmp = tempfile.mkdtemp(prefix="withdraw-")
    try:
        root = certified.materialize(conn, version, os.path.join(tmp, "set"))
        if probe is None:
            s3, bucket = store
            probe = certify.s3_probe(s3, bucket, settings.orch_default_arch,
                                     cache=shadow.DigestCache(conn))
        try:
            results = dict(certify.build_by_arch(
                root, probe=probe, only_archs=[arch],
                valid_days=_valid_days(settings)))
        except certify.CertifyError as e:
            if shadow._transient_cause(e):
                raise
            raise Refused(str(e)[:1000])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if key_id is None:
        from .. import signproxy
        key_id = signproxy.call(settings, trust.proxy_pubkey)[0]
    seq = next_sequence(conn, settings, store, arch)
    m = dict(results[arch], certified_by=[], certified_at=certify.now_iso(), sequence=seq,
             issuer=issuer(store), withdrawn={"set_version": version},
             event=_chain_position(conn))
    body = json.dumps(m, indent=1, sort_keys=True).encode()
    return body, {"set_version": version, "set_commit": r["commit_sha"] or "", "in_set": 1,
                  "boms": "[]", "sha256": hashlib.sha256(body).hexdigest(), "key_id": key_id,
                  "sequence": seq}


def run_withdraw(params, deps):
    """The withdraw action, on the certifier thread."""
    conn, settings = deps.conn, deps.settings
    sign, writer = getattr(deps, "sign", None), getattr(deps, "writer", None)
    arch, version = _name(params["arch"], "architecture"), int(params["set_version"])
    bid = WITHDRAWAL_PREFIX + str(version) + ("-" + params["refresh"] if params.get("refresh") else "")
    # Made from the set as it is when it runs, not as it was when requested: a
    # build the set took in meanwhile must stay in.
    version = certified.current(conn)[0] or version
    try:
        try:
            if _repairable(conn, settings, deps.store(), arch)[0] in ("signature", "previous"):
                raise actions.Defer("the %s manifest in place is half-written: it is"
                                    " repaired first" % arch, 60 * 1000)
            row = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND"
                               " build_id = ? AND arch = ?", (NO_PIPELINE, bid, arch)).fetchone()
            if row is None or row["state"] in ("failed", "refused"):   # (asked for again)
                body, fields = compute_withdrawal(conn, settings, deps.store(), arch, version,
                                                  probe=getattr(deps, "probe", None),
                                                  key_id=getattr(deps, "key_id", None))
                with db.transaction(conn):
                    _upsert(conn, NO_PIPELINE, bid, arch, "signing", "", manifest=body,
                            signature=None, written_ms=None, **fields)
                row = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND"
                                   " build_id = ? AND arch = ?", (NO_PIPELINE, bid, arch)).fetchone()
            if row["state"] == "signing" and conn.execute(
                    "SELECT 1 FROM certify_runs WHERE arch = ? AND written_ms IS NOT NULL AND"
                    " created_ms > ?", (arch, row["created_ms"])).fetchone():
                # A manifest made after this one is in place: it has no more trust.
                with db.transaction(conn):
                    conn.execute("UPDATE certify_runs SET state = 'skipped', manifest = NULL,"
                                 " reason = 'superseded by a newer manifest', updated_ms = ?"
                                 " WHERE id = ?", (db.now_ms(), row["id"]))
                return {"arch": arch, "set_version": version, "superseded": True}
            if row["state"] == "signing":
                _sign_and_write(conn, settings, deps.store(), row, sign, writer)
        except Exception as e:
            if isinstance(e, (Refused, actions.Defer)):
                raise
            from .. import signproxy
            if (shadow.transient(e) or shadow._transient_cause(e)
                    or isinstance(e, (OSError, RuntimeError, signproxy.Unavailable))):
                raise actions.Retry("%s: %s" % (type(e).__name__, str(e)[:300]))
            raise
    except Refused as e:
        with db.transaction(conn):
            conn.execute("UPDATE certify_runs SET state = 'refused', reason = ?, updated_ms = ?"
                         " WHERE pipeline_id = ? AND build_id = ? AND arch = ? AND state ="
                         " 'signing'", (str(e)[:1000], db.now_ms(), NO_PIPELINE, bid, arch))
            events.append(conn, "withdraw.refused", "arch:%s" % arch, events.WORKER,
                          {"arch": arch, "set_version": version, "reason": str(e)[:500]})
        raise
    return {"arch": arch, "set_version": version}


def _scope(params, deps):
    conn = getattr(deps, "conn", None)
    if conn is None:
        return ""
    if not params.get("pipeline"):
        # A build of no pipeline acts for the group its CLI pre-approval names.
        r = conn.execute("SELECT groups FROM approvals WHERE kind = 'preapproval' AND"
                         " via = 'cli' AND subject = ? ORDER BY id DESC LIMIT 1",
                         (params["build_id"],)).fetchone()
        groups = json.loads(r["groups"]) if r else []
        return groups[0] if len(groups) == 1 else ""
    r = conn.execute("SELECT community FROM pipelines WHERE id = ?",
                     (int(params["pipeline"]),)).fetchone()
    return (r["community"] if r else "") or ""


actions.register(actions.Kind(
    name=KIND,
    summary="Certify a build: sign its manifests and write them to the store"
            " (approved by the build's pre-approval only)",
    params={"type": "object",
            "properties": {"pipeline": {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                           "build_id": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,200}$"}},
            "required": ["build_id"], "additionalProperties": False},
    approval="preapproval",
    subject=lambda p: "certify:%s:%s" % (p.get("pipeline") or "cli", p["build_id"]),
    scope=_scope,
    run=run,
    safe_to_rerun=True,
    requesters=frozenset({"worker"})))

def run_decertify(params, deps):
    """The decertify action: a new version of the set without the requested
    group's BOMs of the build (of one architecture, or all). Another group that
    certified the build too keeps vouching for it until it decertifies it."""
    conn, settings = deps.conn, deps.settings
    if not settings.orch_owns_set:
        raise Refused("the certified set is bits-manifests' until the switch: remove the BOMs"
                      " with a merge request there")
    q, args = ("SELECT * FROM certified WHERE removed_version IS NULL AND build_id = ?",
               [params["build_id"]])
    if params.get("arch"):
        q += " AND arch = ?"
        args.append(params["arch"])
    found = conn.execute(q, args).fetchall()
    rows = [r for r in found if r["grp"].lower() == params["group"].lower()]
    if not rows:
        raise Refused("build %s%s is not in %s's certified set"
                      % (params["build_id"], " (%s)" % params["arch"] if params.get("arch") else "",
                         params["group"]))
    with db.transaction(conn):
        version, _added, removed = certified.change(
            conn, remove={r["path"] for r in rows},
            cause="decertify:%s:%s" % (params["build_id"], params.get("arch") or "*"))
        events.append(conn, "decertify.done", "build:%s" % params["build_id"], events.WORKER,
                      {"build_id": params["build_id"], "arch": params.get("arch") or "",
                       "group": params["group"], "reason": params.get("reason", ""),
                       "version": version, "removed": removed})
    others = sorted({r["grp"] for r in found} - {r["grp"] for r in rows})
    return dict({"version": version, "removed": removed},
                **({"still_certified_by": others} if others else {}))


actions.register(actions.Kind(
    name=DECERTIFY,
    summary="Take a build's BOMs out of the certified set (all architectures, or one);"
            " its manifests are then re-signed without them",
    params={"type": "object",
            "properties": {"group": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
                           "build_id": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,200}$"},
                           "arch": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,200}$"},
                           "reason": {"type": "string", "pattern": "^[^\\n]{0,500}$"}},
            "required": ["group", "build_id"], "additionalProperties": False},
    approval="requester",
    subject=lambda p: "decertify:%s:%s" % (p["build_id"], p.get("arch") or "*"),
    scope=lambda p, deps: p["group"].lower(),
    run=run_decertify,
    safe_to_rerun=True,
    requesters=frozenset({"person"})))

def run_decertify_package(params, deps):
    """The decertify-package action: a new version of the set in which each BOM
    of the requested group (of one architecture, or all) lists no entry of the
    package any more. Removal only: such a BOM is replaced by itself without
    those entries, and taken out when none is left; its manifests are then
    re-signed with fewer entries (a withdrawal). Nothing to remove is no error."""
    conn, settings = deps.conn, deps.settings
    if not settings.orch_owns_set:
        raise Refused("the certified set is bits-manifests' until the switch: remove the"
                      " package's entries with a merge request there")
    name, group = params["package"].lower(), params["group"].lower()
    q, args = "SELECT * FROM certified WHERE removed_version IS NULL", []
    if params.get("arch"):
        q += " AND arch = ?"
        args.append(params["arch"])
    add, remove, builds, others = {}, set(), set(), set()
    for r in conn.execute(q, args).fetchall():
        body = conn.execute("SELECT body FROM boms WHERE sha256 = ?",
                            (r["bom_sha256"],)).fetchone()["body"]
        try:
            doc = json.loads(body)
            entries = doc["packages"]
        except (ValueError, KeyError, TypeError):
            continue                               # not a BOM: it certifies nothing
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries
                if not (isinstance(e, dict) and str(e.get("package") or "").lower() == name)]
        if len(kept) == len(entries):
            continue
        if r["grp"].lower() != group:
            others.add(r["grp"])                   # another group's to take out
            continue
        builds.add(r["build_id"])
        if any(isinstance(e, dict) and e.get("hash") and e.get("tarball_sha256") for e in kept):
            doc["packages"] = kept
            doc["decertified_packages"] = sorted(
                set(doc.get("decertified_packages") or []) | {params["package"]})
            add[r["path"]] = json.dumps(doc, indent=1).encode()
        else:
            remove.add(r["path"])
    version = None
    if add or remove:
        with db.transaction(conn):
            version, _added, _removed = certified.change(
                conn, add=add, remove=remove,
                cause="decertify-package:%s:%s:%s" % (group, name, params.get("arch") or "*"))
            events.append(conn, "decertify.package", "package:%s" % name, events.WORKER,
                          {"package": params["package"], "group": params["group"],
                           "arch": params.get("arch") or "", "reason": params.get("reason", ""),
                           "version": version, "replaced": sorted(add),
                           "removed": sorted(remove)})
    return dict({"version": version, "boms_replaced": len(add), "boms_removed": len(remove),
                 "builds": sorted(builds)}, **({"still_certified_by": sorted(others)}
                                                if others else {}))


actions.register(actions.Kind(
    name=DECERTIFY_PACKAGE,
    summary="Take a package out of a group's certified BOMs (all architectures, or one):"
            " each BOM is replaced by itself without the package's entries",
    params={"type": "object",
            "properties": {"group": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
                           "package": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$"},
                           "arch": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,200}$"},
                           "reason": {"type": "string", "pattern": "^[^\\n]{0,500}$"}},
            "required": ["group", "package"], "additionalProperties": False},
    approval="requester",
    subject=lambda p: "decertify-package:%s:%s:%s" % (p["group"].lower(), p["package"].lower(),
                                                      p.get("arch") or "*"),
    scope=lambda p, deps: p["group"].lower(),
    run=run_decertify_package,
    safe_to_rerun=True,
    requesters=frozenset({"person"})))

actions.register(actions.Kind(
    name=WITHDRAW,
    summary="Withdraw from an architecture's signed manifest the BOMs the certified set"
            " removed (re-signed with fewer entries only, so no pre-approval)",
    params={"type": "object",
            "properties": {"arch": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,200}$"},
                           "set_version": {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                           # a day, or r<sequence> after a repair
                           "refresh": {"type": "string", "pattern": "^([0-9]{8}|r[0-9]{1,19})$"}},
            "required": ["arch", "set_version"], "additionalProperties": False},
    approval="requester",
    subject=lambda p: "withdraw:%s:%s%s" % (p["arch"], p["set_version"],
                                           "-" + p["refresh"] if p.get("refresh") else ""),
    scope=lambda p, deps: "",
    run=run_withdraw,
    safe_to_rerun=True,
    requesters=frozenset({"worker"})))




def _in_place(settings, store, arch):
    """(bytes, .sig bytes) of the architecture's manifest in the store, each None
    when missing."""
    s3, bucket = store
    key = "%s-%s.json" % (write_prefix(settings), arch)
    out = []
    for k in (key, key + ".sig"):
        try:
            out.append(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
        except Exception as e:
            if not shadow._missing(e):
                raise
            out.append(None)
    return out


def _verifies(raw, sig):
    from bits_helpers import trust
    try:
        return bool(raw and sig and trust.verify_bytes(raw, json.loads(sig),
                                                       trust.load_trusted_keys()))
    except ValueError:
        return False


def _repairable(conn, settings, store, arch):
    """What repair does for the architecture's manifest in the store, when it is
    one this database signed and did not record as written: (plan, row, previous),
    plan 'recorded' (it verifies: written in full), 'signature' (a withdrawal's,
    put back), 'previous' (the manifest before it goes back: its .sig is still
    in place) or 're-sign' (nothing to put back); plan None otherwise."""
    raw, sig = _in_place(settings, store, arch)
    row = raw is not None and conn.execute(
        "SELECT * FROM certify_runs WHERE arch = ? AND sha256 = ? AND signature IS NOT NULL"
        " AND written_ms IS NULL AND sequence IS NOT NULL AND state != 'signing'"
        " ORDER BY sequence DESC LIMIT 1", (arch, hashlib.sha256(raw).hexdigest())).fetchone()
    if not row:
        return None, None, None
    if _verifies(raw, sig):
        return "recorded", row, None
    if _is_withdrawal(row):
        return "signature", row, None
    prev = conn.execute("SELECT * FROM certify_runs WHERE arch = ? AND written_ms IS NOT NULL"
                        " AND manifest IS NOT NULL AND signature IS NOT NULL AND sequence < ?"
                        " ORDER BY sequence DESC LIMIT 1", (arch, row["sequence"])).fetchone()
    if prev is not None and sig == prev["signature"].encode():
        return "previous", row, prev
    return "re-sign", row, None


def repair(conn, settings, store, writer=None):
    """An architecture's manifest in the store that this database signed but
    did not record as written, by a run that then ended. Written in full, it is
    recorded. Half-written (its bytes, not its signature): a withdrawal's
    signature is put back (removal only); a certification's is not (its
    approval may be gone): a re-sign from the set is asked for, and meanwhile
    the manifest before it goes back when the .sig in place is still its own.
    Nothing new is signed here. Returns how many."""
    n = 0
    for (arch,) in conn.execute("SELECT DISTINCT arch FROM certify_runs WHERE signature IS NOT"
                                " NULL AND written_ms IS NULL AND sequence IS NOT NULL AND"
                                " state != 'signing'").fetchall():
        key = "%s-%s.json" % (write_prefix(settings), arch)
        try:
            plan, row, prev = _repairable(conn, settings, store, arch)
            if plan is None:
                continue
            if plan in ("previous", "re-sign"):          # asked first: it waits for the put
                version = certified.current(conn)[0]
                created = version and actions.request(
                    conn, WITHDRAW, {"arch": arch, "set_version": str(version),
                                     "refresh": "r%d" % row["sequence"]},
                    events.WORKER, "", True, cause="repair",
                    idempotency_key="repair:%s:%d" % (arch, row["sequence"]))[1]
                if plan == "re-sign" and not created:
                    continue
            if plan in ("signature", "previous"):
                if writer is None:
                    from .. import credproxy
                    writer = credproxy.s3_writer(settings)[0]
                if plan == "signature":
                    writer.put_object(Bucket=store[1], Key=key + ".sig",
                                      Body=row["signature"].encode(),
                                      ContentType="application/json")
                else:
                    writer.put_object(Bucket=store[1], Key=key, Body=bytes(prev["manifest"]),
                                      ContentType="application/json")
        except Exception:                         # pylint: disable=broad-except
            log.exception("certifier: repairing the %s manifest", arch)
            continue
        with db.transaction(conn):
            if plan in ("recorded", "signature"):
                conn.execute("UPDATE certify_runs SET written_ms = ?, updated_ms = ? WHERE id = ?",
                             (db.now_ms(), db.now_ms(), row["id"]))
            events.append(conn, "certify.repaired", "arch:%s" % arch, events.WORKER,
                          {"arch": arch, "sequence": row["sequence"], "done": plan})
        n += 1
    return n


def failed_actions(conn):
    """A certification whose action failed for good (its retries spent, or
    interrupted): the build's decision says so; a withdrawal's run too."""
    n = 0
    for r in conn.execute(
            "SELECT c.id, a.error FROM certify_runs c JOIN actions a ON a.kind = ? AND"
            " a.subject = 'withdraw:' || c.arch || ':' || substr(c.build_id, 5)"
            " WHERE c.pipeline_id = ? AND c.build_id LIKE 'set@%' AND c.state = 'signing'"
            " AND a.state = 'failed'",
            (WITHDRAW, NO_PIPELINE)).fetchall():
        with db.transaction(conn):
            conn.execute("UPDATE certify_runs SET state = 'failed', reason = ?, updated_ms = ?"
                         " WHERE id = ?", ((r["error"] or "the action failed")[:1000],
                                           db.now_ms(), r["id"]))
        n += 1
    for r in conn.execute(
            "SELECT c.pipeline_id, c.build_id, a.error FROM certify_runs c JOIN actions a"
            " ON a.kind = ? AND a.subject = 'certify:' || CASE c.pipeline_id WHEN 0 THEN 'cli'"
            " ELSE c.pipeline_id END || ':' || c.build_id AND a.approval_id IS c.approval_id"
            " WHERE c.arch = '*' AND c.state = 'signing' AND a.state = 'failed'",
            (KIND,)).fetchall():
        _decide(conn, r["pipeline_id"], r["build_id"], "failed",
                r["error"] or "the action failed", archs=True)
        n += 1
    return n


def summary(conn, since_ms=0):
    """The builds' decisions since *since_ms*, by state."""
    return {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM certify_runs WHERE arch = '*' AND updated_ms >= ?"
        " GROUP BY state", (since_ms,))}


def for_builds(conn, keys):
    """The worker's certification of each of *keys* ((pipeline, build_id) pairs)
    it considered: ``{key: {state, reason, approver, archs: [{id, arch, state,
    reason, written}]}}``, without bytes."""
    keys = {(int(p), str(b)) for p, b in keys}
    out = {}
    pids = sorted({p for p, _b in keys})
    for i in range(0, len(pids), 500):
        part = pids[i:i + 500]
        for r in conn.execute(
                "SELECT id, pipeline_id, build_id, arch, state, reason, approver, written_ms"
                " FROM certify_runs WHERE pipeline_id IN (%s) ORDER BY arch"
                % ",".join("?" * len(part)), part):
            key = (r["pipeline_id"], r["build_id"])
            if key not in keys:
                continue
            d = out.setdefault(key, {"state": "", "reason": "", "approver": "", "archs": []})
            if r["arch"] == "*":
                d.update(state=r["state"], reason=r["reason"], approver=r["approver"])
            else:
                d["archs"].append({"id": r["id"], "arch": r["arch"], "state": r["state"],
                                   "reason": r["reason"], "written": r["written_ms"] is not None})
    return out


def listing(conn, pipeline=None, limit=100):
    q, args = ("SELECT id, pipeline_id, build_id, arch, state, reason, approval_id, approver,"
               " set_version, set_commit, in_set, boms, sha256, key_id, signed_ms, written_ms,"
               " created_ms, updated_ms FROM certify_runs"), []
    if pipeline is not None:
        q += " WHERE pipeline_id = ?"
        args.append(int(pipeline))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    return [dict(r, boms=json.loads(r["boms"] or "[]")) for r in conn.execute(q, args)]


# --- the thread ----------------------------------------------------------------------

class Certifier(threading.Thread):
    """Runs the certify actions (and, until M8b, the comparison) on a thread of its own, with
    its own connection, every *interval* seconds."""

    def __init__(self, path, settings, deps_factory, authorize, interval, stop, note):
        super().__init__(name="certifier", daemon=True)
        self.path, self.settings, self.deps_factory = path, settings, deps_factory
        self.authorize, self.interval, self.stop_event, self.note = authorize, interval, stop, note

    def run(self):
        while not self.stop_event.is_set():
            try:
                conn = db.connect(self.path)
            except Exception:
                log.exception("certifier: cannot open %s; trying again", self.path)
                self.stop_event.wait(max(60.0, self.interval))
                continue
            deps = self.deps_factory()
            deps.conn = conn
            try:
                while not self.stop_event.is_set():
                    began = time.monotonic()
                    try:
                        actions.run_due(conn, deps, self.authorize,
                                        kinds={KIND, WITHDRAW, DECERTIFY,
                                               DECERTIFY_PACKAGE})
                        failed_actions(conn)
                        repair(conn, self.settings, deps.store(), getattr(deps, "writer", None))
                        # Until M8b: compared with the CI's certifications.
                        if not self.settings.orch_owns_set:
                            from . import canary
                            canary.match(conn, self.settings)
                    except Exception as exc:
                        log.exception("certifier step failed")
                        self.note(conn, "certifier", "%s: %s" % (type(exc).__name__, exc))
                    else:
                        self.note(conn, "certifier", None)
                    self.stop_event.wait(max(1.0, self.interval - (time.monotonic() - began)))
            finally:
                conn.close()
