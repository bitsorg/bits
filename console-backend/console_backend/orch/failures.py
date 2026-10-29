# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Failures: every failed build job leaves a record, repeats are one issue, and a
reviewed signature can name an issue's cause.

collect - for each failed build pipeline not collected yet, each of its failed
          jobs (latest attempt): the end of its log (or of its bits-build.log
          artifact, when GitLab cut the log at its size limit) is parsed for
          bits' "BUILD FAILED: <pkg>@<version>" blocks and their "Error excerpt"
          (one failure per failed package); without one, the job's last lines
          and GitLab's failure reason are the excerpt. The excerpt is redacted
          before anything is stored.
issue   - the fingerprint of a failure is its package and its error lines with
          paths, hashes, numbers and times taken out; the same fingerprint is the
          same issue, with a count, first and last seen, communities, platforms.
match   - active signatures (regular expressions over the excerpt and
          "reason:<failure_reason>") name an issue's cause and suggested action.
backfill- with BITS_ORCH_BACKFILL_DAYS, a few of the older failed pipelines
          GitLab still has are recorded on every pass, newest first, until done.

Excerpts are untrusted text: stored capped, shown as text, never interpreted.
Whole logs stay in GitLab and are linked.
"""

import datetime
import hashlib
import json
import logging
import re

from . import db, events, reconcile

log = logging.getLogger("bits_orchestrator")

MAX_EXCERPT = 8 * 1024
LOG_TAIL = 1 << 20
COLLECT_PER_PASS = 20
BACKFILL_PER_PASS = 20
RETRY_MS = 10 * 60 * 1000
GIVE_UP_MS = 24 * 3600 * 1000
ARTIFACT = "bits-build.log"

# --- redaction ----------------------------------------------------------------

_KEY = (r"[\w.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key(?:[_-]?id)?"
        r"|private[_-]?key|credentials?|auth)[\w.-]*")
_VALUE = r"(?:\"[^\"\n]+\"|'[^'\n]+'|[A-Za-z0-9/+=_.~:@%-]{4,})"
_SECRETS = [
    (re.compile(r"\b(glpat|gldt|glrt|glptt|glcbt|gloas|glsoat|glffct|glimt|glagent)-[A-Za-z0-9_-]{8,}"),
     r"\1-<redacted>"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), r"\1_<redacted>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_<redacted>"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), r"\1<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted-jwt>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted-private-key>"),
    # Authorization: Bearer x, PRIVATE-TOKEN: x, Cookie: x
    (re.compile(r"(?i)\b(authorization|private-token|job-token|x-api-key|cookie|set-cookie)"
                r"(\s*[:=]\s*)(bearer\s+|basic\s+|token\s+)?\S+"), r"\1\2\3<redacted>"),
    # anything://user:pass@ and anything://token@
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1<redacted>@"),
    # ?private_token=x&access_token=x
    (re.compile(r"(?i)([?&][\w.-]*(?:token|key|secret|password|sig|signature)=)[^&\s\"']+"),
     r"\1<redacted>"),
    # --token x, --password=x, -p x / -px (mysql style) after a password-ish option
    (re.compile(r"(?i)(--[\w-]*(?:token|password|passwd|secret|api-key)[\w-]*)([= ]\s*)\S+"),
     r"\1\2<redacted>"),
    # KEY=value, KEY: value, "key": "value" (environment, YAML, JSON)
    (re.compile(r"(?i)(\b" + _KEY + r"[\"']?)(\s*[=:]\s*)" + _VALUE), r"\1\2<redacted>"),
]


def redact(text):
    """The text with credentials replaced: tokens of the known forms, auth
    headers, URL user:password, key=value secrets, private keys."""
    for rx, repl in _SECRETS:
        text = rx.sub(repl, text)
    return text


# --- parsing logs ---------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_SECTION = re.compile(r"section_(start|end):\d+:[A-Za-z0-9_.-]+(\[[^\]]*\])?\r?")
_FAILED = re.compile(r"BUILD FAILED:\s*(\S+?)@(\S+)")
_BITS_VERSION = re.compile(r"^\s*bits:\s*(.+?)\s*$", re.M)
_TRUNCATED = "Job's log exceeded limit"
# bits' logger prefixes each line of an error with its level ("ERROR: ...", or in
# debug "2026-10-11@09:00:01:ERROR:<pkg>:<version>: ...").
_LEVEL = re.compile(r"^(?:\d{4}-\d\d-\d\d@\d\d:\d\d:\d\d:)?(?:ERROR|CRITICAL)(?::[^\s:]+){0,3}: ?")
# GitLab runner's own lines after the script: not the failure.
_RUNNER = re.compile(r"^(Uploading artifacts|Downloading artifacts|Cleaning up|Saving cache|"
                     r"Restoring cache|Running after_script|Job succeeded|(ERROR: )?Job failed: exit code|"
                     r"WARNING: .*(artifact|cache)|[a-z_./-]+: found \d+ matching|"
                     r"Uploading artifacts as|Executing \"step_|Not uploading)", re.I)
# Where bits' failure message ends (its next headings).
_BLOCK_END = re.compile(r"^\s*(Environment:|Development Packages:|Next Steps|BUILD FAILED:)")


def clean(raw):
    """Log bytes as text lines: ANSI colours, GitLab section markers and progress
    redraws (carriage returns) taken out."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = _SECTION.sub("", _ANSI.sub("", text))
    return [_LEVEL.sub("", line.rstrip("\r").rsplit("\r", 1)[-1]) for line in text.split("\n")]


def parse(lines, before=40):
    """[(package, version, excerpt, bits_version)] for each BUILD FAILED block in
    the log lines; [] when there is none. The excerpt is bits' own "Error
    excerpt" (to its next heading), else the lines just before the block."""
    found = {}
    for i, line in enumerate(lines):
        m = _FAILED.search(line)
        if not m:
            continue
        block, in_excerpt, bits_version = [], False, ""
        for nxt in lines[i + 1:i + 400]:
            if _FAILED.search(nxt):
                break
            v = _BITS_VERSION.match(nxt)
            if v:
                bits_version = v.group(1)
            if nxt.strip().startswith("Error excerpt:"):
                in_excerpt = True
                continue
            if in_excerpt and _BLOCK_END.match(nxt):
                in_excerpt = False
            if in_excerpt:
                block.append(nxt)
        excerpt = "\n".join(block).strip("\n") \
            or "\n".join(lines[max(0, i - before):i]).strip("\n")
        # A package reported twice (the run summary repeats it): the last one wins.
        found[m.group(1)] = (m.group(1), m.group(2), excerpt, bits_version)
    return list(found.values())


def last_lines(lines, n=40):
    """The job's last lines, without GitLab runner's own trailer."""
    keep = [ln for ln in lines if ln.strip() and not _RUNNER.match(ln.strip())]
    return "\n".join(keep[-n:])


# --- fingerprints ---------------------------------------------------------------

_NORMALIZE = [
    (re.compile(r"(?i)\b(correlation_id|request_id|job_id|pipeline_id)=\S+"), r"\1=<id>"),
    (re.compile(r"\bcc[A-Za-z0-9]{6}\.(?:s|o|res|ltrans\d*\.\w+)\b"), "<tmp>"),
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}\b"), "<id>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)?\b"), "<time>"),
    (re.compile(r"\b\d\d:\d\d:\d\d(\.\d+)?\b"), "<time>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:/[^\s/:'\"()\[\]]+)+/"), "<path>/"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "<hex>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\d+"), "N"),
    (re.compile(r"[ \t]+"), " "),
]
_MATCHED_HEAD = re.compile(r"^\s*Matched error lines")
_TAIL_HEAD = re.compile(r"^\s*Last \d+ lines of log")


def normalize(line):
    for rx, repl in _NORMALIZE:
        line = rx.sub(repl, line)
    return line.strip()


def key_lines(excerpt, n=5):
    """The lines a failure is recognised by: bits' matched error lines when the
    excerpt has them (the tail varies from run to run), else its last lines;
    normalised, consecutive repeats dropped, the last *n*."""
    lines = excerpt.splitlines()
    matched, in_matched = [], False
    for ln in lines:
        if _MATCHED_HEAD.match(ln):
            in_matched = True
            continue
        if _TAIL_HEAD.match(ln):
            in_matched = False
            continue
        if in_matched:
            matched.append(ln)
    # The real error lines first: with make -j, "make[N]: *** ... Error N" lines
    # would otherwise push them out of the last n.
    errs = [ln for ln in matched if re.search(r"(?i)error|undefined reference|fatal|traceback", ln)
            and not re.search(r"make(\[\d+\])?: \*\*\*", ln)]
    use = errs or matched or [ln for ln in lines if ln.strip()][-n:]
    out = []
    for ln in use:
        k = normalize(ln)
        if k and (not out or out[-1] != k):
            out.append(k)
    return out[-n:]


def fingerprint(package, excerpt, reason=""):
    keys = key_lines(excerpt) or ["reason:" + (reason or "unknown")]
    body = "\n".join([package or "-"] + keys)
    return hashlib.sha256(body.encode()).hexdigest()[:16], keys


# --- signatures -----------------------------------------------------------------

SEEDS = [
    (r"Non-compatible flatbuffers version",
     "Headers generated by one flatbuffers version, built against another",
     "Check which flatbuffers the recipe builds and generates with (arrow uses its bundled one)", 0),
    (r"No space left on device",
     "The build host ran out of disk",
     "Clean the host's work area (old builds, docker images), then retry", 0),
    (r"Could not resolve host|Connection timed out|Failed to connect to|TLS handshake timeout"
     r"|curl: \(28\)|fatal: unable to access|Temporary failure in name resolution",
     "A download failed (network)",
     "Retry", 1),
    (r"reason:(runner_system_failure|stuck_or_timeout_failure|runner_unsupported"
     r"|scheduler_failure|data_integrity_failure)",
     "The runner failed or lost the job",
     "Retry on another host", 1),
]


def seed_signatures(conn):
    """The first signatures, once (an admin can retire them)."""
    if reconcile._kv_get(conn, "signatures.seeded") == "1":
        return 0
    with db.transaction(conn):
        for pattern, cause, action, safe in SEEDS:
            conn.execute("INSERT INTO signatures (pattern, cause, action, safe_to_retry, state,"
                         " proposed_by, accepted_by, created_ms) VALUES (?,?,?,?, 'active',"
                         " 'seed', 'seed', ?)", (pattern, cause, action, safe, db.now_ms()))
        reconcile._kv_set(conn, "signatures.seeded", "1")
    return len(SEEDS)


def _compiled(conn):
    out = []
    for s in conn.execute("SELECT * FROM signatures WHERE state = 'active' ORDER BY id"):
        try:
            out.append((s["id"], re.compile(s["pattern"])))
        except re.error as exc:
            log.warning("signature %s does not compile: %s", s["id"], exc)
    return out


def match(signatures, excerpt, reason):
    """The id of the first active signature that matches, or None."""
    text = "%s\nreason:%s" % (excerpt, reason or "")
    for sid, rx in signatures:
        if rx.search(text):
            return sid
    return None


# --- recording ------------------------------------------------------------------

def _record(conn, row, job, failure, signatures):
    """Insert one failure and fold it into its issue; returns (issue_id, new)."""
    package, version, excerpt, bits_version, source = failure
    reason = job.get("failure_reason") or ""
    excerpt = redact(excerpt)[-MAX_EXCERPT:]
    fp, keys = fingerprint(package, excerpt, reason)
    when = job.get("finished_at") or row["finished_at"] or row["updated_at"] or ""
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM failures WHERE job_id = ? AND package = ?",
                        (job["id"], package)).fetchone():
            return None, False
        issue = conn.execute("SELECT * FROM issues WHERE fingerprint = ?", (fp,)).fetchone()
        new = issue is None
        if new:
            title = keys[0] if keys else ("reason: " + (reason or "unknown"))
            cur = conn.execute(
                "INSERT INTO issues (fingerprint, package, title, excerpt, first_seen, last_seen,"
                " count, communities, platforms, signature_id, created_ms)"
                " VALUES (?,?,?,?,?,?,0,'[]','[]',?,?)",
                (fp, package, title[:300], excerpt, when, when,
                 match(signatures, excerpt, reason), db.now_ms()))
            issue = conn.execute("SELECT * FROM issues WHERE id = ?", (cur.lastrowid,)).fetchone()
        comms = sorted(set(json.loads(issue["communities"])) | ({row["community"]} - {""}))
        plats = sorted(set(json.loads(issue["platforms"])) | ({row["platform"]} - {""}))
        conn.execute("UPDATE issues SET count = count + 1, communities = ?, platforms = ?,"
                     " first_seen = min(first_seen, ?), last_seen = max(last_seen, ?)"
                     " WHERE id = ?", (json.dumps(comms), json.dumps(plats), when, when,
                                       issue["id"]))
        conn.execute(
            "INSERT INTO failures (job_id, pipeline_id, package, version, community, platform,"
            " job_name, failure_reason, runner, bits_version, excerpt, source, log_url, failed_at,"
            " fingerprint, issue_id, recorded_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], row["id"], package, version, row["community"], row["platform"],
             job.get("name") or "", reason, job.get("runner") or "", bits_version, excerpt,
             source, job.get("web_url") or "", when, fp, issue["id"], db.now_ms()))
        if new:
            events.append(conn, "issue.opened", "issue:%d" % issue["id"], events.WORKER,
                          {"issue": issue["id"], "package": package, "community": row["community"],
                           "title": issue["title"], "signature": issue["signature_id"]})
        events.append(conn, "failure.recorded", "pipeline:%d" % row["id"], events.WORKER,
                      {"pipeline": row["id"], "job": job["id"], "package": package,
                       "community": row["community"], "issue": issue["id"], "source": source})
    return issue["id"], new


def _failures_of(reader, row, job):
    """[(package, version, excerpt, bits_version, source)] for one failed job."""
    raw = reader.job_log_tail(job["id"], LOG_TAIL) or b""
    lines = clean(raw)
    source = "log"
    found = parse(lines)
    truncated = any(_TRUNCATED in ln for ln in lines[-50:])
    if not found and (truncated or job.get("stage") == "build"):
        try:
            art = reader.job_artifact_tail(job["id"], ARTIFACT, LOG_TAIL)
        except Exception as exc:          # the log is still something
            log.warning("artifact %s of job %s: %s", ARTIFACT, job["id"], exc)
            art = None
        if art:
            alines = clean(art)
            afound = parse(alines)
            if afound or truncated:
                lines, found, source = alines, afound, "artifact"
    if found:
        return [(pkg, ver, ex, bv, source) for pkg, ver, ex, bv in found]
    has_text = any(ln.strip() for ln in lines)
    return [(row["package"], row["version"], last_lines(lines), "", source if has_text else "none")]


def collect(conn, reader, budget=COLLECT_PER_PASS):
    """Record the failures of failed build pipelines not collected yet. Returns
    how many were recorded. One pipeline whose logs cannot be read is tried
    again after RETRY_MS and does not hold up the others."""
    now = db.now_ms()
    rows = conn.execute(
        "SELECT * FROM pipelines WHERE is_build = 1 AND outcome = 'failed' AND"
        " (collected_ms = 0 OR (collected_ms < 0 AND -collected_ms <= ?))"
        " ORDER BY id DESC LIMIT ?", (now - RETRY_MS, budget)).fetchall()
    signatures = _compiled(conn)
    recorded = 0
    for row in rows:
        jobs = json.loads(row["jobs"] or "[]")
        latest = {}
        for j in jobs:
            k = (j.get("stage"), j.get("name"))
            if k not in latest or j.get("id", 0) > latest[k].get("id", 0):
                latest[k] = j
        failed = [j for j in latest.values()
                  if j.get("status") == "failed" and not j.get("allow_failure")]
        if not failed:
            # Failed with no failed job to read (deleted, stale, canceled by
            # GitLab): one record for the pipeline itself.
            failed = [{"id": -row["id"], "name": "(pipeline)", "stage": "",
                       "failure_reason": "pipeline:%s" % (row["gl_status"] or "unknown"),
                       "web_url": row["web_url"], "_no_log": True}]
        try:
            for job in failed:
                if conn.execute("SELECT 1 FROM failures WHERE job_id = ?",
                                (job["id"],)).fetchone():
                    continue
                found = ([(row["package"], row["version"], "", "", "none")]
                         if job.get("_no_log") else _failures_of(reader, row, job))
                for f in found:
                    issue_id, _new = _record(conn, row, job, f, signatures)
                    recorded += issue_id is not None
            mark = db.now_ms()
        except Exception as exc:
            # Tried again every RETRY_MS, for a day after the pipeline settled.
            gave_up = row["checked_ms"] and row["checked_ms"] < now - GIVE_UP_MS
            log.warning("failures of pipeline %s: %s%s", row["id"], exc,
                        " (giving up)" if gave_up else "")
            mark = db.now_ms() if gave_up else -db.now_ms()
        with db.transaction(conn):
            conn.execute("UPDATE pipelines SET collected_ms = ? WHERE id = ?", (mark, row["id"]))
    return recorded


def rematch(conn):
    """Give issues without a cause the first active signature that matches one
    of their failures (after signatures change). Returns how many were named."""
    signatures = _compiled(conn)
    named = 0
    for issue in conn.execute("SELECT id FROM issues WHERE signature_id IS NULL").fetchall():
        for f in conn.execute("SELECT excerpt, failure_reason FROM failures WHERE issue_id = ?"
                              " ORDER BY id DESC LIMIT 5", (issue["id"],)).fetchall():
            sid = match(signatures, f["excerpt"], f["failure_reason"])
            if sid:
                with db.transaction(conn):
                    conn.execute("UPDATE issues SET signature_id = ? WHERE id = ?",
                                 (sid, issue["id"]))
                named += 1
                break
    return named


# --- backfill -------------------------------------------------------------------

def backfill(conn, reader, days, lookback_days=7.0, budget=BACKFILL_PER_PASS):
    """Record a page of older failed pipelines (newest first), until *days* back
    or GitLab has no more. They are then settled and collected like any other.
    Returns how many were read; 0 when done or off."""
    if not days:
        return 0
    if reconcile._kv_get(conn, "backfill.done") == "1":
        if days <= float(reconcile._kv_get(conn, "backfill.days", "0") or 0):
            return 0
        with db.transaction(conn):        # asked for more: carry on from where it stopped
            reconcile._kv_set(conn, "backfill.done", "0")
    floor = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    start = reconcile._kv_get(conn, "backfill.before")
    if start is None:                   # where discovery started: it has everything newer
        start = reconcile._iso(datetime.datetime.now(datetime.timezone.utc)
                               - datetime.timedelta(days=lookback_days))
    before = reconcile._when(start)
    page = int(reconcile._kv_get(conn, "backfill.page", "1") or 1)
    got = reader.list_pipelines_before(reconcile._iso(before), page=page, per_page=budget)
    for p in got:
        if p.get("source") in reconcile.BUILD_SOURCES:
            reconcile.record_pipeline(conn, reader, p)
    oldest = min((reconcile._when(p.get("updated_at")) for p in got
                  if reconcile._when(p.get("updated_at"))), default=None)
    with db.transaction(conn):
        if len(got) < budget or (oldest is not None and oldest < floor):
            reconcile._kv_set(conn, "backfill.done", "1")
            reconcile._kv_set(conn, "backfill.days", str(days))
            events.append(conn, "backfill.done", "worker", events.WORKER,
                          {"days": days, "before": reconcile._iso(before)})
        elif oldest is not None:
            nxt = oldest + datetime.timedelta(seconds=1)
            if nxt < before:
                reconcile._kv_set(conn, "backfill.before", reconcile._iso(nxt))
                reconcile._kv_set(conn, "backfill.page", "1")
            else:                       # a page of one second: the next page of it
                reconcile._kv_set(conn, "backfill.before", reconcile._iso(before))
                reconcile._kv_set(conn, "backfill.page", str(page + 1))
    return len(got)


# --- reads for the API ----------------------------------------------------------

def _issue_public(r, sig=None):
    out = {"issue": r["id"], "package": r["package"], "title": r["title"],
           "excerpt": r["excerpt"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
           "count": r["count"], "communities": json.loads(r["communities"]),
           "platforms": json.loads(r["platforms"]), "status": r["status"]}
    out["signature"] = ({"id": sig["id"], "cause": sig["cause"], "action": sig["action"],
                         "safe_to_retry": bool(sig["safe_to_retry"])} if sig else None)
    return out


def _failure_public(f):
    return {"failure": f["id"], "job": f["job_id"], "pipeline": f["pipeline_id"],
            "package": f["package"], "version": f["version"], "community": f["community"],
            "platform": f["platform"], "job_name": f["job_name"],
            "failure_reason": f["failure_reason"], "runner": f["runner"],
            "bits_version": f["bits_version"], "source": f["source"], "log_url": f["log_url"],
            "failed_at": f["failed_at"], "issue": f["issue_id"]}


def _sig(conn, sid):
    return conn.execute("SELECT * FROM signatures WHERE id = ?", (sid,)).fetchone() if sid else None


def _in(communities, column):
    names = sorted(communities)
    return " AND %s IN (%s)" % (column, ",".join("?" * len(names))), names


def _scoped(conn, out, issue_id, communities):
    """An issue as a community admin sees it: counts, where, and the excerpt
    from their own communities' failures only."""
    clause, names = _in(communities, "community")
    occ = conn.execute("SELECT * FROM failures WHERE issue_id = ?%s ORDER BY id DESC" % clause,
                       [issue_id] + names).fetchall()
    if not occ:
        return None
    when = [f["failed_at"] for f in occ if f["failed_at"]]
    keys = key_lines(occ[0]["excerpt"])
    out.update(count=len(occ), excerpt=occ[0]["excerpt"],
               communities=sorted({f["community"] for f in occ} - {""}),
               platforms=sorted({f["platform"] for f in occ} - {""}),
               first_seen=min(when) if when else out["first_seen"],
               last_seen=max(when) if when else out["last_seen"],
               title=(keys[0] if keys else out["title"])[:300])
    return occ


def list_issues(conn, communities=None, package=None, limit=100):
    """Issues seen in the given communities (None = all), last seen first. For a
    scoped caller every field is from their communities' failures only."""
    q, args = "SELECT * FROM issues i WHERE 1=1", []
    if communities is not None:
        if not communities:
            return []
        clause, names = _in(communities, "f.community")
        q += " AND EXISTS (SELECT 1 FROM failures f WHERE f.issue_id = i.id%s)" % clause
        args.extend(names)
    if package:
        q += " AND package = ?"
        args.append(package)
    q += " ORDER BY last_seen DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    out = []
    for r in conn.execute(q, args).fetchall():
        d = _issue_public(r, _sig(conn, r["signature_id"]))
        if communities is not None and _scoped(conn, d, r["id"], communities) is None:
            continue
        out.append(d)
    return out


def get_issue(conn, issue_id, communities=None, limit=50):
    r = conn.execute("SELECT * FROM issues WHERE id = ?", (int(issue_id),)).fetchone()
    if r is None:
        return None
    out = _issue_public(r, _sig(conn, r["signature_id"]))
    if communities is not None:
        if not communities:
            return None
        occ = _scoped(conn, out, r["id"], communities)
        if occ is None:
            return None
        occ = occ[:int(limit)]
    else:
        occ = conn.execute("SELECT * FROM failures WHERE issue_id = ? ORDER BY id DESC LIMIT ?",
                           (r["id"], int(limit))).fetchall()
    out["failures"] = [_failure_public(f) for f in occ]
    return out


def list_failures(conn, communities=None, pipeline=None, limit=100):
    q, args = "SELECT * FROM failures WHERE 1=1", []
    if communities is not None:
        if not communities:
            return []
        clause, names = _in(communities, "community")
        q += clause
        args.extend(names)
    if pipeline is not None:
        q += " AND pipeline_id = ?"
        args.append(int(pipeline))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    out = []
    for f in conn.execute(q, args):
        d = _failure_public(f)
        d["excerpt"] = f["excerpt"]
        out.append(d)
    return out


def list_signatures(conn):
    return [{"id": s["id"], "pattern": s["pattern"], "cause": s["cause"], "action": s["action"],
             "safe_to_retry": bool(s["safe_to_retry"]), "state": s["state"],
             "proposed_by": s["proposed_by"], "accepted_by": s["accepted_by"],
             "version": s["version"]}
            for s in conn.execute("SELECT * FROM signatures ORDER BY id")]
