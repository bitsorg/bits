# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Append-only audit trail for signing actions.

MVP: one structured JSON line per event via the logging module (captured by the
container log). Never records secrets — only who/what/when/key_id/digest. A
tamper-evident hash chain is a documented follow-up (design §9).
"""

import json
import logging
import time

_log = logging.getLogger("bits_console_backend.audit")


def record(event: str, **fields):
    fields["event"] = event
    fields["ts"] = time.time()
    _log.info(json.dumps(fields, sort_keys=True))
