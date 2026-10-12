# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Certification in shadow (M4): recompute what the bits-manifests CI signed.

For every pipeline of the bits-manifests main branch whose certify job signed,
the worker works out what that job certified and computes the same manifests
itself, with bits' own merge and store check (`certify.build_by_arch`), from the
same inputs:

  * the manifests/ tree at the pipeline's commit (the GitLab archive API);
  * the architectures the push changed, from the tree at the pipeline's
    before_sha, worked out exactly as the CI job's script does (a manifest's
    `sources` depends on which architectures were certified together);
  * the store, through the same probe, with the sha256 of each object kept in
    store_digests: objects never change, so only new ones are hashed.

It compares the result with the manifests the job signed (its artifacts, or the
store's copy while it still carries the pipeline's commit), once their
signature verifies against the trust anchor. Timestamps and certified_by are
left out of the comparison; everything else must be equal. Each architecture is
a sign run (mode 'shadow') and a shadow.match or shadow.mismatch event. Nothing
is signed, committed or written to the store.

Where git and the archive differ, the two can disagree, and the comparison
shows it: git lists only the new path of a renamed BOM; a change of file mode
alone, symlinks and .gitattributes export rules are not seen in the archive.
None of these occur in bits-manifests today. An outage of GitLab or S3 fails the
step (tried again next pass) without counting against a certification.

This runs on its own thread with its own connection (`Runner`): the first
certification of an architecture hashes every object in it, which takes as long
as the CI job does, and must not hold up the worker's actions.
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import zipfile

from . import db, events

log = logging.getLogger("bits_orchestrator.shadow")
_SHA = re.compile(r"[0-9a-f]{7,64}")

VOLATILE = ("created_at", "expires", "certified_at", "certified_by")
MAX_ATTEMPTS = 8
KEEP_TREES = 4
MAX_TREE = 512 << 20          # the extracted manifests/ tree
MAX_SIGNED = 64 << 20         # the signed manifests in a job's artifacts
_LIST = 20


class Skip(Exception):
    """Nothing to compare for this certification (with the reason)."""


def transient(e):
    """An error of GitLab, S3 or the network rather than of this certification:
    the step fails and is tried again, without counting against it."""
    from .. import forge_ops
    if isinstance(e, forge_ops.ForgeError):
        return e.status == 0 or e.status == 429 or e.status >= 500
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0
        code = str((resp.get("Error") or {}).get("Code", ""))
        return status >= 500 or status == 429 or code in ("SlowDown", "Throttling",
                                                          "RequestTimeout", "InternalError",
                                                          "ServiceUnavailable")
    mod = type(e).__module__ or ""
    return isinstance(e, (OSError, TimeoutError)) or mod.startswith(("botocore", "urllib3",
                                                                     "requests"))


# --- inputs -------------------------------------------------------------------

def _safe_member(m):
    name = m.name
    return (m.isfile() and not name.startswith("/") and ".." not in name.split("/")
            and name.endswith(".json") and "/manifests/" in "/" + name)


def tree(reader, sha, cache_dir):
    """The manifests/ directory of the bits-manifests tree at *sha*, extracted
    under *cache_dir* (kept for the next certification, whose before_sha it
    usually is). Only regular .json files under manifests/ are extracted."""
    if not _SHA.fullmatch(sha or ""):
        raise Skip("not a commit sha: %r" % (sha,))
    dest = os.path.join(cache_dir, sha)
    done = os.path.join(dest, ".complete")
    if os.path.exists(done):
        os.utime(dest)                    # recently used: pruned last
        return os.path.join(dest, "manifests")
    data = reader.repository_archive(sha, path="manifests")
    if data is None:
        raise Skip("commit %s is not in bits-manifests" % sha[:12])
    shutil.rmtree(dest, ignore_errors=True)
    root = os.path.join(dest, "manifests")
    os.makedirs(root)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar:
            if not _safe_member(m):
                continue
            total += m.size
            if total > MAX_TREE:
                raise RuntimeError("the manifests tree at %s is over %d bytes" % (sha[:12], MAX_TREE))
            # <archive top dir>/manifests/<group>/<file>.json -> manifests/<group>/...
            rel = m.name.split("/manifests/", 1)[1] if "/manifests/" in m.name else m.name[len("manifests/"):]
            out = os.path.join(root, rel)
            if not os.path.realpath(out).startswith(os.path.realpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with tar.extractfile(m) as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)
    open(done, "w").close()
    return root


def prune(cache_dir, keep=KEEP_TREES, protect=()):
    """Keep the *keep* most recently used trees, and those in *protect*."""
    try:
        dirs = [os.path.join(cache_dir, d) for d in os.listdir(cache_dir)]
    except OSError:
        return
    dirs = sorted((d for d in dirs if os.path.isdir(d)), key=os.path.getmtime, reverse=True)
    for d in dirs[keep:]:
        if os.path.basename(d) not in protect:
            shutil.rmtree(d, ignore_errors=True)


def _files(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith(".json"):
                p = os.path.join(dirpath, n)
                out[os.path.relpath(p, root).replace(os.sep, "/")] = p
    return out


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _bom_arch(text):
    """The CI job's reading of one BOM's architecture: None when it cannot tell."""
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    s = {e.get("effective_architecture") or "shared"
         for e in m.get("packages") or []
         if isinstance(e, dict) and e.get("hash") and e.get("tarball_sha256")}
    return m.get("effective_architecture") or (s.pop() if len(s) == 1 else None)


def changed_archs(new_root, old_root):
    """(changed, archs) as the CI job computes them for `git diff before HEAD --
    manifests/`: the .json files that differ, and the architectures to certify,
    or None for every architecture (a file whose architecture it cannot tell)."""
    new, old = _files(new_root), _files(old_root) if old_root else {}
    changed = sorted(p for p in set(new) | set(old)
                     if p not in new or p not in old or _read(new[p]) != _read(old[p]))
    archs, ok = set(), True
    for p in changed:
        got = _bom_arch(_read(new[p]).decode("utf-8", "replace")) if p in new else (
            _bom_arch(_read(old[p]).decode("utf-8", "replace")))
        if got:
            archs.add(got)
        else:
            ok = False
    return changed, (sorted(archs) if ok and archs else None)


def signed_outputs(reader, store, prefix, job_id, sha, archs):
    """{arch: (manifest bytes, signature envelope, source)} the certify job
    signed: from its artifacts when it kept them, else the store's copy of each
    architecture in *archs* (None: every one in the store), marked 'superseded'
    once a later certification replaced it."""
    out = {}
    data = reader.job_artifacts(job_id) if job_id else None
    if data:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            wanted = [i for i in z.infolist() if i.filename.rsplit("/", 1)[-1].startswith(
                "common-manifest-")]
            if sum(i.file_size for i in wanted) > MAX_SIGNED:
                raise RuntimeError("job %s: signed manifests over %d bytes" % (job_id, MAX_SIGNED))
            for n in sorted(names):
                base = n.rsplit("/", 1)[-1]
                if base.startswith("common-manifest-") and base.endswith(".json"):
                    arch = base[len("common-manifest-"):-len(".json")]
                    sig = z.read(n + ".sig") if n + ".sig" in names else b""
                    out[arch] = (z.read(n), _envelope(sig), "artifacts")
        if out:
            return out
    if store is None:
        return out
    s3, bucket = store
    if archs is None:
        archs = _store_archs(s3, bucket, prefix)
    for arch in archs:
        key = "%s-%s.json" % (prefix, arch)
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            sig = s3.get_object(Bucket=bucket, Key=key + ".sig")["Body"].read()
        except Exception as e:
            if _missing(e):
                continue
            raise
        try:
            commit = json.loads(body).get("source_commit")
        except (ValueError, AttributeError):
            commit = None
        out[arch] = (body, _envelope(sig), "store" if commit == sha else "superseded")
    return out


def _store_archs(s3, bucket, prefix):
    archs, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix + "-"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            k = o["Key"]
            if k.endswith(".json"):
                archs.append(k[len(prefix) + 1:-len(".json")])
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return sorted(archs)


def _missing(e):
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "404", "NotFound")


def _envelope(raw):
    try:
        env = json.loads(raw or b"null")
    except ValueError:
        return None
    return env if isinstance(env, dict) else None


# --- comparing ----------------------------------------------------------------

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content(manifest):
    return {k: v for k, v in manifest.items() if k not in VOLATILE}


def digest(manifest):
    return hashlib.sha256(_canonical(content(manifest)).encode()).hexdigest()


def _label(p):
    return "%s@%s-%s %s" % (p.get("package", "?"), p.get("version", "?"),
                            p.get("revision", "?"), str(p.get("hash", ""))[:12])


def diff(ours, signed):
    """What differs between our manifest and the signed one, capped."""
    key = lambda p: (p.get("effective_architecture") or "", p.get("hash"))  # noqa: E731
    a = {key(p): p for p in ours.get("packages") or [] if isinstance(p, dict)}
    b = {key(p): p for p in signed.get("packages") or [] if isinstance(p, dict)}
    only_ours = sorted(set(a) - set(b), key=str)
    only_signed = sorted(set(b) - set(a), key=str)
    changed = sorted((k for k in set(a) & set(b) if _canonical(a[k]) != _canonical(b[k])), key=str)
    out = {}
    if only_ours:
        out["only_ours"] = {"count": len(only_ours), "first": [_label(a[k]) for k in only_ours[:_LIST]]}
    if only_signed:
        out["only_signed"] = {"count": len(only_signed),
                              "first": [_label(b[k]) for k in only_signed[:_LIST]]}
    if changed:
        fields = sorted({f for k in changed for f in set(a[k]) | set(b[k])
                         if a[k].get(f) != b[k].get(f)})
        out["changed"] = {"count": len(changed), "fields": fields,
                          "first": [_label(a[k]) for k in changed[:_LIST]]}
    for f in sorted((set(ours) | set(signed)) - set(VOLATILE) - {"packages"}):
        x, y = ours.get(f), signed.get(f)
        if x == y:
            continue
        if isinstance(x, list) and isinstance(y, list):
            out[f] = {"only_ours": sorted(map(str, set(map(str, x)) - set(map(str, y))))[:_LIST],
                      "only_signed": sorted(map(str, set(map(str, y)) - set(map(str, x))))[:_LIST],
                      "order": sorted(map(str, x)) == sorted(map(str, y))}
        else:
            out[f] = {"ours": x, "signed": y}
    return out


# --- the digest cache -----------------------------------------------------------

class DigestCache:
    """store_digests as the probe's cache (see certify.s3_probe). It trusts an
    object's key, ETag and size to stand for its bytes: fine for a comparison,
    while CI still hashes every object; signing (M8) must hash for itself."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, key, etag, size):
        r = self.conn.execute("SELECT etag, size, sha256 FROM store_digests WHERE key = ?",
                              (key,)).fetchone()
        if r and r["etag"] == etag and r["size"] == int(size):
            return r["sha256"]
        return None

    def put(self, key, etag, size, sha256):
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO store_digests (key, etag, size, sha256, checked_ms) VALUES (?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET etag = excluded.etag, size = excluded.size,"
                " sha256 = excluded.sha256, checked_ms = excluded.checked_ms",
                (key, etag, int(size), sha256, db.now_ms()))


class _RememberOnly:
    """A digest cache that is written, never read: the object is hashed."""

    def __init__(self, cache):
        self.cache = cache

    def get(self, key, etag, size):
        return None

    def put(self, key, etag, size, sha256):
        self.cache.put(key, etag, size, sha256)


# --- the steps ------------------------------------------------------------------

def _subject(pid):
    return "certification:%d" % int(pid)


def discover(conn, reader, settings, now_ms=None):
    """Record the bits-manifests main pipelines that finished, and look again at
    one not yet compared whose pipeline changed (a retried certify job)."""
    now = now_ms or db.now_ms()
    since = now - int(settings.orch_shadow_lookback_days * 86400 * 1000)
    first = conn.execute("SELECT value FROM kv WHERE key = 'shadow.since_ms'").fetchone()
    if first is None:
        with db.transaction(conn):
            conn.execute("INSERT INTO kv (key, value) VALUES ('shadow.since_ms', ?)", (str(since),))
        floor = since
    else:
        floor = int(first["value"])
    n = 0
    for p in reader.finished_pipelines(settings.orch_manifests_branch):
        if not isinstance(p, dict) or p.get("source") not in ("push", "web", "schedule"):
            continue
        created = _ms(p.get("created_at"))
        if created is not None and created < floor:
            continue
        pid, updated = int(p["id"]), str(p.get("updated_at") or "")
        row = conn.execute("SELECT state, updated_at FROM certifications WHERE pipeline_id = ?",
                           (pid,)).fetchone()
        if row is None:
            with db.transaction(conn):
                conn.execute("INSERT OR IGNORE INTO certifications (pipeline_id, sha, updated_at,"
                             " created_ms, updated_ms) VALUES (?,?,?,?,?)",
                             (pid, str(p.get("sha") or ""), updated, now, now))
            n += 1
        elif row["updated_at"] != updated and row["state"] in ("skipped", "failed"):
            # A retried certify job: look again.
            with db.transaction(conn):
                conn.execute("UPDATE certifications SET state = 'pending', reason = '',"
                             " attempts = 0, next_ms = 0, updated_at = ?, updated_ms = ?"
                             " WHERE pipeline_id = ?", (updated, now, pid))
            n += 1
    return n


def _ms(iso):
    if not iso:
        return None
    import datetime
    try:
        return int(datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _set(conn, pid, **fields):
    fields["updated_ms"] = db.now_ms()
    cols = ", ".join("%s = ?" % k for k in fields)
    conn.execute("UPDATE certifications SET %s WHERE pipeline_id = ?" % cols,
                 list(fields.values()) + [pid])


def compare_one(conn, reader, store, settings, cache_dir, build_by_arch=None,
                trusted=None, probe_factory=None, upto=None, console=None):
    """Compare the oldest due certification (with a pipeline id up to *upto*,
    the newest the certified set has followed, when given). Returns its
    pipeline id, or None when none is due."""
    now = db.now_ms()
    row = conn.execute("SELECT * FROM certifications WHERE state = 'pending' AND next_ms <= ?"
                       " AND pipeline_id <= ? ORDER BY pipeline_id LIMIT 1",
                       (now, upto if upto is not None else 1 << 62)).fetchone()
    if row is None:
        return None
    pid = row["pipeline_id"]
    try:
        _compare(conn, row, reader, store, settings, cache_dir, build_by_arch, trusted,
                 probe_factory, console)
    except Skip as s:
        with db.transaction(conn):
            _set(conn, pid, state="skipped", reason=str(s)[:500])
    except Exception as e:
        if transient(e) or _transient_cause(e):
            raise                         # the step fails; this one is tried again as it is
        attempts = row["attempts"] + 1
        reason = "%s: %s" % (type(e).__name__, str(e)[:400])
        with db.transaction(conn):
            if attempts >= MAX_ATTEMPTS:
                _set(conn, pid, state="failed", attempts=attempts, reason=reason)
                events.append(conn, "shadow.failed", _subject(pid), events.WORKER,
                              {"error": reason, "attempts": attempts})
            else:
                _set(conn, pid, attempts=attempts, reason=reason,
                     next_ms=now + min(3600, 60 * 2 ** attempts) * 1000)
        log.warning("certification %s: %s (attempt %d)", pid, reason, attempts)
    return pid


def _transient_cause(e):
    """certify wraps store errors in CertifyError: look at the cause."""
    c = e.__cause__ or e.__context__
    return c is not None and transient(c)


def _certify_job(jobs, name):
    mine = [j for j in jobs if isinstance(j, dict) and j.get("name") == name]
    return max(mine, key=lambda j: int(j.get("id") or 0)) if mine else None


def _compare(conn, row, reader, store, settings, cache_dir, build_by_arch, trusted,
             probe_factory, console=None):
    pid = row["pipeline_id"]
    pipeline = reader.get_pipeline(pid)
    job = _certify_job(reader.pipeline_jobs(pid), settings.orch_certify_job)
    status = (job or {}).get("status") or ""
    with db.transaction(conn):
        _set(conn, pid, job_id=(job or {}).get("id"), job_status=status,
             updated_at=str(pipeline.get("updated_at") or row["updated_at"]))
        # Whether that version of the certified set was signed (M8 needs to know).
        conn.execute("UPDATE set_versions SET job_status = ? WHERE pipeline_id = ?",
                     (status or "none", pid))
    if job is None:
        raise Skip("no %s job" % settings.orch_certify_job)
    if status != "success":
        raise Skip("%s %s" % (settings.orch_certify_job, status))
    sha = str(pipeline.get("sha") or row["sha"])
    before = str(pipeline.get("before_sha") or "")
    if not before.strip("0"):
        parents = (reader.commit(sha) or {}).get("parent_ids") or []
        before = parents[0] if parents else ""
    try:
        new_root = tree(reader, sha, cache_dir)
        old_root = tree(reader, before, cache_dir) if before else None
        _compare_trees(conn, pid, sha, before, new_root, old_root, job, reader, store,
                       settings, build_by_arch, trusted, probe_factory, console)
    finally:
        prune(cache_dir, protect=(sha, before))


def _compare_trees(conn, pid, sha, before, new_root, old_root, job, reader, store, settings,
                   build_by_arch, trusted, probe_factory, console=None):
    from bits_helpers import certify, trust
    build_by_arch = build_by_arch or certify.build_by_arch
    changed, archs = changed_archs(new_root, old_root)
    with db.transaction(conn):
        _set(conn, pid, sha=sha, before_sha=before, archs=json.dumps(archs or []))
    if not changed:
        raise Skip("no manifests changed")
    signed = signed_outputs(reader, store, settings.orch_manifest_prefix, job["id"], sha, archs)
    if not signed:
        raise Skip("the signed manifests are gone (no artifacts, and the store has newer ones)")
    trusted = trust.load_trusted_keys() if trusted is None else trusted
    parsed, key_id = {}, None
    for arch, (body, env, source) in signed.items():
        kid = trust.verify_bytes(body, env, trusted) if env else None
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        parsed[arch] = (doc if isinstance(doc, dict) else None, kid, source)
        key_id = key_id or kid
    if probe_factory is None:
        s3, bucket = store
        cache = DigestCache(conn)
        base = certify.s3_probe(s3, bucket, settings.orch_default_arch, cache=cache)
        # The objects of the build being certified are hashed afresh (and the
        # digests remembered): a certification must not take them on trust.
        fresh_base = certify.s3_probe(s3, bucket, settings.orch_default_arch,
                                      cache=_RememberOnly(cache))
    else:
        base = probe_factory(conn)
        base, fresh_base = base if isinstance(base, tuple) else (base, base)
    memo, hashed = {}, set()

    def probe_for(fresh):
        def probe(arch, h, tarball=None):
            # The tree's, the set's and the replay's computations ask about the
            # same objects: once each, the fresh ones hashed once.
            k = (arch, h, tarball)
            if h in fresh and k not in hashed:
                memo[k] = fresh_base(arch, h, tarball)
                hashed.add(k)
            elif k not in memo:
                memo[k] = base(arch, h, tarball)
            return memo[k]
        return probe

    def compute(root, only=archs, fresh=()):
        try:
            return dict(build_by_arch(root, probe=probe_for(fresh), source_commit=sha,
                                      only_archs=only,
                                      key_id=(lambda: key_id) if key_id else None)), None
        except certify.CertifyError as e:
            if _transient_cause(e):
                raise                     # the store failed to answer, not this certification
            return None, ("*", "mismatch", "", 0, "", "",
                          {"error": "the worker refuses what was signed: %s" % str(e)[:1000]})

    # The worker's own certification of the build (M8a), first: it hashes the
    # build's objects, which the tree's and the set's computations then reuse.
    from . import replay
    replay_runs = replay.runs(conn, settings, console, store, sha, new_root, old_root, job,
                              parsed, key_id, compute)
    ours, refused = compute(new_root)
    runs = [refused] if refused else judge(ours, parsed)
    # The same from the certified set as of this certification (M5): the set
    # must give exactly what the tree gives.
    from . import certified
    version = certified.version_of(conn, pid)
    if version is None:
        set_runs = [("*", "unsynced", "", 0, "", "",
                     {"error": "the certified set did not follow this pipeline"})]
    else:
        # The set as of this pipeline must hold exactly the tree's BOMs.
        have, want = certified.members(conn, version), {
            p: hashlib.sha256(_read(f)).hexdigest() for p, f in _files(new_root).items()}
        if have != want:
            set_runs_tree = [("(tree)", "mismatch", "", len(have), certified.digest_of(have),
                              certified.digest_of(want), {
                                  "only_set": sorted(set(have) - set(want))[:_LIST],
                                  "only_tree": sorted(set(want) - set(have))[:_LIST],
                                  "different": sorted(p for p in set(have) & set(want)
                                                      if have[p] != want[p])[:_LIST]})]
        else:
            set_runs_tree = [("(tree)", "match", "", len(have), certified.digest_of(have),
                              certified.digest_of(want), {})]
        set_root = certified.materialize(conn, version, os.path.join(
            os.path.dirname(os.path.abspath(new_root)) + ".set"))
        try:
            mine, refused = compute(set_root)
        finally:
            shutil.rmtree(os.path.dirname(set_root), ignore_errors=True)
        set_runs = set_runs_tree + ([refused] if refused else judge(mine, parsed))
    with db.transaction(conn):
        for mode, rows in (("shadow", runs), ("set", set_runs), (replay.MODE, replay_runs)):
            for arch, state, source, n, ours_sha, signed_sha, d in rows:
                _record(conn, pid, sha, mode, arch, state, source, n, ours_sha, signed_sha, d)
        _set(conn, pid, state="compared", reason="")


def judge(ours, parsed):
    """Sign runs for each architecture: our manifests against the signed ones."""
    runs = []
    for arch in sorted(set(ours) | set(parsed)):
        mine = ours.get(arch)
        doc, kid, source = parsed.get(arch, (None, None, ""))
        if source == "superseded":
            # The store's copy is of a later (or, for an architecture this run
            # did not sign, an earlier) certification: not this one's.
            if mine is not None:
                runs.append((arch, "superseded", source, len(mine["packages"]),
                             digest(mine), "", {}))
            continue
        if mine is None:
            runs.append((arch, "mismatch", source, 0, "", digest(doc) if doc else "",
                         {"error": "signed, but not in the worker's result"}))
            continue
        if arch not in parsed:
            runs.append((arch, "mismatch", "", len(mine["packages"]), digest(mine), "",
                         {"error": "in the worker's result, but not signed"}))
            continue
        if doc is None or kid is None:
            runs.append((arch, "unverified", source, len(mine["packages"]), digest(mine),
                         "", {"error": "the signed manifest does not verify"}))
            continue
        d, a, b = diff(mine, doc), digest(mine), digest(doc)
        if a != b and not d:
            d = {"order": "the same packages and fields, in another order"}
        runs.append((arch, "match" if a == b else "mismatch", source,
                     len(mine["packages"]), a, b, d))
    return runs


def _record(conn, pid, sha, mode, arch, state, source, n, ours_sha, signed_sha, d):
    conn.execute(
        "INSERT INTO sign_runs (certification, mode, arch, state, source, source_commit,"
        " packages, manifest_sha256, signed_sha256, diff, created_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(certification, mode, arch) DO UPDATE SET state = excluded.state,"
        " source = excluded.source, packages = excluded.packages,"
        " manifest_sha256 = excluded.manifest_sha256,"
        " signed_sha256 = excluded.signed_sha256, diff = excluded.diff,"
        " created_ms = excluded.created_ms",
        (pid, mode, arch, state, source, sha, n, ours_sha, signed_sha, _canonical(d), db.now_ms()))
    prefix = {"shadow": "shadow.", "set": "shadow.set_"}.get(mode, mode + ".")
    if state == "unverified" and mode == "shadow":
        events.append(conn, "shadow.unverified", _subject(pid), events.WORKER,
                      {"arch": arch, "source": source, "commit": sha})
    # The tree's result is an event either way; the set's only when it differs.
    if (state == "mismatch" or (state == "match" and mode in ("shadow", "replay"))
            or state in ("unsynced", "refused", "missing", "error")):
        summary = {k: (v.get("count") if isinstance(v, dict) and "count" in v else True)
                   for k, v in d.items()}
        if "error" in d:
            summary = {"error": d["error"][:500]}
        events.append(conn, prefix + state, _subject(pid), events.WORKER,
                      {"arch": arch, "packages": n, "source": source, "commit": sha,
                       **({"differs": summary} if state != "match" else {})})


def runs(conn, limit=50, state=""):
    """Certifications, newest first, each with its sign runs."""
    limit = max(1, min(int(limit), 500))
    if state:
        rows = conn.execute("SELECT * FROM certifications WHERE state = ?"
                            " ORDER BY pipeline_id DESC LIMIT ?", (state, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM certifications ORDER BY pipeline_id DESC LIMIT ?",
                            (limit,)).fetchall()
    out = []
    for r in rows:
        c = {k: r[k] for k in r.keys()}
        c["archs"] = json.loads(c["archs"] or "[]")
        c["sign_runs"] = [dict({k: s[k] for k in s.keys()}, diff=json.loads(s["diff"] or "{}"))
                          for s in conn.execute("SELECT * FROM sign_runs WHERE certification = ?"
                                                " ORDER BY arch", (r["pipeline_id"],))]
        out.append(c)
    return out


def summary(conn, since_ms=0, mode="shadow"):
    """Counts of sign runs by state since *since_ms*: the gate's numbers. *mode*
    'shadow' (from the git tree, M4), 'set' (from the certified set, M5) or
    'replay' (the worker's own certification of the build, M8a)."""
    return {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM sign_runs WHERE mode = ? AND created_ms >= ?"
        " GROUP BY state", (mode, int(since_ms)))}


# --- the thread -----------------------------------------------------------------

class Runner(threading.Thread):
    """Discover and compare certifications on a thread of its own, with its own
    connection, every *interval* seconds."""

    def __init__(self, path, settings, deps, interval, stop, note):
        """*note(conn, step, error)* records the step's failure or recovery."""
        super().__init__(name="shadow", daemon=True)
        self.path, self.settings, self.deps = path, settings, deps
        self.interval, self.stop_event, self.note = interval, stop, note
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "shadow")

    def run(self):
        while not self.stop_event.is_set():
            try:
                conn = db.connect(self.path)
            except Exception:
                log.exception("shadow: cannot open %s; trying again", self.path)
                self.stop_event.wait(max(60.0, self.interval))
                continue
            try:
                while not self.stop_event.is_set():
                    began = time.monotonic()
                    self.once(conn)
                    self.stop_event.wait(max(1.0, self.interval - (time.monotonic() - began)))
            finally:
                conn.close()

    def _console(self):
        """The build project, for a build's ui-config.yaml (None: not readable)."""
        try:
            return self.deps.reader()
        except Exception as e:
            log.warning("replay: the build project cannot be read: %s", e)
            return None

    def once(self, conn):
        try:
            reader = self.deps.manifests_reader()
            store = self.deps.store()
            if reader is None:
                return
            if store is None:
                raise RuntimeError("BITS_ORCH_STORE is not set: certifications cannot be"
                                   " checked against the store")
            discover(conn, reader, self.settings)
            from . import certified
            certified.sync(conn, reader, self.cache_dir, branch=self.settings.orch_manifests_branch)
            upto = certified.followed(conn) or 0
            console = self._console()
            while not self.stop_event.is_set():
                if compare_one(conn, reader, store, self.settings, self.cache_dir,
                               upto=upto, console=console) is None:
                    break
            certified.match_store(conn, store)
        except Exception as exc:
            log.exception("shadow certification failed")
            self.note(conn, "shadow", "%s: %s" % (type(exc).__name__, exc))
        else:
            self.note(conn, "shadow", None)
