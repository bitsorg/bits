# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin (ADR bits-services 0001): the site's admin page, reached only through an
SSH tunnel to its unix socket: sign-in with a one-time setup code (a file only the
bits-admin group reads), a session, /healthz, the host helper's status, and the
security keys that will unlock the signer (passkeys.py).

A page from another site in the admin's browser cannot use the tunnel: requests must
name an allowed Host (no DNS rebinding) and a POST an allowed Origin.
"""

import hmac
import html
import json
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import passkeys

HELPER_SOCKET = os.environ.get("BITS_ADMIN_HELPER_SOCKET", "/run/bits-admin-helper/helper.sock")
CODE_FILE = os.environ.get("BITS_ADMIN_CODE_FILE", "/run/bits-admin/setup-code")
ORIGINS = [o.strip().rstrip("/") for o in
           os.environ.get("BITS_ADMIN_ORIGINS", "http://localhost:8800").split(",") if o.strip()]
CODE_TTL, SESSION_TTL, CHALLENGE_TTL = 1800, 12 * 3600, 300
FAILURES, WINDOW = 20, 600  # wrong setup codes per window (seconds); a new code clears them
COOKIE = "bits_admin_session"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
with open(os.path.join(os.path.dirname(__file__), "static", "admin.js")) as _f:
    SCRIPT = _f.read()
HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                      "script-src 'self'; connect-src 'self'; "
                                      "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
           # same-origin, not no-referrer: with no-referrer a browser sends Origin: null
           # even on the page's own form, and the Origin check would refuse it.
           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
           "Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def _plain(code):
    return "".join(c for c in code.upper() if c in ALPHABET)


def write_code(code, path=CODE_FILE):
    """The setup code into its file (mode 0640: the bits-admin group reads it), whole."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".setup-code.")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o640)
            f.write(code + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("bits-admin: a new setup code is in %s, valid %d minutes" % (path, CODE_TTL // 60),
          file=sys.stderr, flush=True)


class State:
    """The setup code, the sessions and the failed attempts, in memory only; each
    code signs in once, and a new one is written for the next sign-in."""

    def __init__(self, now=time.time, write=write_code):
        self.now, self.write, self.sessions, self.failures = now, write, {}, []
        self.challenges = {}  # (session, purpose) -> (value, until), each used once
        self.lock = threading.Lock()
        self.renew_code()

    def renew_code(self):
        self.code, self.code_until, self.failures = new_code(), self.now() + CODE_TTL, []
        self.write(self.code)

    def locked_out(self):
        with self.lock:
            self.failures = [t for t in self.failures if self.now() - t <= WINDOW]
            return len(self.failures) >= FAILURES

    def check_code(self, given):
        with self.lock:
            if self.now() > self.code_until:
                self.renew_code()
                return False
            if hmac.compare_digest(_plain(given), _plain(self.code)):
                self.renew_code()  # used: the next sign-in needs the next one
                return True
            self.failures.append(self.now())
            return False

    def open_session(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = self.now() + SESSION_TTL
        return token

    def signed_in(self, token):
        with self.lock:
            until = self.sessions.get(token or "")
            if until is None or until < self.now():
                self.sessions.pop(token or "", None)
                for key in [k for k in self.challenges if k[0] == token]:
                    del self.challenges[key]
                return False
            return True

    def close_session(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)
            for key in [k for k in self.challenges if k[0] == token]:
                del self.challenges[key]

    def put_challenge(self, token, purpose, value):
        with self.lock:
            self.challenges[(token, purpose)] = (value, self.now() + CHALLENGE_TTL)

    def take_challenge(self, token, purpose):
        with self.lock:
            value, until = self.challenges.pop((token, purpose), (None, 0))
            return value if until >= self.now() else None


def ask_helper(request, path=HELPER_SOCKET):
    """One request to the host helper (tools/bits-admin-helper.py in bits-services)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(130)
            c.connect(path)
            c.sendall((json.dumps(request) + "\n").encode())
            return json.loads(c.makefile().readline())
    except (OSError, ValueError) as e:
        return {"ok": False, "error": "the host helper is not reachable (%s)" % type(e).__name__}


def _page(title, body):
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>%s</title>"
        "<style>body{font:15px system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
        "pre{background:#f3f4f6;padding:1rem;overflow:auto}input,button{font:inherit;padding:.3rem .6rem}"
        "</style><h1>bits-admin</h1>%s" % (html.escape(title), body), headers=HEADERS)


def _keys_html(keys):
    if not keys:
        return "<p>None yet.</p>"
    rows = "".join("<tr><td>%s</td><td>%s</td><td><code>%s</code></td><td><form method=post "
                   "action=/passkeys/delete><input type=hidden name=id value=\"%s\"><button>Remove"
                   "</button></form></td></tr>" % (html.escape(k["label"]), html.escape(k["added"]),
                                                    html.escape(k["id"][:8]), html.escape(k["id"]))
                   for k in keys)
    return "<table><tr><th>Label<th>Added<th>Id<th></tr>%s</table>" % rows


async def _body(request, limit):
    """The request body, or ValueError when it is larger than limit (chunked too)."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise ValueError("too large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ValueError("too large")
    return body


def create_app(state=None, helper=ask_helper, origins=None, store=None):
    state = state or State()
    store = store or passkeys.Store()
    origins = [o.rstrip("/") for o in (origins or ORIGINS)]
    rp = passkeys.rp_id(origins)
    if any(urlsplit(o).hostname != rp for o in origins):
        raise ValueError("BITS_ADMIN_ORIGINS must all have the host %s (the passkeys' rp_id)" % rp)
    hosts = {urlsplit(o).netloc.lower() for o in origins} | {"localhost"}
    app = FastAPI(title="bits-admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in hosts:
            return Response("unknown host", status_code=421, headers=HEADERS)
        if request.method not in ("GET", "HEAD") and \
                request.headers.get("origin", "").rstrip("/") not in origins:
            return Response("cross-site request refused", status_code=403, headers=HEADERS)
        return await call_next(request)

    def signed_in(request):
        return state.signed_in(request.cookies.get(COOKIE))

    def session(request):
        token = request.cookies.get(COOKIE)
        return token if state.signed_in(token) else None

    def refuse(message, status=400):
        return JSONResponse({"error": message}, status_code=status, headers=HEADERS)

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok"}, headers=HEADERS)

    @app.get("/admin.js")
    def script():
        return Response(SCRIPT, media_type="text/javascript", headers=HEADERS)

    @app.get("/")
    def index(request: Request):
        if not signed_in(request):
            return _page("Sign in", "<p>Enter the setup code: <code>make admin-code</code> on the "
                         "host prints it. Each code signs in once.</p>"
                         "<form method=post action=/login><input name=code autocomplete=off autofocus "
                         "size=22 placeholder=XXXX-XXXX-XXXX-XXXX> <button>Sign in</button></form>")
        status = helper({"op": "status"})
        return _page("Site", "<p>Signed in with the setup code.</p><h2>Host helper</h2><pre>%s</pre>"
                     "<h2>Security keys</h2>%s<form id=enrol><input id=label maxlength=%d size=30 "
                     "placeholder=\"e.g. YubiKey 5C, Predrag\"> <button>Add security key</button>"
                     "</form><p id=status></p><form method=post action=/logout><button>Sign out"
                     "</button></form><script src=/admin.js></script>"
                     % (html.escape(json.dumps(status, indent=1)), _keys_html(store.load()),
                        passkeys.LABEL_MAX))

    @app.post("/login")
    async def login(request: Request):
        if state.locked_out():
            return _page("Sign in", "<p>Too many wrong codes: try again in 10 minutes, "
                         "or restart bits-admin for a new code.</p>")
        if int(request.headers.get("content-length") or 0) > 1024:
            return _page("Sign in", "<p>Wrong or expired code.</p><p><a href=/>Again</a></p>")
        body = await request.body()
        if len(body) > 1024:
            return _page("Sign in", "<p>Wrong or expired code.</p><p><a href=/>Again</a></p>")
        code = (parse_qs(body.decode(errors="replace")).get("code") or [""])[0]
        if not state.check_code(code):
            return _page("Sign in", "<p>Wrong or expired code.</p><p><a href=/>Again</a></p>")
        r = RedirectResponse("/", status_code=303, headers=HEADERS)
        r.set_cookie(COOKIE, state.open_session(), max_age=SESSION_TTL, httponly=True,
                     samesite="strict", path="/")
        return r

    @app.post("/passkeys/options")
    async def passkey_options(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:
            label = passkeys.clean_label(json.loads(await _body(request, 1024)).get("label", ""))
        except (ValueError, AttributeError):
            return refuse("bad request")
        options, challenge = passkeys.registration_options(rp, label, store.load())
        state.put_challenge(token, "enrol", (challenge, label))
        return Response(options, media_type="application/json", headers=HEADERS)

    @app.post("/passkeys")
    async def passkey_add(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        started = state.take_challenge(token, "enrol")
        if not started:
            return refuse("the enrolment expired; start again")
        challenge, label = started
        try:
            credential = json.loads(await _body(request, 16384))["credential"]
            key = passkeys.verify_registration(rp, origins, credential, challenge, label, state.now())
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            print("bits-admin: enrolment refused (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            return refuse("the security key's answer did not verify")
        try:
            store.add(key)
        except passkeys.Refused as e:
            return refuse(str(e))
        except OSError as e:
            print("bits-admin: cannot save %s: %s" % (store.path, e), file=sys.stderr, flush=True)
            return refuse("bits-admin could not save the key (see its log)", 500)
        print("bits-admin: security key %s (%s) enrolled" % (key["id"][:8], key["label"]),
              file=sys.stderr, flush=True)
        return JSONResponse({"ok": True}, headers=HEADERS)

    @app.post("/passkeys/delete")
    async def passkey_delete(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        try:
            key_id = (parse_qs((await _body(request, 1024)).decode(errors="replace")).get("id") or [""])[0]
        except ValueError:
            return refuse("bad request")
        if store.remove(key_id):
            print("bits-admin: security key %s removed" % key_id[:8], file=sys.stderr, flush=True)
        return RedirectResponse("/", status_code=303, headers=HEADERS)

    @app.post("/logout")
    def logout(request: Request):
        state.close_session(request.cookies.get(COOKIE))
        r = RedirectResponse("/", status_code=303, headers=HEADERS)
        r.delete_cookie(COOKIE, path="/")
        return r

    return app


def bind(path):
    """The listening socket, mode 0660 (the bits-admin group); a stale socket is
    removed first, anything else there is refused."""
    if os.path.lexists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            sys.exit("%s exists and is not a socket" % path)
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(64)
    return sock


def main():
    import uvicorn
    path = os.environ.get("BITS_ADMIN_SOCKET", "/run/bits-admin/admin.sock")
    sock = bind(path)
    print("bits-admin listening on %s for %s" % (path, ", ".join(ORIGINS)), file=sys.stderr, flush=True)
    uvicorn.run(create_app(), fd=sock.fileno(), server_header=False, proxy_headers=False)
