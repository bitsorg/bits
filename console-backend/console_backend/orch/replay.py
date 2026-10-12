# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's own certification of a build, replayed beside the CI's (M8a).

When the shadow (M4) compares a certification of the bits-manifests CI, the
worker also works out what it would have certified for the same build, on its
own terms, and compares that with what the CI signed (sign runs mode 'replay',
the evidence for the M8a gate):

  * the build: the one the push added BOMs for, by their source_pipeline_id, as
    the CI job finds it (one build per push; a CLI build, a delete-only push or
    BOMs from several builds are not the worker's to certify in M8a);
  * its approval: a console pre-approval of that pipeline, approved before the
    CI job started and neither expired nor revoked then. Nothing else approves
    a certification: no pre-approval, no certification;
  * its BOMs: the build's own copies in the store (the keys its build index
    names, under MANIFESTS/<build_id>/), not the merge request's, each matched
    to the MR's file of the same architecture and put at that file's path, so
    the inputs are ordered as the CI's are;
  * its group: the build's certify group (certify_group in the community's
    ui-config.yaml at the pipeline's commit, else the community), which every
    BOM's directory must be;
  * the checks bits-manifests does: one architecture per BOM, the pre-approver
    an admin of every group in the result (the backend's admin policy, not
    ADMINS), the signing key allowed for every group by the key policy (and no
    key policy is a refusal), the store holding the bytes (each object hashed
    by the worker itself once, its digest kept while its key, ETag and size
    stay the same: the digest cache the shadow uses);
  * the baseline: the tree the CI certified, the build's files replaced by
    the store's copies, and the architectures the CI certified, so the result
    can equal the CI's exactly.

Nothing is signed or written. A refusal, or a build the worker does not get to,
is a sign run too ('refused', 'missing'), with the reason.
"""

import json
import logging
import os
import shutil

from . import certified, db, shadow

log = logging.getLogger("bits_orchestrator.replay")
MODE = "replay"
_UI_MAX = 1 << 20


class Refuse(Exception):
    """The worker would not certify this build (the reason is the message)."""


class Missing(Exception):
    """The worker has nothing to certify this build from (the reason)."""


def _docs(root, paths):
    out = {}
    for p in paths:
        try:
            out[p] = json.loads(shadow._read(os.path.join(root, p)))
        except ValueError:
            out[p] = None
    return out


def build_of(new_root, old_root):
    """(pipeline id, {path: MR BOM}) of the build a push added BOMs for, as the
    CI job's BUILD_INFO decides; Missing for what the worker leaves alone."""
    new, old = shadow._files(new_root), shadow._files(old_root) if old_root else {}
    changed = sorted(p for p in set(new) | set(old)
                     if p not in new or p not in old
                     or shadow._read(new[p]) != shadow._read(old[p]))
    added = [p for p in changed if p in new]
    if not added:
        raise Missing("a delete-only push (revocation)")
    docs = _docs(new_root, added)
    sids, bids = set(), set()
    for d in docs.values():
        if not isinstance(d, dict):
            continue
        sid = str(d.get("source_pipeline_id") or "").strip()
        if sid:
            sids.add(sid)
        else:
            bids.add(str(d.get("build_id") or "").strip())
    if sids and bids or len(sids) > 1:
        raise Missing("BOMs from more than one build in one push")
    if not sids:
        raise Missing("a CLI build (bits certify): not certified by the worker in M8a")
    sid = next(iter(sids))
    if not sid.isdigit():
        raise Missing("source_pipeline_id %r is not a pipeline id" % sid)
    return int(sid), docs


def preapproval_at(conn, build_id, at_ms):
    """The console pre-approval of *build_id* in force at *at_ms*, or None."""
    return conn.execute(
        "SELECT * FROM approvals WHERE kind = 'preapproval' AND subject = ? AND via = 'console'"
        " AND created_ms <= ? AND expires_ms > ? AND (revoked_ms IS NULL OR revoked_ms > ?)"
        " ORDER BY id DESC LIMIT 1", (str(build_id), at_ms, at_ms, at_ms)).fetchone()


def certify_group(console, pipeline):
    """The group the build's certify job uses: its community's certify_group in
    ui-config.yaml at the pipeline's commit, else the community lowercased (as
    bits-certify.sh does)."""
    try:
        variables = json.loads(pipeline["variables"] or "{}")
    except ValueError:
        variables = {}
    community = str(variables.get("COMMUNITY") or pipeline["community"] or "").strip()
    if not community:
        raise Refuse("the build pipeline has no COMMUNITY")
    if "/" in community or community.startswith("."):
        raise Refuse("COMMUNITY %r is not a directory name" % community)
    group = ""
    if console is not None and pipeline["sha"]:
        import yaml
        raw = console.file_raw("communities/%s/ui-config.yaml" % community, pipeline["sha"],
                               max_bytes=_UI_MAX)
        if raw is not None:
            try:
                cfg = yaml.safe_load(raw) or {}
            except yaml.YAMLError as e:
                raise Refuse("communities/%s/ui-config.yaml does not parse: %s"
                             % (community, str(e)[:200]))
            group = str((cfg.get("certify_group") if isinstance(cfg, dict) else "") or "").strip()
    elif console is None:
        raise Missing("no reader for the build project (its ui-config.yaml)")
    return (group or community).lower()


def store_boms(conn, store, mr_docs, pid):
    """{path: (key, the build's store BOM for that MR file's architecture,
    whether it differs from the MR's)}, from the keys the pipeline's build
    index names (a build id is shared by every run of the same release: its
    store directory holds other pipelines' copies too)."""
    from bits_helpers import certify
    s3, bucket = store
    out, by_build = {}, {}
    for path, doc in sorted(mr_docs.items()):
        if not isinstance(doc, dict):
            raise Refuse("%s is not a JSON object" % path)
        try:
            arch = certify.bom_architecture(doc) or ""
        except certify.CertifyError as e:
            raise Refuse("%s: %s" % (path, e))
        bid = str(doc.get("build_id") or "")
        if bid not in by_build:
            # (Recorded when the build was first seen: a publish job retried in
            # the same pipeline rewrites the same keys, almost always with the
            # same content; a difference shows as store_differs_from_mr.)
            row = conn.execute("SELECT bom_keys FROM builds WHERE pipeline_id = ? AND build_id = ?",
                               (pid, bid)).fetchone()
            if row is None:
                raise Missing("build %s of pipeline %s is not in the store's build index" % (bid, pid))
            docs = []
            for key in json.loads(row["bom_keys"] or "[]"):
                try:
                    sdoc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
                except ValueError:
                    continue
                except Exception as e:
                    if shadow._missing(e):
                        continue                # gone from the store
                    raise                       # transient: tried again; else an error run
                if isinstance(sdoc, dict):
                    docs.append((key, sdoc))
            by_build[bid] = docs
        hits = []
        for key, sdoc in by_build[bid]:
            try:
                if (certify.bom_architecture(sdoc) or "") == arch:
                    hits.append((key, sdoc))
            except certify.CertifyError:
                continue
        if not hits:
            raise Missing("no store copy of %s's BOM for %s" % (bid or path, arch or "shared"))
        if len(hits) > 1:
            raise Missing("%d store BOMs of build %s for %s" % (len(hits), bid, arch or "shared"))
        key, sdoc = hits[0]
        if str(sdoc.get("source_pipeline_id") or "") != str(pid):
            raise Refuse("the store's BOM %s is of pipeline %s, not %s"
                         % (key, sdoc.get("source_pipeline_id"), pid))
        out[path] = (key, sdoc, certified._strip(sdoc) != certified._strip(doc))
    return out


def _groups(manifests):
    """The groups of the result, as the sign path reads them (main._groups_of):
    an entry without one, or a manifest without entries, is 'common'."""
    groups = set()
    for m in manifests.values():
        pkgs = (m or {}).get("packages") or []
        if not pkgs:
            groups.add("common")
        for p in pkgs:
            g = p.get("group") if isinstance(p, dict) else None
            groups.add(g if isinstance(g, str) and g else "common")
    return sorted(groups) or ["common"]


def runs(conn, settings, console, store, sha, new_root, old_root, job, parsed, key_id,
         compute):
    """The replay's sign runs for this certification. *compute(root)* is the
    shadow's computation (the CI's architectures)."""
    try:
        return _runs(conn, settings, console, store, new_root, old_root, job, parsed,
                     key_id, compute)
    except Missing as e:
        return [("*", "missing", "", 0, "", "", {"error": str(e)[:1000]})]
    except Refuse as e:
        return [("*", "refused", "", 0, "", "", {"error": str(e)[:1000]})]
    except Exception as e:
        if shadow.transient(e) or shadow._transient_cause(e):
            raise                       # GitLab or the store: the step is tried again
        log.warning("replay of %s: %s", sha[:12], e)
        return [("*", "error", "", 0, "", "", {"error": "%s: %s" % (type(e).__name__,
                                                                   str(e)[:1000])})]


def _runs(conn, settings, console, store, new_root, old_root, job, parsed, key_id, compute):
    from .. import authz
    from bits_helpers import trust
    if key_id is None:
        raise Missing("the CI's signed manifests do not verify against the trust anchor")
    pid, mr_docs = build_of(new_root, old_root)
    pipeline = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
    if pipeline is None:
        raise Missing("build pipeline %s is not recorded" % pid)
    at = shadow._ms((job or {}).get("started_at") or (job or {}).get("created_at")) or db.now_ms()
    pre = preapproval_at(conn, pid, at)
    if pre is None:
        raise Refuse("no console pre-approval of pipeline %s in force when the CI signed" % pid)
    user = pre["approver"]
    group = certify_group(console, pipeline)
    wrong = sorted({p.split("/")[0] for p in mr_docs} - {group})
    if wrong:
        raise Refuse("BOMs under %s, but the build certifies into %s" % (", ".join(wrong), group))
    boms = store_boms(conn, store, mr_docs, pid)
    archs = sorted({shadow._bom_arch(json.dumps(d)) or "?" for _k, d, _x in boms.values()})
    if "?" in archs:
        raise Refuse("a store BOM whose architecture the CI cannot tell")
    # The tree the CI certified (its deletions applied), the build's files
    # replaced by the store's copies.
    root = os.path.join(os.path.dirname(os.path.abspath(new_root)) + ".replay", "manifests")
    shutil.rmtree(os.path.dirname(root), ignore_errors=True)
    try:
        shutil.copytree(new_root, root)
        for path, (_key, doc, _x) in boms.items():
            dest = os.path.join(root, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as fh:
                json.dump(dict(doc, certified_by=[user]), fh, indent=1)
        mine, refused = compute(root)
    finally:
        shutil.rmtree(os.path.dirname(root), ignore_errors=True)
    if refused:
        return [(refused[0], "refused") + refused[2:]]
    groups = _groups(mine)
    resolved = authz.resolved_admin_policy(settings, None)
    denied = [g for g in groups if not authz.is_admin_for(user, g, resolved)]
    if denied:
        raise Refuse("%s (who pre-approved pipeline %s) is not an admin for %s"
                     % (user, pid, ", ".join(denied)))
    policy = trust.load_key_policy()
    if policy is None:
        raise Refuse("no key policy (key-policy.json): the worker fails closed")
    badk = [g for g in groups if not trust.key_authorized(key_id, g, policy)]
    if badk:
        raise Refuse("signing key %s is not allowed for %s" % (key_id, ", ".join(badk)))
    extra = {"build": pid, "approval": pre["id"], "approver": user, "group": group,
             "store": sorted(k for k, _d, _x in boms.values())}
    differs = sorted(k for k, _d, x in boms.values() if x)
    if differs:
        extra["store_differs_from_mr"] = differs
    ci_archs = sorted(a for a in parsed if parsed[a][2] != "superseded")
    if archs != ci_archs:
        # The push certified other architectures too (a deletion, or a file
        # whose architecture it could not tell): computed as the CI did.
        extra["archs"] = {"build": archs, "ci": ci_archs}
    out = []
    for r in shadow.judge(mine, parsed):
        d = dict(r[6])
        if r[1] != "match" or differs or "archs" in extra:
            d["replay"] = extra
        out.append(r[:6] + (d,))
    return out
