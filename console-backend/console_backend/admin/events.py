# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's audit chain, events.jsonl under BITS_ADMIN_DATA: what admins did, as the
orchestrator's rows and hashes (orch/events.py), so audit-log can keep a copy. Never a
passphrase, a code or a PRF output.

    python3 -m console_backend.admin.events verify
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

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


MAX_ANSWER = 1 << 20  # audit-log's answers are small; more is refused


def _printable(text, limit=200):
    return "".join(c if c.isprintable() else "?" for c in str(text))[:limit]


class Outside:
    """Keeps audit-log's copy of the chain (/v1/chains/bits-admin): compares the heads, then
    sends what is new. A copy that disagrees is never written to; the page says so."""

    def __init__(self, chain, url, token, call=None, name="bits-admin", batch=100):
        self.chain, self.token, self.batch = chain, token, batch
        self.base = "%s/v1/chains/%s" % (url.rstrip("/"), name)
        self.call = call or self._http
        self.status = {"state": "not checked yet", "seq": None, "error": "", "checked": None}
        self.wake = threading.Event()

    def _http(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, self._answer(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, self._answer(e)
            except (ValueError, OSError, http.client.HTTPException):
                return e.code, {}
        except http.client.HTTPException as e:  # cut short, a bad status line
            raise OSError("its answer: %s" % type(e).__name__)

    @staticmethod
    def _answer(r):
        data = r.read(MAX_ANSWER + 1)
        if len(data) > MAX_ANSWER:
            raise ValueError("an answer over %d bytes" % MAX_ANSWER)
        out = json.loads(data)
        if not isinstance(out, dict):
            raise ValueError("an answer that is not an object")
        return out

    def _set(self, state, seq, error=""):
        error = _printable(error)
        if (state, error) != (self.status["state"], self.status["error"]):
            print("bits-admin: audit-log copy: %s%s" % (state, ": " + error if error else ""),
                  file=sys.stderr, flush=True)
        self.status = {"state": state, "seq": seq, "error": error, "checked": time.time()}
        return self.status

    def sync(self):
        """One pass: in step, diverged or unreachable."""
        seq = self.status["seq"]
        try:
            rows = self.chain.rows()
            code, head = self.call("GET", "/head")
            if code != 200:
                raise OSError("its head: HTTP %d %s" % (code, head.get("error", "")))
            seq = head["seq"]
            if not isinstance(seq, int) or seq < 0 or seq > len(rows):
                return self._set("diverged", seq, "it holds %r events, here %d" % (seq, len(rows)))
            if seq and rows[seq - 1]["hash"] != head["hash"]:
                return self._set("diverged", seq, "its event %d is not this one" % seq)
            while seq < len(rows):
                code, out = self.call("POST", "/events", {"events": rows[seq:seq + self.batch]})
                if code == 409:
                    return self._set("diverged", seq, out.get("error", ""))
                if code != 200:
                    raise OSError("sending: HTTP %d %s" % (code, out.get("error", "")))
                new = out["head"]["seq"]
                if not isinstance(new, int) or not seq < new <= len(rows):
                    return self._set("diverged", seq, "after sending, it holds %r events" % (new,))
                seq = new
            return self._set("in step", seq)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            return self._set("unreachable", seq, e)

    def run(self, interval=60):
        """Forever: a pass at start, after each new event (wake) and every *interval* s."""
        while True:
            try:
                self.sync()
            except Exception as e:  # the thread must not end: the page would show an old state
                self._set("unreachable", self.status["seq"], "%s: %s" % (type(e).__name__, e))
            self.wake.wait(interval)
            self.wake.clear()


def outside_from_env(chain):
    """The Outside for BITS_ADMIN_AUDIT_URL and its token file, or None (logged)."""
    url = os.environ.get("BITS_ADMIN_AUDIT_URL", "")
    if not url:
        return None
    try:
        with open(os.environ.get("BITS_ADMIN_AUDIT_TOKEN_FILE", "/run/audit-chain/token")) as f:
            token = f.read().strip()
    except OSError as e:
        token, why = "", e
    else:
        why = "the token file is empty"
    if not token:
        print("bits-admin: audit-log copy off: no token (%s); make audit-token" % why,
              file=sys.stderr, flush=True)
        return None
    return Outside(chain, url, token)


def main(argv):
    if argv != ["verify"]:
        sys.exit(__doc__.split("\n\n")[-1])
    count, broken = Chain().verify()
    print("%d events, %s" % (count, "intact" if broken is None else "broken at event %d" % broken))
    return 0 if broken is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
