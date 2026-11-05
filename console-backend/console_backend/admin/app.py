# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin (ADR bits-services 0001): the site's admin page, reached only through an
SSH tunnel to its unix socket: sign-in with a one-time setup code (a file only the
bits-admin group reads), a session, /healthz, the host helper's status, the keys
(passkeys.py) and, with one of them, unlocking the signer through the host helper.

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


def _on(value):
    """A setting is on unless empty, 0, false or no: an unknown value errs to strict."""
    return (value or "").strip().lower() not in ("", "0", "false", "no")


# Off by default: Touch ID, Face ID and other synced passkeys may unlock too.
SECURITY_KEYS_ONLY = _on(os.environ.get("BITS_ADMIN_SECURITY_KEYS_ONLY"))
CODE_TTL, SESSION_TTL, CHALLENGE_TTL, GRANT_TTL = 1800, 12 * 3600, 300, 60
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

    def put_challenge(self, token, purpose, value, ttl=CHALLENGE_TTL):
        with self.lock:
            self.challenges[(token, purpose)] = (value, self.now() + ttl)

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
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td><td><form "
                   "method=post action=/passkeys/delete><input type=hidden name=id value=\"%s\"><button>Remove"
                   "</button></form></td></tr>" % (html.escape(k["label"]),
                                                    "synced passkey" if k.get("synced") else "device-bound",
                                                    html.escape(k["added"]), html.escape(k["id"][:8]),
                                                    html.escape(k["box"]["stored"]) if k.get("box") else "–",
                                                    html.escape(k["id"]))
                   for k in keys)
    choices = "".join("<option value=\"%s\">%s</option>" % (html.escape(k["id"]), html.escape(k["label"]))
                      for k in keys)
    return ("<table><tr><th>Label<th>Kind<th>Added<th>Id<th>Passphrase stored<th></tr>%s</table>"
            "<h3>Store the signer's passphrase under a key</h3><form id=box><select id=box-key>%s</select> "
            "<input type=password id=passphrase autocomplete=off size=30 placeholder=passphrase> "
            "<button>Store</button></form>" % (rows, choices))


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


def create_app(state=None, helper=ask_helper, origins=None, store=None, security_keys_only=None):
    state = state or State()
    keys_only = SECURITY_KEYS_ONLY if security_keys_only is None else security_keys_only
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
        keys = store.load()
        boxed = [k for k in keys if k.get("box") and not (keys_only and k.get("synced"))]
        unlock = ""
        if status.get("signer") != "loaded" and boxed:
            unlock = ("<h2>Unlock the signer</h2><form id=unlock><select id=unlock-key>%s</select> "
                      "<button>Unlock</button></form>" % "".join(
                          "<option value=\"%s\">%s</option>" % (html.escape(k["id"]), html.escape(k["label"]))
                          for k in boxed))
        return _page("Site", "<p>Signed in with the setup code.</p><h2>Host helper</h2><pre>%s</pre>%s"
                     "<h2>Keys</h2><p>%s</p>%s<form id=enrol><input id=label maxlength=%d size=30 "
                     "placeholder=\"e.g. YubiKey 5C, MacBook Touch ID\"> <button>Add key</button>"
                     "</form><p id=status></p><form method=post action=/logout><button>Sign out"
                     "</button></form><script src=/admin.js></script>"
                     % (html.escape(json.dumps(status, indent=1)), unlock,
                        "Security keys only (BITS_ADMIN_SECURITY_KEYS_ONLY)." if keys_only else
                        "Security keys (YubiKey) and synced passkeys (Touch ID, Face ID).",
                        _keys_html(keys), passkeys.LABEL_MAX))

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
            key = passkeys.verify_registration(rp, origins, credential, challenge, label, state.now(),
                                               security_keys_only=keys_only)
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            print("bits-admin: enrolment refused (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            return refuse("the key's answer did not verify")
        try:
            store.add(key)
        except passkeys.Refused as e:
            return refuse(str(e))
        except OSError as e:
            print("bits-admin: cannot save %s: %s" % (store.path, e), file=sys.stderr, flush=True)
            return refuse("bits-admin could not save the key (see its log)", 500)
        kind = "synced passkey" if key["synced"] else "device-bound"
        print("bits-admin: key %s (%s, %s) enrolled" % (key["id"][:8], key["label"], kind),
              file=sys.stderr, flush=True)
        return JSONResponse({"ok": True}, headers=HEADERS)

    @app.post("/passkeys/box-options")
    async def box_options(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:
            key = store.get(json.loads(await _body(request, 1024)).get("id"))
        except (ValueError, AttributeError):
            return refuse("bad request")
        if not key:
            return refuse("no such key")
        if keys_only and key.get("synced"):
            return refuse("this is a synced passkey and this site accepts security keys only")
        options, challenge = passkeys.authentication_options(rp, key)
        state.put_challenge(token, "box", (challenge, key["id"]))
        return Response(options, media_type="application/json", headers=HEADERS)

    @app.post("/passkeys/box")
    async def box_store(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        started = state.take_challenge(token, "box")
        if not started:
            return refuse("this expired; start again")
        challenge, key_id = started
        key = store.get(key_id)
        try:
            body = json.loads(await _body(request, 8192))
            passphrase, credential = body["passphrase"], body["credential"]
            box = passkeys.check_box(passphrase, body["iv"], body["ct"])
        except passkeys.Refused as e:
            return refuse(str(e))
        except (ValueError, TypeError, KeyError):
            return refuse("bad request")
        try:
            if not key:
                raise passkeys.Refused("no such key")
            count = passkeys.verify_assertion(rp, origins, credential, challenge, key,
                                              security_keys_only=keys_only)
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            print("bits-admin: passphrase not stored (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            return refuse("the key's answer did not verify")
        store.update(key_id, sign_count=count)
        checked = helper({"op": "check-signer-passphrase", "passphrase": passphrase})
        if not checked.get("ok"):
            return refuse("not stored: %s" % checked.get("error", "the host helper refused"))
        box["stored"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(state.now()))
        try:
            if not store.update(key_id, box=box):
                return refuse("no such key (removed meanwhile)")
        except OSError as e:
            print("bits-admin: cannot save %s: %s" % (store.path, e), file=sys.stderr, flush=True)
            return refuse("bits-admin could not save it (see its log)", 500)
        print("bits-admin: passphrase stored under key %s" % key_id[:8], file=sys.stderr, flush=True)
        return JSONResponse({"ok": True}, headers=HEADERS)

    @app.post("/unlock/options")
    async def unlock_options(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:
            key = store.get(json.loads(await _body(request, 1024)).get("id"))
        except (ValueError, AttributeError):
            return refuse("bad request")
        if not key or not key.get("box"):
            return refuse("no passphrase is stored under this key")
        if keys_only and key.get("synced"):
            return refuse("this is a synced passkey and this site accepts security keys only")
        options, challenge = passkeys.authentication_options(rp, key)
        state.put_challenge(token, "unlock", (challenge, key["id"]))
        return Response(options, media_type="application/json", headers=HEADERS)

    @app.post("/unlock/box")
    async def unlock_box(request: Request):
        """After a verified touch, the stored copy for the browser to decrypt, and one
        minute to send the passphrase on."""
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        started = state.take_challenge(token, "unlock")
        if not started:
            return refuse("this expired; start again")
        challenge, key_id = started
        key = store.get(key_id)
        try:
            credential = json.loads(await _body(request, 8192))["credential"]
            if not key or not key.get("box"):
                raise passkeys.Refused("no passphrase is stored under this key")
            count = passkeys.verify_assertion(rp, origins, credential, challenge, key,
                                              security_keys_only=keys_only)
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            print("bits-admin: unlock refused (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            return refuse("the key's answer did not verify")
        store.update(key_id, sign_count=count)
        state.put_challenge(token, "unlock-grant", key_id, GRANT_TTL)
        return JSONResponse({"iv": key["box"]["iv"], "ct": key["box"]["ct"]}, headers=HEADERS)

    @app.post("/unlock")
    async def unlock(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:  # before the grant is taken: a bad request does not use it up
            passphrase = json.loads(await _body(request, 2048))["passphrase"]
            if not isinstance(passphrase, str) or not 0 < len(passphrase.encode()) <= 1024:
                raise ValueError("no passphrase")
        except (ValueError, TypeError, KeyError):
            return refuse("bad request")
        key_id = state.take_challenge(token, "unlock-grant")
        if not key_id:
            return refuse("touch the key first (or it was more than a minute ago)")
        answer = helper({"op": "unlock-signer", "passphrase": passphrase})
        if not answer.get("ok"):
            print("bits-admin: unlock with key %s refused: %s" % (key_id[:8], answer.get("error")),
                  file=sys.stderr, flush=True)
            return refuse("not unlocked: %s" % answer.get("error", "the host helper refused"))
        print("bits-admin: signer unlocked with key %s (signing key %s)" % (key_id[:8], answer.get("key_id16")),
              file=sys.stderr, flush=True)
        return JSONResponse({"ok": True, "key_id16": answer.get("key_id16", "")}, headers=HEADERS)

    @app.post("/passkeys/delete")
    async def passkey_delete(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        try:
            key_id = (parse_qs((await _body(request, 1024)).decode(errors="replace")).get("id") or [""])[0]
        except ValueError:
            return refuse("bad request")
        if store.remove(key_id):
            print("bits-admin: key %s removed" % key_id[:8], file=sys.stderr, flush=True)
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
