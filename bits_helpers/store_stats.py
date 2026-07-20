# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3 binary-store statistics, computed by bits itself.

bits already holds the S3 credentials and the (signed) manifests, so it can
summarise the store directly instead of a separate CI collector walking the
bucket. This produces the document the Monitoring dashboard's S3 bar consumes:
per-architecture totals *and* a per-build (manifest) breakdown with a signed
flag, so a platform bar can be split into per-build segments marked signed /
uncertified.

The aggregation is pure (no S3, no network) so it is unit-testable; the CLI
(`bits store-stats`, see :func:`doStoreStats`) wires it to the real S3 client
and manifests.

Store layout the keys follow (ADR-0005):
    TARS/<arch>/store/<h2>/<hash>/<pkg>-<verrev>.<arch>.tar.gz
"""
import json
import os
import time

from bits_helpers.log import debug, info

SCHEMA_VERSION = 2          # v1 had arch/other/totals; v2 adds "manifests"
UNCERTIFIED = "(uncertified)"


def parse_arch_hash(key, tars_prefix="TARS/"):
    """``TARS/<arch>/store/<h2>/<hash>/<file>`` -> ``(arch, hash)``.

    Returns ``(None, None)`` for keys outside the store prefix (e.g. MANIFESTS/),
    and ``(arch, None)`` when the key is under an arch but not a store object.
    """
    if not key.startswith(tars_prefix):
        return (None, None)
    parts = key[len(tars_prefix):].split("/")
    arch = parts[0] if parts and parts[0] else None
    h = parts[3] if len(parts) >= 5 and parts[1] == "store" else None
    return (arch, h)


def hash_to_build_map(build_manifests):
    """Map content hash -> build_id from build BOM manifests (first build wins).

    Sorted by build_id so the attribution of a hash shared by several builds is
    deterministic.
    """
    out = {}
    for man in sorted(build_manifests or [],
                      key=lambda m: str(m.get("build_id") or "")):
        bid = str(man.get("build_id") or man.get("_source_group") or "(unknown)")
        for e in man.get("packages") or []:
            h = e.get("hash")
            if h and h not in out:
                out[h] = bid
    return out


def signed_builds_from_common(common_manifests):
    """Set of build_ids vouched for by verified signed common manifests.

    A build is "signed" when its build_id appears in a signed common manifest's
    ``sources`` list (i.e. it was certified into the trusted set).
    """
    signed = set()
    for cm in common_manifests or []:
        for bid in cm.get("sources") or []:
            signed.add(str(bid))
    return signed


def summarise(objects, hash_to_build=None, signed_build_ids=None,
              tars_prefix="TARS/"):
    """Aggregate ``(key, size)`` objects into arch + per-manifest stats.

    * ``hash_to_build`` maps a content hash to the build (manifest) that produced
      it; unknown hashes are attributed to ``"(uncertified)"``.
    * ``signed_build_ids`` marks which builds are signed.

    Both are optional: with neither, the ``manifests`` list still groups objects
    by ``(uncertified, arch)`` so the caller degrades gracefully. Pure function.
    """
    hash_to_build = hash_to_build or {}
    signed_build_ids = set(signed_build_ids or ())
    per_arch = {}                 # arch -> [bytes, objects]
    per_manifest = {}             # (build_id, arch) -> [bytes, objects]
    other = [0, 0]
    total = [0, 0]
    for key, size in objects:
        size = int(size or 0)
        total[0] += size
        total[1] += 1
        arch, h = parse_arch_hash(key, tars_prefix)
        if arch:
            s = per_arch.setdefault(arch, [0, 0])
            s[0] += size
            s[1] += 1
            build = hash_to_build.get(h) if h else None
            m = per_manifest.setdefault((build or UNCERTIFIED, arch), [0, 0])
            m[0] += size
            m[1] += 1
        else:
            other[0] += size
            other[1] += 1

    arch_list = sorted(
        ({"arch": a, "bytes": b, "objects": n} for a, (b, n) in per_arch.items()),
        key=lambda e: e["bytes"], reverse=True)
    manifests = sorted(
        ({"manifest": bld, "arch": a, "bytes": b, "objects": n,
          "signed": bld != UNCERTIFIED and bld in signed_build_ids}
         for (bld, a), (b, n) in per_manifest.items()),
        key=lambda e: e["bytes"], reverse=True)
    return {
        "arch": arch_list,
        "manifests": manifests,
        "other": {"bytes": other[0], "objects": other[1]},
        "total_bytes": total[0],
        "total_objects": total[1],
    }


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def to_prometheus(stats):
    """Prometheus exposition text for the store stats (per-arch + per-manifest)."""
    lines = ["# TYPE bits_store_bytes gauge", "# TYPE bits_store_objects gauge"]
    for e in stats["arch"]:
        a = _esc(e["arch"])
        lines.append('bits_store_bytes{arch="%s"} %d' % (a, e["bytes"]))
        lines.append('bits_store_objects{arch="%s"} %d' % (a, e["objects"]))
    for e in stats.get("manifests", []):
        lbl = 'manifest="%s",arch="%s",signed="%s"' % (
            _esc(e["manifest"]), _esc(e["arch"]), "1" if e["signed"] else "0")
        lines.append('bits_store_manifest_bytes{%s} %d' % (lbl, e["bytes"]))
        lines.append('bits_store_manifest_objects{%s} %d' % (lbl, e["objects"]))
    lines.append("bits_store_bytes_total %d" % stats["total_bytes"])
    lines.append("bits_store_objects_total %d" % stats["total_objects"])
    return "\n".join(lines) + "\n"


def push_store_gauges(s3_client, bucket, monitor_url, work_dir=None) -> bool:
    """Summarise the S3 store and POST the gauges to the VM *monitor_url*.

    Called wherever the store was just mutated — end of a `bits build` that
    uploaded, and end of a `bits publish` bulk upload (re-publishes go through
    publish, not build, and the Monitoring dashboard's store bar would otherwise
    go stale/empty between builds). Best-effort and fire-and-forget: listing the
    store or pushing the gauges must never fail the caller. Returns True when
    the push succeeded.
    """
    import urllib.request
    try:
        hash_to_build = {}
        if work_dir:
            try:
                from bits_helpers import certify as _certify
                hash_to_build = hash_to_build_map(_certify.load_build_manifests(
                    [os.path.join(work_dir, "MANIFESTS")]))
            except Exception:  # pylint: disable=broad-except
                pass  # per-build attribution is optional; degrade to (uncertified)
        stats = summarise(iter_s3_objects(s3_client, bucket), hash_to_build, set())
        req = urllib.request.Request(
            monitor_url.rstrip("/") + "/api/v1/import/prometheus",
            data=to_prometheus(stats).encode("utf-8"),
            headers={"Content-Type": "text/plain"}, method="POST")
        urllib.request.urlopen(req, timeout=15).close()
        info("store-stats: pushed store gauges to %s (%d objects, %d arch)",
             monitor_url, stats["total_objects"], len(stats["arch"]))
        return True
    except Exception as exc:  # pylint: disable=broad-except
        debug("store-stats push skipped: %s", exc)
        return False


def iter_s3_objects(client, bucket, prefix=""):
    """Yield ``(key, size)`` for every object in the bucket (paginated)."""
    paginator = client.get_paginator("list_objects_v2")
    kw = {"Bucket": bucket}
    if prefix:
        kw["Prefix"] = prefix
    for page in paginator.paginate(**kw):
        for obj in page.get("Contents", []) or []:
            yield obj["Key"], obj.get("Size", 0)


def build_document(stats, bucket=None, endpoint=None):
    """Wrap *stats* in the versioned document the dashboard fetches."""
    doc = {"v": SCHEMA_VERSION, "ts": int(time.time())}
    if bucket:
        doc["bucket"] = bucket
    if endpoint:
        doc["endpoint"] = endpoint
    doc.update(stats)
    return doc


def doStoreStats(args, parser):
    """CLI entrypoint for ``bits store-stats``.

    Lists the S3 store, loads the build + signed common manifests, and writes the
    v2 store document (and optionally pushes Prometheus gauges). Reuses the same
    S3 client/manifest helpers as ``bits certify`` so the view is consistent.
    """
    from bits_helpers.log import banner, warning
    store = getattr(args, "storeStatsStore", None) or getattr(args, "remoteStore", "")
    if not store:
        parser.error("store-stats needs --store (or --remote-store)")

    from bits_helpers.publish import _normalize_s3_store
    from bits_helpers.sync import remote_from_url
    store = _normalize_s3_store(store)
    writer = remote_from_url(store, store, str(getattr(args, "architecture", "") or ""),
                             args.workDir)
    bucket = getattr(writer, "remoteStore", None) or getattr(writer, "writeStore", None)
    s3 = writer.s3

    # Build (manifest) attribution + signed set — best-effort: a missing/unsigned
    # manifest set just yields an all-uncertified breakdown, never an error.
    hash_to_build, signed = {}, set()
    try:
        from bits_helpers import certify
        srcs = list(getattr(args, "manifests", None) or [])
        if not srcs:
            srcs = [os.path.join(args.workDir, "MANIFESTS")]
        hash_to_build = hash_to_build_map(certify.load_build_manifests(srcs))
    except Exception as exc:  # pylint: disable=broad-except
        warning("store-stats: could not load build manifests (%s); "
                "per-build breakdown will be uncertified only.", exc)
    try:
        common = _load_common_manifests(getattr(args, "trustManifest", None))
        signed = signed_builds_from_common(common)
    except Exception as exc:  # pylint: disable=broad-except
        warning("store-stats: could not load signed manifests (%s); "
                "builds will show as unsigned.", exc)

    tars_prefix = getattr(args, "tarsPrefix", None) or "TARS/"
    stats = summarise(iter_s3_objects(s3, bucket), hash_to_build, signed, tars_prefix)
    doc = build_document(stats, bucket=bucket)

    out_path = getattr(args, "out", None) or "store.json"
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    banner("store-stats: %d objects, %d bytes across %d arch / %d build(s) -> %s",
           stats["total_objects"], stats["total_bytes"], len(stats["arch"]),
           len(stats["manifests"]), out_path)

    murl = (getattr(args, "monitorUrl", None) or os.environ.get("METRICS_URL") or "").strip().rstrip("/")
    if murl:
        import urllib.request
        try:
            req = urllib.request.Request(
                murl + "/api/v1/import/prometheus",
                data=to_prometheus(stats).encode("utf-8"),
                headers={"Content-Type": "text/plain"}, method="POST")
            urllib.request.urlopen(req, timeout=15).close()
            banner("store-stats: pushed gauges to %s", murl)
        except Exception as exc:  # never fail on a metrics push
            warning("store-stats: metrics push failed: %s", exc)
    return 0


def _load_common_manifests(trust_manifest):
    """Load + signature-verify comma-separated signed common manifests (paths or
    URLs); return the parsed dicts. Empty when none configured or verifiable."""
    if not trust_manifest:
        return []
    from bits_helpers import trust
    out = []
    for src in (s.strip() for s in str(trust_manifest).split(",") if s.strip()):
        try:
            if not trust.verify_manifest(src):
                continue
            with open(src) as fh:
                out.append(json.load(fh))
        except Exception:  # pylint: disable=broad-except
            continue
    return out
