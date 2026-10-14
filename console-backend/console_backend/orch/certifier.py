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
DEFAULT_REMOTE = "https://gitlab.cern.ch/buncic/bits-manifests"
WAIT_MS = 24 * 3600 * 1000            # how long a build waits for its pre-approval
PER_PASS = 20
DAY_MS = 24 * 3600 * 1000


class Refused(Exception):
    """The worker will not certify this build (the reason is the message)."""


# --- the rows ------------------------------------------------------------------

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


def _decide(conn, pid, bid, state, reason, event=True, **fields):
    """The whole build's decision (arch '*'), with an event."""
    with db.transaction(conn):
        _upsert(conn, pid, bid, "*", state, reason, **fields)
        if event:
            events.append(conn, "certify." + state, "pipeline:%d" % int(pid), events.WORKER,
                          {"pipeline": int(pid), "build_id": bid,
                           **({"reason": reason[:500]} if reason else {})})


def signed_today(conn, now=None):
    """Signatures the worker made in the last day (each pipeline, build and
    architecture signs once)."""
    return conn.execute("SELECT COUNT(*) FROM certify_runs WHERE signed_ms > ?",
                        ((now or db.now_ms()) - DAY_MS,)).fetchone()[0]


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _name(value, what):
    """An architecture or group, as it goes into a file name and a store key:
    a plain name, never a path."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise Refused("%s %r is not a plain name" % (what, value))
    return value


def write_prefix(settings):
    """Where the worker writes its manifests: until M8b, the canary prefix,
    which must stay out of the production manifests' place and be plain names."""
    cp = settings.orch_canary_prefix.strip("/")
    top = settings.orch_manifest_prefix.strip("/").split("/")[0]
    parts = cp.split("/")
    if (not cp or parts[0] in (top, "MANIFESTS", "TARS", "BUILDS")
            or not all(_NAME.fullmatch(x) and ".." not in x for x in parts)):
        raise RuntimeError("BITS_ORCH_CANARY_PREFIX %r must be plain names, not under %s/"
                           % (cp, top))
    return cp


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


# --- the run (the certifier thread) -----------------------------------------------

def _approval(conn, params):
    a = conn.execute("SELECT * FROM approvals WHERE id = ?",
                     (params.get("_approval") or 0,)).fetchone()
    now = db.now_ms()
    if (a is None or a["kind"] != "preapproval" or a["via"] != "console"
            or a["subject"] != str(params["pipeline"])
            or a["approver"] != params.get("_approver")):
        raise Refused("the action's approval is not a console pre-approval of pipeline %s by %s"
                      % (params["pipeline"], params.get("_approver")))
    if a["revoked_ms"] is not None or a["expires_ms"] <= now:
        raise Refused("the pre-approval of pipeline %s is no longer in force" % params["pipeline"])
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


def _path(group, bid, arch, key):
    """Where the CI's merge request puts this BOM: manifests/<group>/<build_id>.
    <arch>.<run leaf> (bits certify), the run leaf the one of its store key."""
    leaf = key.rsplit("/", 1)[-1]
    tail = "." + arch + ".json"
    stem = leaf[:-len(tail)] if leaf.endswith(tail) else leaf[:-len(".json")]
    return "%s/%s.%s.%s.json" % (group, bid, arch, stem)


def compute(conn, settings, console, store, pid, bid, approver, probe=None, key_id=None):
    """The rows to sign: [(arch, body bytes, fields)], computed from the set at
    its latest version and the build's store BOMs. Raises Refused."""
    from bits_helpers import certify, trust
    from .. import authz
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
        in_set = {}
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
            with open(dest, "w") as fh:
                json.dump(dict(doc, certified_by=[approver]), fh, indent=1)
        if probe is None:
            s3, bucket = store
            probe = certify.s3_probe(s3, bucket, settings.orch_default_arch,
                                     cache=shadow.DigestCache(conn))
        try:
            results = certify.build_by_arch(root, probe=probe, only_archs=sorted(boms),
                                            valid_days=settings.orch_certify_valid_days or None)
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
            raise Refused("%s (who pre-approved pipeline %s) is not an admin for %s"
                          % (approver, pid, ", ".join(denied)))
        bad = [g for g in groups if not trust.key_authorized(key_id, g, policy)]
        if bad:
            raise Refused("signing key %s is not allowed for %s" % (key_id, ", ".join(bad)))
        m = dict(m, certified_by=[approver], certified_at=certify._now_iso())
        body = json.dumps(m, indent=1, sort_keys=True).encode()
        out.append((arch, body, {
            "set_version": version, "set_commit": commit or "", "in_set": int(in_set.get(arch, False)),
            "boms": json.dumps([[boms[arch][0], hashlib.sha256(json.dumps(
                boms[arch][1], sort_keys=True).encode()).hexdigest()]] if arch in boms else []),
            "sha256": hashlib.sha256(body).hexdigest(), "key_id": key_id}))
    return out


def _sign_and_write(conn, settings, store, row, sign=None, writer=None):
    """Sign the stored bytes (once: a rerun reuses the kept signature) and
    write them. The groups' checks are made again on the bytes being signed."""
    from bits_helpers import trust
    from .. import authz
    arch = _name(row["arch"], "architecture")
    body = bytes(row["manifest"])
    if row["signature"]:
        env = json.loads(row["signature"])
    else:
        policy = trust.load_key_policy()
        if policy is None:
            raise Refused("no key policy (key-policy.json): the worker fails closed")
        groups = replay._groups({arch: json.loads(body)})
        resolved = authz.resolved_admin_policy(settings, None)
        denied = [g for g in groups if not authz.is_admin_for(row["approver"], g, resolved)]
        if denied:
            raise Refused("%s is no longer an admin for %s" % (row["approver"], ", ".join(denied)))
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
    newer = conn.execute("SELECT 1 FROM certify_runs WHERE arch = ? AND written_ms IS NOT NULL"
                         " AND created_ms > ?", (arch, row["created_ms"])).fetchone()
    written = None
    if newer is None:
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
    pid, bid = int(params["pipeline"]), params["build_id"]
    try:
        try:
            a = _approval(conn, params)
            rows = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND build_id = ?"
                                " AND arch != '*'", (pid, bid)).fetchall()
            if not rows:
                if signed_today(conn) >= settings.orch_certify_daily_signs:
                    raise actions.Defer("the worker reached its limit of %d signatures a day"
                                        % settings.orch_certify_daily_signs, 3600 * 1000)
                todo = compute(conn, settings, deps.reader(), deps.store(), pid, bid,
                               a["approver"], probe=probe, key_id=key_id)
                with db.transaction(conn):
                    for arch, body, fields in todo:
                        _upsert(conn, pid, bid, arch, "signing", "", manifest=body,
                                approval_id=a["id"], approver=a["approver"], **fields)
                rows = conn.execute("SELECT * FROM certify_runs WHERE pipeline_id = ? AND"
                                    " build_id = ? AND arch != '*'", (pid, bid)).fetchall()
            for row in rows:
                if row["state"] == "signing":
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
        _decide(conn, pid, bid, "refused", str(e))
        with db.transaction(conn):
            conn.execute("UPDATE certify_runs SET state = 'refused', manifest = NULL, reason = ?,"
                         " updated_ms = ? WHERE pipeline_id = ? AND build_id = ? AND arch != '*'"
                         " AND state = 'signing'", (str(e)[:1000], db.now_ms(), pid, bid))
        raise
    archs = sorted(r["arch"] for r in conn.execute(
        "SELECT arch FROM certify_runs WHERE pipeline_id = ? AND build_id = ? AND arch != '*'",
        (pid, bid)))
    with db.transaction(conn):
        _upsert(conn, pid, bid, "*", "signed", "")
    return {"archs": archs}


def _scope(params, deps):
    conn = getattr(deps, "conn", None)
    if conn is None:
        return ""
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
            "required": ["pipeline", "build_id"], "additionalProperties": False},
    approval="preapproval",
    subject=lambda p: "certify:%s:%s" % (p["pipeline"], p["build_id"]),
    scope=_scope,
    run=run,
    safe_to_rerun=True,
    requesters=frozenset({"worker"})))




def failed_actions(conn):
    """A certification whose action failed for good (its retries spent, or
    interrupted): the build's decision says so."""
    n = 0
    for r in conn.execute(
            "SELECT c.pipeline_id, c.build_id, a.error FROM certify_runs c JOIN actions a"
            " ON a.kind = ? AND a.subject = 'certify:' || c.pipeline_id || ':' || c.build_id"
            " WHERE c.arch = '*' AND c.state = 'signing' AND a.state = 'failed'",
            (KIND,)).fetchall():
        _decide(conn, r["pipeline_id"], r["build_id"], "failed", r["error"] or "the action failed")
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
                        actions.run_due(conn, deps, self.authorize, kinds={KIND})
                        failed_actions(conn)
                        # Until M8b: compared with the CI's certifications.
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
