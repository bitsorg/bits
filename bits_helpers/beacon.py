# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Best-effort reuse beacon (ADR-0004 §6, usage-informed GC).

When a build reuses artifacts from the shared store, it reports the small
references (build id + content hashes) to a console endpoint so the console can
tell which shared objects are actually consumed. This is *deliberately* not a
proxy — manifests and tarballs are fetched straight from S3; only tiny
references are reported here, never artifact data.

The call is fire-and-forget: it runs entirely in a daemon thread and swallows
every error, so it can NEVER block the build, fail it, or keep the process alive
at exit. If the console is down or slow, the build neither waits nor notices.
"""

import threading
from urllib.parse import urlencode
from urllib.request import urlopen

# Cap hashes per GET so the URL stays a sane length; the worker sends batches.
_BEACON_BATCH = 200


def send_reuse_beacon(console_url, build_id, hashes, timeout=2.0,
                      batch=_BEACON_BATCH):
    """Report reused hashes to ``<console_url>/api/reuse`` without blocking.

    ``GET /api/reuse?build=<build_id>&hashes=<h1,h2,…>`` (batched). Runs in a
    daemon thread; all network errors are swallowed. Returns the thread (so
    tests/callers may join it) or None when there is nothing to send.
    """
    if not console_url or not build_id or not hashes:
        return None
    # Only speak http(s): never let a misconfigured URL turn this into a
    # file://, ftp:// or other local/SSRF fetch via urlopen.
    if not str(console_url).lower().startswith(("http://", "https://")):
        return None
    hashes = list(dict.fromkeys(hashes))          # de-dup, keep order
    base = console_url.rstrip("/") + "/api/reuse"

    def _worker():
        for i in range(0, len(hashes), batch):
            q = urlencode({"build": build_id, "hashes": ",".join(hashes[i:i + batch])})
            resp = None
            try:
                resp = urlopen(base + "?" + q, timeout=timeout)  # nosec - best-effort GET
            except Exception:
                pass                                # never propagate; best-effort
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

    t = threading.Thread(target=_worker, name="bits-reuse-beacon", daemon=True)
    t.start()
    return t
