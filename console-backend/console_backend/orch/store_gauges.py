# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The store's gauges for Monitoring (bits_store_*), pushed by the worker on a
schedule, as a build through bits-store lists nothing: objects by architecture,
and by certified build from the certified set; the rest is "(uncertified)"."""

import json
import logging

from bits_helpers import store_stats
from bits_helpers.metrics import push_prometheus

from . import db
from .reconcile import _kv_get, _kv_set

log = logging.getLogger(__name__)
NEXT_KEY = "store.gauges.next_ms"     # kv: when they are next due
RETRY_S = 300                         # after a failure, the next try this soon


def certified_builds(conn):
    """({hash: build id}, {build ids}) of the certified set's live BOMs; a hash
    shared by builds goes to the first by build id, as `bits store-stats` does."""
    by_hash, ids = {}, set()
    for r in conn.execute("SELECT c.build_id, b.body FROM certified c JOIN boms b"
                          " ON b.sha256 = c.bom_sha256 WHERE c.removed_version IS NULL"
                          " AND c.build_id != '' ORDER BY c.build_id"):
        try:
            doc = json.loads(r["body"])
        except (ValueError, TypeError):
            continue
        ids.add(r["build_id"])
        for p in (doc.get("packages") or []) if isinstance(doc, dict) else []:
            if isinstance(p, dict) and p.get("hash"):
                by_hash.setdefault(str(p["hash"]), r["build_id"])
    return by_hash, ids


def step(conn, store, url, every_s, now_ms=None, push=push_prometheus):
    """List the store and push its gauges to *url*, at most every *every_s*
    seconds (a failure raises, and is tried again in RETRY_S). Returns the
    objects counted, or 0 when off or not due."""
    now = db.now_ms() if now_ms is None else now_ms
    if not url or now < int(_kv_get(conn, NEXT_KEY, "0") or 0):
        return 0
    try:
        if store is None:
            raise RuntimeError("the store gauges need the store (BITS_ORCH_STORE)")
        s3, bucket = store
        by_hash, signed = certified_builds(conn)
        stats = store_stats.summarise(store_stats.iter_s3_objects(s3, bucket), by_hash, signed)
        push(url, store_stats.to_prometheus(stats), timeout=15)
    except Exception:
        with db.transaction(conn):
            _kv_set(conn, NEXT_KEY, str(int(now + 1000 * min(RETRY_S, every_s))))
        raise
    with db.transaction(conn):
        _kv_set(conn, NEXT_KEY, str(int(now + 1000 * every_s)))
    log.info("store gauges: %d objects, %d architectures -> %s",
             stats["total_objects"], len(stats["arch"]), url)
    return stats["total_objects"]
