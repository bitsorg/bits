# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Push Prometheus text-format samples to a VictoriaMetrics/Prometheus
``/api/v1/import/prometheus`` endpoint. One place for the URL suffix, the
content type and the POST, so the monitor and the store-stats collector don't
each hard-code them."""

import urllib.request

PROMETHEUS_IMPORT_PATH = "/api/v1/import/prometheus"


def push_prometheus(base_url, body, timeout=15):
    """POST *body* (str or bytes) as Prometheus text to
    ``<base_url>/api/v1/import/prometheus`` and return the HTTP status (or None).

    Raises on failure — the caller decides how loudly to report it.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + PROMETHEUS_IMPORT_PATH,
        data=body, headers={"Content-Type": "text/plain"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        return getattr(resp, "status", None)
    finally:
        resp.close()
