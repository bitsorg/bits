# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's audit chain, events.jsonl under BITS_ADMIN_DATA: what admins did, as the
orchestrator's rows and hashes (orch/events.py), so audit-log can keep a copy. Never a
passphrase, a code or a PRF output.

    python3 -m console_backend.admin.events verify
"""

import json
import os
import sys
import threading
import time

from ..orch.events import GENESIS, MAX_PAYLOAD, _canonical, _digest
from .passkeys import DATA_DIR


class Chain:
    def __init__(self, path=None, now_ms=lambda: int(time.time() * 1000)):
        self.path = path or os.path.join(DATA_DIR, "events.jsonl")
        self.now_ms, self.lock, self._head = now_ms, threading.Lock(), None

    def _lines(self):
        """The complete lines; a last one without its newline (a torn write) is left out."""
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return []
        return data.split(b"\n")[:-1]

    def rows(self):
        return [json.loads(line) for line in self._lines() if line.strip()]

    def head(self):
        """(seq, hash) of the last event, (0, GENESIS) for none; a torn last line is cut off."""
        if self._head is None:
            lines = self._lines()
            size = sum(len(line) + 1 for line in lines)
            if os.path.exists(self.path) and os.path.getsize(self.path) > size:
                print("bits-admin: %s: cutting off a torn last line" % self.path, file=sys.stderr, flush=True)
                os.truncate(self.path, size)
            last = json.loads(lines[-1]) if lines else None
            self._head = (last["seq"], last["hash"]) if last else (0, GENESIS)
        return self._head

    def append(self, kind, subject, actor, payload=None):
        """One event by the person *actor*, written and synced before it returns."""
        text = _canonical(payload or {})
        if len(text) > MAX_PAYLOAD:
            raise ValueError("event payload over %d bytes" % MAX_PAYLOAD)
        with self.lock:
            seq, prev = self.head()
            row = {"seq": seq + 1, "ts_ms": self.now_ms(), "kind": kind, "subject": subject,
                   "actor_kind": "person", "actor_id": actor, "cause": None, "payload": text,
                   "prev_hash": prev}
            row["hash"] = _digest(prev, row)
            data = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    while data:
                        data = data[os.write(fd, data):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                self._head = None  # the file decides what was written: read it again next time
                raise
            self._head = (row["seq"], row["hash"])
        return row

    def verify(self):
        """(count, None) when intact, or (count, n): the first event, counted from 1, whose
        link or hash does not hold, or which is not an event at all."""
        prev, n = GENESIS, 0
        for line in self._lines():
            n += 1
            try:
                r = json.loads(line)
                ok = r["seq"] == n and r["prev_hash"] == prev and _digest(prev, r) == r["hash"]
            except (ValueError, KeyError, TypeError):
                ok = False
            if not ok:
                return n, n
            prev = r["hash"]
        return n, None


def main(argv):
    if argv != ["verify"]:
        sys.exit(__doc__.split("\n\n")[-1])
    count, broken = Chain().verify()
    print("%d events, %s" % (count, "intact" if broken is None else "broken at event %d" % broken))
    return 0 if broken is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
