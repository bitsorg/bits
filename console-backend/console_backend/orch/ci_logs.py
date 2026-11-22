# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host journals build jobs send (POST /ops/ci/logs), written to VictoriaLogs.

A job sends its host's journal as `journalctl -o json --all` lines. The job, pipeline,
runner and community each line is filed under come from the job's verified CI ID
token and the orchestrator's records; the rest (host, unit, time) is the job's claim."""

import datetime
import json
import re

import requests

from console_backend.orch import failures

# The journal fields kept (most others are about the process: its command line can
# hold secrets), under the names they are searched by.
KEEP = {"_HOSTNAME": "host", "_SYSTEMD_UNIT": "unit_name", "SYSLOG_IDENTIFIER": "ident",
        "PRIORITY": "priority", "_PID": "pid", "_COMM": "comm", "CONTAINER_NAME": "container",
        "_TRANSPORT": "transport"}
MAX_BYTES = 8 << 20
MAX_LINES = 20000
JOB_BYTES = 64 << 20   # all a job sends: its token is good for as long as the job may run
MSG_MAX = 32 << 10     # VictoriaLogs drops a line over 256 KiB, and the lines after it
FIELD_MAX = 200
MAX_STREAMS = 100      # (host, unit) pairs a request opens; the rest go to "other"
OPS_TENANT = {"AccountID": "0", "ProjectID": "0"}   # host and service logs: overall admins

_VARYING = re.compile(r"[0-9a-f]{8,}|\d+")          # session-12.scope, libpod-<id>.scope
_AUDIT_ARGS = re.compile(r"\b(proctitle|a\d+)=(\"[^\"]*\"|\S+)")   # command lines
_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _text(value):
    """A journal value as text: one that is not UTF-8 comes as a list of bytes, a field
    set more than once as a list of its values."""
    if isinstance(value, list):
        if all(isinstance(b, int) for b in value):
            return bytes(b & 0xFF for b in value).decode("utf-8", "replace")
        return "\n".join(_text(v) for v in value if v is not None)
    return value if isinstance(value, str) else str(value)


def _message(entry, keys):
    """The entry's message, redacted, cut to MSG_MAX; *keys* follows a private key a
    program logs line by line (pid → lines left), so its body never passes either."""
    if entry.get("MESSAGE") is None:
        return "(no message, or one journalctl left out: not sent with --all)"
    msg = _text(entry["MESSAGE"])
    who = (entry.get("_PID"), entry.get("SYSLOG_IDENTIFIER"))
    if keys.get(who):
        keys[who] = 0 if _KEY_END.search(msg) else keys[who] - 1
        return "[REDACTED: private key line]"
    if _KEY_BEGIN.search(msg) and not _KEY_END.search(msg):
        keys[who] = 64
    msg = _AUDIT_ARGS.sub(r"\1=[REDACTED]", failures.redact(msg)) or "(empty)"
    if len(msg) > MSG_MAX:
        msg = msg[:MSG_MAX] + " … (%d characters cut)" % (len(msg) - MSG_MAX)
    return msg


def rows(body, stamp):
    """VictoriaLogs JSON lines for the journal JSON lines in *body*, each message
    redacted and each carrying *stamp*; ValueError for a line that is not an entry."""
    out, keys, streams = [], {}, set()
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except RecursionError:
            raise ValueError("nested too deep: %.40s" % line)
        if not isinstance(entry, dict):
            raise ValueError("not a journal entry: %.40s" % line)
        if entry.get("_TRANSPORT") == "audit":     # command lines and arguments, nothing else
            continue
        row = {"_msg": _message(entry, keys)}
        try:
            row["_time"] = datetime.datetime.fromtimestamp(
                int(entry["__REALTIME_TIMESTAMP"]) / 1e6, datetime.timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass                                   # VictoriaLogs then takes the arrival time
        for field, name in KEEP.items():
            if entry.get(field) is not None:
                row[name] = _text(entry[field])[:FIELD_MAX]
        row.setdefault("host", "-")
        unit = _VARYING.sub("N", row.get("unit_name") or row.get("transport") or "-")
        if (row["host"], unit) not in streams and len(streams) >= MAX_STREAMS:
            row["host"] = unit = "other"
        streams.add((row["host"], unit))
        row["unit"] = unit
        row.update(stamp)                          # last: the token's names, not the line's
        out.append(json.dumps(row, separators=(",", ":"), ensure_ascii=False))   # under 256 KiB
    return out


def write(base, lines, timeout=(5, 30)):
    """*lines* into VictoriaLogs at *base*, in the ops tenant; raises on failure."""
    r = requests.post(base.rstrip("/") + "/insert/jsonline",
                      params={"_stream_fields": "host,unit"},
                      data=("\n".join(lines) + "\n").encode("utf-8", "replace"), timeout=timeout,
                      headers=dict(OPS_TENANT, **{"Content-Type": "application/stream+json"}))
    r.raise_for_status()
