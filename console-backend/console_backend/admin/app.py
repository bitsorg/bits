# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin (ADR bits-services 0001): the site's admin page, reached only through an
SSH tunnel to its unix socket: sign-in with a one-time setup code (a file only the
bits-admin group reads), a session, /healthz, the host helper's status, the keys
(passkeys.py) and, with one of them, unlocking the signer through the host helper.

A page from another site in the admin's browser cannot use the tunnel: requests must
name an allowed Host (no DNS rebinding) and a POST an allowed Origin.
"""

import hashlib
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

from . import events, passkeys
from . import hosts as hosts_page_view
from . import service_secrets as secrets_page_view
from . import settings as settings_page_view

HELPER_SOCKET = os.environ.get("BITS_ADMIN_HELPER_SOCKET", "/run/bits-admin-helper/helper.sock")
CODE_FILE = os.environ.get("BITS_ADMIN_CODE_FILE", "/run/bits-admin/setup-code")
# Left by sudo make admin-reset: at start, the setup code works again for CODE_TTL.
RESET_FILE = os.environ.get("BITS_ADMIN_RESET_FILE", "/run/bits-admin/reset")
ORIGINS = [o.strip().rstrip("/") for o in
           os.environ.get("BITS_ADMIN_ORIGINS", "http://bits-admin.localhost:8800").split(",") if o.strip()]


def _on(value):
    """A setting is on unless empty, 0, false or no: an unknown value errs to strict."""
    return (value or "").strip().lower() not in ("", "0", "false", "no")


# Off by default: Touch ID, Face ID and other synced passkeys may unlock too.
SECURITY_KEYS_ONLY = _on(os.environ.get("BITS_ADMIN_SECURITY_KEYS_ONLY"))
CODE_TTL, SESSION_TTL, CHALLENGE_TTL, GRANT_TTL = 1800, 12 * 3600, 300, 60
SIGNINS = 32  # sign-ins in progress at once; anyone at the socket may start one
INVITE_TTL, INVITES = 3600, 10  # an invite adds one admin's first key, within an hour
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
        self.invites = {}  # code -> (name, until, invited by), each used once
        self.invited_by = {}  # an invite's session -> who invited
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

    def add_invite(self, name, by, admins):
        """A new invite's code; None if *name* is an admin's, invited or mid-enrolment
        already, or too many are open."""
        code = new_code()
        with self.lock:
            self.invites = {c: v for c, v in self.invites.items() if v[1] >= self.now()}
            busy = {v[0] for v in self.invites.values()} | {
                n for until, n, how in self.sessions.values() if how == "invite" and until >= self.now()}
            if name in set(admins) | busy or len(self.invites) >= INVITES:
                return None
            self.invites[_plain(code)] = (name, self.now() + INVITE_TTL, by)
        return code

    def pending_invites(self):
        with self.lock:
            return sorted((v for v in self.invites.values() if v[1] >= self.now()), key=lambda v: v[1])

    def close_sessions_of(self, name):
        """*name*'s sessions, and every invite they made, used or not."""
        with self.lock:
            self.invites = {c: v for c, v in self.invites.items() if v[2] != name}
            gone = {t for t, v in self.sessions.items() if v[1] == name or self.invited_by.get(t) == name}
            self.sessions = {t: v for t, v in self.sessions.items() if t not in gone}

    def cancel_invite(self, name):
        """The invite of *name*, and its session if it was used but no key added yet."""
        with self.lock:
            self.invites = {c: v for c, v in self.invites.items() if v[0] != name}
            self.sessions = {t: v for t, v in self.sessions.items() if not (v[1] == name and v[2] == "invite")}

    def take_invite(self, given):
        """(name, invited by) of a valid invite, which is used up; a miss counts as a
        wrong code towards the lockout."""
        with self.lock:
            for code, (name, until, by) in list(self.invites.items()):
                if hmac.compare_digest(_plain(given), code):
                    del self.invites[code]
                    if until >= self.now():
                        return name, by
            self.failures.append(self.now())
            return None

    def open_session(self, name, how, ttl=SESSION_TTL):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = (self.now() + ttl, name, how)
        return token

    def how(self, token):
        """How the session signed in: "setup code" or "key <label>"."""
        with self.lock:
            return self.sessions.get(token or "", (0, None, ""))[2]

    def who(self, token):
        """The signed-in admin's name, or None (an expired session is dropped)."""
        with self.lock:
            until, name, _ = self.sessions.get(token or "", (None, None, None))
            if until is None or until < self.now():
                self.sessions.pop(token or "", None)
                for key in [k for k in self.challenges if k[0] == token]:
                    del self.challenges[key]
                return None
            return name

    def signed_in(self, token):
        return self.who(token) is not None

    def close_session(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)
            for key in [k for k in self.challenges if k[0] == token]:
                del self.challenges[key]

    def put_challenge(self, token, purpose, value, ttl=CHALLENGE_TTL):
        with self.lock:
            for key in [k for k, (_, until) in self.challenges.items() if until < self.now()]:
                del self.challenges[key]  # abandoned sign-ins would pile up otherwise
            self.challenges[(token, purpose)] = (value, self.now() + ttl)

    def open_challenges(self, purpose):
        with self.lock:
            return sum(p == purpose and until >= self.now() for (_, p), (_, until) in self.challenges.items())

    def peek_challenge(self, token, purpose):
        with self.lock:
            value, until = self.challenges.get((token, purpose), (None, 0))
            return value if until >= self.now() else None

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


def _page(title, body, head=""):
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>%s<title>%s</title>"
        "<style>body{font:15px system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
        "pre{background:#f3f4f6;padding:1rem;overflow:auto}input,button{font:inherit;padding:.3rem .6rem}"
        "</style><h1>bits-admin</h1>%s" % (head, html.escape(title), body), headers=HEADERS)


def _keys_html(keys):
    if not keys:
        return "<p>None yet.</p>"
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td><td>"
                   "<form method=post action=/passkeys/delete><input type=hidden name=id value=\"%s\"><button>"
                   "Remove</button></form></td></tr>" % (html.escape(k.get("owner") or "–"), html.escape(k["label"]),
                                                    "synced passkey" if k.get("synced") else "device-bound",
                                                    html.escape(k["added"]), html.escape(k["id"][:8]),
                                                    html.escape(k["box"]["stored"]) if k.get("box") else "–",
                                                    html.escape(k["id"]))
                   for k in keys)
    choices = "".join("<option value=\"%s\">%s</option>" % (html.escape(k["id"]), html.escape(k["label"]))
                      for k in keys)
    return ("<table><tr><th>Owner<th>Label<th>Kind<th>Added<th>Id<th>Passphrase stored<th></tr>%s</table>"
            "<h3>Store the signer's passphrase under a key</h3><p>The signer's passphrase, the one "
            "<code>make unlock</code> asks for: typed once per key, after that Unlock and the key are "
            "enough.</p><form id=box><select id=box-key>%s</select> "
            "<input type=password id=passphrase autocomplete=off size=30 placeholder=\"signer passphrase\"> "
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


RESET_MAX_AGE = 600  # an older reset file is left from a failed admin-reset


def take_reset(path, owner=0):
    """True once for a fresh reset file of *owner* (root), which is removed; any other
    file there is removed and ignored. Not removable: ignored, or every start would reopen."""
    try:
        st = os.lstat(path)
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        print("bits-admin: reset ignored, cannot remove %s: %s" % (path, e), file=sys.stderr, flush=True)
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or time.time() - st.st_mtime > RESET_MAX_AGE:
        print("bits-admin: reset ignored: %s is not a fresh file of uid %d" % (path, owner),
              file=sys.stderr, flush=True)
        return False
    return True


def create_app(state=None, helper=ask_helper, origins=None, store=None, security_keys_only=None,
               reset_file=RESET_FILE, reset_owner=0, chain=None, outside=None):
    state = state or State()
    chain = chain or events.Chain()

    def audit(actor, kind, subject, **payload):
        """One event in the chain; a failure is logged and does not undo the action."""
        try:
            chain.append(kind, subject, actor or "-", payload)
        except (OSError, ValueError) as e:
            print("bits-admin: cannot record %s: %s" % (kind, e), file=sys.stderr, flush=True)
        if outside:
            outside.wake.set()  # its copy in audit-log follows now

    reset_until = 0
    if take_reset(reset_file, reset_owner):
        state.renew_code()
        reset_until = state.now() + CODE_TTL
        print("bits-admin: reset: the setup code works for %d minutes" % (CODE_TTL // 60),
              file=sys.stderr, flush=True)
        audit("root", "admin.reset", "setup code", minutes=CODE_TTL // 60)
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
        """A full session; an invite's may only add its key (enrol_session)."""
        token = request.cookies.get(COOKIE)
        return token if state.signed_in(token) and state.how(token) != "invite" else None

    def enrol_session(request):
        token = request.cookies.get(COOKIE)
        return token if state.signed_in(token) else None

    async def form(request):
        return {k: v[0] for k, v in parse_qs((await _body(request, 1024)).decode(errors="replace")).items()}

    def log(request, text):
        print("bits-admin: %s: %s" % (state.who(request.cookies.get(COOKIE)) or "-", text),
              file=sys.stderr, flush=True)

    def refuse(message, status=400):
        return JSONResponse({"error": message}, status_code=status, headers=HEADERS)

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok"}, headers=HEADERS)

    @app.get("/admin.js")
    def script():
        return Response(SCRIPT, media_type="text/javascript", headers=HEADERS)

    def signers():
        return [k for k in store.load() if k.get("owner") and not (keys_only and k.get("synced"))]

    def code_allowed():
        """The setup code: while no admin key can sign in, or for a while after a reset."""
        return not signers() or state.now() < reset_until

    def admins_html(me):
        owners = {}
        for k in store.load():
            if k.get("owner"):
                owners[k["owner"]] = owners.get(k["owner"], 0) + 1
        rows = "".join("<li>%s (%d key%s)%s</li>" % (
            html.escape(n), c, "" if c == 1 else "s", "" if n == me else
            " <form method=post action=/admins/remove style=display:inline><input type=hidden name=name "
            "value=\"%s\"><button>Remove</button></form>" % html.escape(n)) for n, c in sorted(owners.items()))
        pending = "".join(
            "<li>%s, invited by %s, until %s UTC <form method=post action=/invites/cancel style=display:inline>"
            "<input type=hidden name=name value=\"%s\"><button>Cancel</button></form></li>"
            % (html.escape(n), html.escape(by), time.strftime("%H:%M", time.gmtime(until)), html.escape(n))
            for n, until, by in state.pending_invites())
        try:
            trail = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts_ms"] / 1000)), html.escape(e["actor_id"]),
                html.escape(e["kind"]), html.escape(e["subject"][:24])) for e in reversed(chain.rows()[-20:]))
        except Exception:  # never the whole page for it
            trail = ("<tr><td colspan=4>unreadable: run <code>python3 -m console_backend.admin.events "
                     "verify</code></td></tr>")
        if outside is None:
            copy = "Its copy in audit-log: off."
        else:
            st = outside.status
            copy = "Its copy in audit-log: %s%s%s%s." % (
                "<b>DIVERGED</b>" if st["state"] == "diverged" else html.escape(st["state"]),
                "" if not isinstance(st["seq"], int) else " at event %d" % st["seq"],
                ": " + html.escape(st["error"]) if st["error"] else "",
                "" if not st.get("checked") else
                " (checked %s UTC)" % time.strftime("%H:%M:%S", time.gmtime(st["checked"])))
        return ("<h2>Admins</h2><ul>%s%s</ul><form method=post action=/invites><input name=name maxlength=40 "
                "size=20 placeholder=\"their name\"> <button>Invite an admin</button></form>"
                "<h2>Recent events</h2><p>%s</p><table><tr><th>UTC<th>Who<th>What<th>Subject</tr>%s</table>"
                % (rows, pending, copy, trail))

    @app.get("/")
    def index(request: Request):
        token = request.cookies.get(COOKIE)
        if signed_in(request) and state.how(token) == "invite":
            return _page("Welcome", "<p>Welcome, %s. Add your key: a passkey on this device, your phone "
                         "(the browser shows a QR code), or a security key. Then sign in with it.</p>"
                         "<form id=enrol><input id=label maxlength=%d size=30 placeholder=\"e.g. YubiKey "
                         "5C, MacBook Touch ID\"> <button>Add key</button></form><p id=status></p>"
                         "<script src=/admin.js></script>" % (html.escape(state.who(token)), passkeys.LABEL_MAX))
        if not signed_in(request) and signers():
            code = ("<p>Or with the setup code (after <code>sudo make admin-reset</code>):</p>"
                    "<form method=post action=/login><input name=name autocomplete=username size=20 "
                    "maxlength=40 placeholder=\"your name\"> <input name=code autocomplete=off size=22 "
                    "placeholder=XXXX-XXXX-XXXX-XXXX> <button>Sign in</button></form>" if code_allowed() else
                    "<p>Lost your keys? On the host: <code>sudo make admin-reset</code>, then "
                    "<code>make admin-code</code>.</p>")
            invite = ("<p>Invited? Enter the invite:</p><form method=post action=/invite><input name=code "
                      "autocomplete=off size=22 placeholder=XXXX-XXXX-XXXX-XXXX> <button>Use the invite</button>"
                      "</form>")
            return _page("Sign in", "<form id=signin><button>Sign in with your key</button></form>"
                         "<p id=status></p>%s%s<script src=/admin.js></script>" % (invite, code))
        if not signed_in(request):
            return _page("Sign in", "<p>Enter your name and the setup code: <code>make admin-code"
                         "</code> on the host prints it. Each code signs in once.</p>"
                         "<form method=post action=/login><input name=name autocomplete=username "
                         "autofocus size=20 maxlength=40 placeholder=\"your name\"> <input name=code "
                         "autocomplete=off size=22 placeholder=XXXX-XXXX-XXXX-XXXX> <button>Sign in"
                         "</button></form>")
        status = helper({"op": "status"})
        keys = store.load()
        boxed = [k for k in keys if k.get("box") and not (keys_only and k.get("synced"))]
        unlock = ""
        if status.get("signer") != "loaded" and keys and not boxed:
            unlock = ("<h2>Unlock the signer</h2><p>%s</p>" % (
                "Only security keys may unlock here (BITS_ADMIN_SECURITY_KEYS_ONLY): store the "
                "signer's passphrase under one below." if any(k.get("box") for k in keys) else
                "No key can unlock yet: store the signer's passphrase under one below, then "
                "unlock here."))
        if status.get("signer") != "loaded" and boxed:
            unlock = ("<h2>Unlock the signer</h2><form id=unlock><select id=unlock-key>%s</select> "
                      "<button>Unlock</button></form>" % "".join(
                          "<option value=\"%s\">%s</option>" % (html.escape(k["id"]), html.escape(k["label"]))
                          for k in boxed))
        return _page("Site", "<p>Signed in as %s (%s). <a href=/hosts>Hosts</a> &middot; <a href=/settings>Settings</a> &middot; <a href=/secrets>Secrets</a></p>"
                     "<h2>Host helper</h2><pre>%s</pre>%s"
                     "<h2>Keys</h2><p>%s</p>%s<form id=enrol><input id=label maxlength=%d size=30 "
                     "placeholder=\"e.g. YubiKey 5C, MacBook Touch ID\"> <button>Add key</button>"
                     "</form><p id=status></p>%s<form method=post action=/logout><button>Sign out"
                     "</button></form><script src=/admin.js></script>"
                     % (html.escape(state.who(request.cookies.get(COOKIE)) or ""),
                        html.escape(state.how(request.cookies.get(COOKIE))),
                        html.escape(json.dumps(status, indent=1)), unlock,
                        "Security keys only (BITS_ADMIN_SECURITY_KEYS_ONLY)." if keys_only else
                        "Security keys (YubiKey) and synced passkeys (Touch ID, Face ID).",
                        _keys_html(keys), passkeys.LABEL_MAX, admins_html(state.who(token))))

    def show_hosts(checks=None, at=None):
        answer = helper({"op": "hosts"})
        if not answer.get("ok"):
            body = "<p>The host helper could not say: %s</p>" % html.escape(str(answer.get("error", "")))
        else:
            body = hosts_page_view.render(answer, helper({"op": "status"}), checks, at)
        return _page("Hosts", "<p><a href=/>Back</a> &middot; <form method=post action=/hosts/check "
                     "style=display:inline><button>Check the connections</button></form></p>" + body,
                     head="<meta http-equiv=refresh content=\"60;url=/hosts\">")

    @app.get("/settings")
    def settings_page(request: Request):
        if not session(request):
            return RedirectResponse("/", status_code=303, headers=HEADERS)
        answer = helper({"op": "settings"})
        if not answer.get("ok"):
            body = "<p>The host helper could not say: %s</p>" % html.escape(str(answer.get("error", "")))
        else:
            body = settings_page_view.render(answer)
        return _page("Settings", "<p><a href=/>Back</a></p>" + body)

    @app.post("/settings/plan")
    async def settings_plan(request: Request):
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:
            form = {k: v[0] for k, v in parse_qs((await _body(request, 131072)).decode(errors="replace"),
                                                 keep_blank_values=True).items()}  # emptied: to unset
        except ValueError:
            return refuse("bad request")
        answer = helper({"op": "settings"})
        if not answer.get("ok"):
            return _page("Settings", "<p>The host helper could not say: %s</p>" % html.escape(str(answer.get("error"))))
        stale = _page("Settings", "<p>.env changed since you opened this page. "
                      "<a href=/settings>Open it again</a> and redo your changes.</p>")
        if not answer.get("base") or form.get("base") != answer["base"]:  # it would undo what changed since
            return stale
        if "profile" in form:  # a site-profile.yaml, chosen or pasted
            text = form["profile"].replace("\r\n", "\n")
            if not text.strip():
                return _page("Settings", "<p>Nothing to load: choose or paste a site-profile.yaml.</p>"
                             "<p><a href=/settings>Back</a></p>")
            changes = {"profile": text}
        else:
            changes = settings_page_view.changes_from(form, answer)
        planned = helper({"op": "settings-plan", "changes": changes})
        if not planned.get("ok"):
            return _page("Settings", "<p>Not possible: %s</p><p><a href=/settings>Back</a></p>"
                         % html.escape(str(planned.get("error", ""))))
        if planned.get("base") != answer["base"]:  # changed between the two questions
            return stale
        plan_id = hashlib.sha256(json.dumps([planned.get("base"), changes], sort_keys=True).encode()).hexdigest()[:16]
        state.put_challenge(token, "settings", {"changes": changes, "base": planned.get("base"), "id": plan_id})
        return _page("Settings", settings_page_view.plan_html(planned, plan_id, profile="profile" in changes))

    def own_keys(token):
        who = state.who(token)
        return [k for k in store.load() if k.get("owner") == who and not (keys_only and k.get("synced"))]

    def apply_options(request, purpose):
        """The options for the touch that confirms the admin's latest *purpose* plan."""
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        keys = own_keys(token)
        if not keys:
            return refuse("you have no key to confirm with")
        options, challenge = passkeys.authentication_options(rp, keys)
        state.put_challenge(token, purpose + "-apply", challenge)
        return Response(options, media_type="application/json", headers=HEADERS)

    async def confirmed(request, purpose):
        """(None, token, key, plan) once the admin's own key confirmed their latest *purpose*
        plan, which is then used up; else (the refusal, None, None, None)."""
        no = lambda message, status=400: (refuse(message, status), None, None, None)
        token = session(request)
        if not token:
            return no("not signed in", 401)
        try:
            body = json.loads(await _body(request, 8192))
            credential, plan_id = body["credential"], str(body.get("plan", ""))
            key = next((k for k in own_keys(token) if k["id"] == credential.get("id")), None)
        except (ValueError, TypeError, KeyError, AttributeError):
            return no("bad request")
        challenge, planned = state.take_challenge(token, purpose + "-apply"), state.peek_challenge(token, purpose)
        if not challenge or not planned:
            return no("this expired; plan again")
        if planned["id"] != plan_id:  # planned again elsewhere since
            return no("this is not your latest plan; plan again")
        if not key:
            return no("confirm with one of your own keys")
        try:
            count = passkeys.verify_assertion(rp, origins, credential, challenge, key, security_keys_only=keys_only)
        except passkeys.Refused as e:
            return no(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            log(request, "%s apply refused (%s)" % (purpose, type(e).__name__))
            return no("the key's answer did not verify")
        store.update(key["id"], sign_count=count)
        if state.take_challenge(token, purpose) is not planned:
            return no("this expired; plan again")
        return None, token, key, planned

    @app.post("/settings/apply/options")
    def settings_apply_options(request: Request):
        return apply_options(request, "settings")

    @app.post("/settings/apply")
    async def settings_apply(request: Request):
        refusal, token, key, planned = await confirmed(request, "settings")
        if refusal:
            return refusal
        done = helper({"op": "settings-apply", "changes": planned["changes"], "base": planned["base"]})
        if not done.get("ok"):
            return refuse("not applied: %s" % done.get("error", "the host helper refused"))
        keys, profile = [str(k) for k in done.get("keys") or []], planned["changes"].get("profile")
        log(request, "applied settings%s: %s%s" % (" from a site-profile.yaml" if profile else "",
                                                    ", ".join(keys) or "nothing changed",
                                                    "; " + str(done["profile_error"]) if done.get("profile_error") else ""))
        audit(state.who(token), "admin.settings_applied", ".env", keys=keys, key=key["id"],
              profile_sha256=hashlib.sha256(profile.encode()).hexdigest() if profile else None,
              profile_saved=not done.get("profile_error") if profile else None)
        return JSONResponse({"ok": True, "keys": keys, "backup": done.get("backup"), "profile": done.get("profile"),
                             "profile_error": done.get("profile_error")}, headers=HEADERS)

    @app.get("/settings/site-profile.yaml")
    def settings_download(request: Request):
        if not session(request):
            return RedirectResponse("/", status_code=303, headers=HEADERS)
        answer = helper({"op": "settings"})
        if not answer.get("ok"):
            return refuse("the host helper could not say", 502)
        return Response(settings_page_view.profile_yaml(answer), media_type="text/yaml",
                        headers=dict(HEADERS, **{"Content-Disposition": "attachment; filename=site-profile.yaml"}))

    @app.get("/hosts")
    def hosts_page(request: Request):
        if not session(request):
            return RedirectResponse("/", status_code=303, headers=HEADERS)
        return show_hosts()

    @app.post("/hosts/check")
    def hosts_check(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        answer = helper({"op": "check"})
        if not answer.get("ok"):
            return _page("Hosts", "<p>The check failed: %s</p><p><a href=/hosts>Back</a></p>"
                         % html.escape(str(answer.get("error", ""))))
        return show_hosts(answer.get("results") or [], answer.get("at"))

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
        form = parse_qs(body.decode(errors="replace"))
        code, name = (form.get("code") or [""])[0], passkeys.clean_name((form.get("name") or [""])[0])
        if not code_allowed():
            return _page("Sign in", "<p>The setup code is off: an admin has a key. Sign in with it, "
                         "or on the host <code>sudo make admin-reset</code>.</p><p><a href=/>Back</a></p>")
        if not name:  # checked first, so a missing name does not use up the code
            return _page("Sign in", "<p>Give your name with the code.</p><p><a href=/>Again</a></p>")
        if not state.check_code(code):
            return _page("Sign in", "<p>Wrong or expired code.</p><p><a href=/>Again</a></p>")
        print("bits-admin: %s: signed in with the setup code" % name, file=sys.stderr, flush=True)
        audit(name, "admin.signed_in", name, how="setup code")
        try:
            claimed = store.claim_unowned(name)
        except (OSError, ValueError) as e:  # still signed in: the code is used up
            print("bits-admin: cannot give keys an owner: %s" % e, file=sys.stderr, flush=True)
            claimed = 0
        if claimed:
            print("bits-admin: %s: %d key(s) without an owner are now theirs" % (name, claimed),
                  file=sys.stderr, flush=True)
            audit(name, "admin.keys_claimed", name, keys=claimed)
        r = RedirectResponse("/", status_code=303, headers=HEADERS)
        r.set_cookie(COOKIE, state.open_session(name, "setup code"), max_age=SESSION_TTL,
                     httponly=True, samesite="strict", path="/")
        return r

    @app.post("/invites")
    async def invite_create(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        try:
            name = passkeys.clean_name((await form(request)).get("name", ""))
        except ValueError:
            return refuse("bad request")
        code = name and state.add_invite(name, state.who(request.cookies.get(COOKIE)),
                                         [k.get("owner") for k in store.load()])
        if not code:
            return _page("Invite", "<p>Give a name that is not an admin's or invited already (at most "
                         "%d invites open).</p><p><a href=/>Back</a></p>" % INVITES)
        log(request, "invited %s" % name)
        audit(state.who(request.cookies.get(COOKIE)), "admin.invited", name)
        return _page("Invite", "<p>Give %s this invite, valid for an hour and usable once:</p><pre>%s</pre>"
                     "<p>They open bits-admin through their own SSH tunnel (they need the host's "
                     "<code>bits-admin</code> group) and enter it under \"Invited?\".</p><p><a href=/>Back"
                     "</a></p>" % (html.escape(name), code))

    @app.post("/invites/cancel")
    async def invite_cancel(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        try:
            name = passkeys.clean_name((await form(request)).get("name", ""))
        except ValueError:
            return refuse("bad request")
        state.cancel_invite(name)
        log(request, "cancelled the invite of %s" % name)
        audit(state.who(request.cookies.get(COOKIE)), "admin.invite_cancelled", name)
        return RedirectResponse("/", status_code=303, headers=HEADERS)

    @app.post("/admins/remove")
    async def admin_remove(request: Request):
        if not session(request):
            return refuse("not signed in", 401)
        try:
            name = passkeys.clean_name((await form(request)).get("name", ""))
        except ValueError:
            return refuse("bad request")
        if name == state.who(request.cookies.get(COOKIE)):
            return _page("Admins", "<p>You cannot remove yourself; another admin can.</p><p><a href=/>Back"
                         "</a></p>")
        if not any(k.get("owner") == name for k in store.load()):
            return _page("Admins", "<p>No such admin.</p><p><a href=/>Back</a></p>")
        state.close_sessions_of(name)  # first: none of theirs can add a key or sign in meanwhile
        removed = store.remove_owner(name)
        log(request, "removed admin %s (%d key%s)" % (name, removed, "" if removed == 1 else "s"))
        audit(state.who(request.cookies.get(COOKIE)), "admin.removed", name, keys=removed)
        return RedirectResponse("/", status_code=303, headers=HEADERS)

    @app.post("/invite")
    async def invite_use(request: Request):
        if state.locked_out():
            return _page("Sign in", "<p>Too many wrong codes: try again in 10 minutes.</p>")
        try:
            got = state.take_invite((await form(request)).get("code", ""))
        except ValueError:
            got = None
        if not got:
            return _page("Sign in", "<p>Wrong, used or expired invite.</p><p><a href=/>Again</a></p>")
        name, by = got
        print("bits-admin: %s: signed in with an invite from %s" % (name, by), file=sys.stderr, flush=True)
        audit(name, "admin.signed_in", name, how="invite", invited_by=by)
        token = state.open_session(name, "invite", INVITE_TTL)
        state.invited_by[token] = by
        r = RedirectResponse("/", status_code=303, headers=HEADERS)
        r.set_cookie(COOKIE, token, max_age=INVITE_TTL, httponly=True, samesite="strict", path="/")
        return r

    @app.post("/signin/options")
    def signin_options():
        keys = [k for k in store.load() if k.get("owner") and not (keys_only and k.get("synced"))]
        if not keys:
            return refuse("no key can sign in yet; use the setup code")
        if state.open_challenges("signin") >= SIGNINS:
            return refuse("too many sign-ins in progress; try again in a few minutes", 429)
        options, challenge = passkeys.authentication_options(rp, keys)
        ticket = secrets.token_urlsafe(24)  # stands for the session not yet open
        state.put_challenge(ticket, "signin", challenge)
        return JSONResponse(dict(json.loads(options), ticket=ticket), headers=HEADERS)

    @app.post("/signin")
    async def signin(request: Request):
        try:
            body = json.loads(await _body(request, 8192))
            ticket, credential = str(body["ticket"]), body["credential"]
            key = store.get(credential["id"])
        except (ValueError, TypeError, KeyError):
            return refuse("bad request")
        challenge = state.take_challenge(ticket, "signin")
        if not challenge:
            return refuse("this expired; start again")
        if not key or not key.get("owner"):
            return refuse("this key is not enrolled here")
        if keys_only and key.get("synced"):
            return refuse("this is a synced passkey and this site accepts security keys only")
        try:
            count = passkeys.verify_assertion(rp, origins, credential, challenge, key,
                                              security_keys_only=keys_only)
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            print("bits-admin: sign-in refused (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            return refuse("the key's answer did not verify")
        store.update(key["id"], sign_count=count)
        print("bits-admin: %s: signed in with key %s" % (key["owner"], key["id"][:8]),
              file=sys.stderr, flush=True)
        audit(key["owner"], "admin.signed_in", key["owner"], how="key", key=key["id"])
        r = JSONResponse({"ok": True}, headers=HEADERS)
        r.set_cookie(COOKIE, state.open_session(key["owner"], "key " + key["label"]),
                     max_age=SESSION_TTL, httponly=True, samesite="strict", path="/")
        return r

    @app.post("/passkeys/options")
    async def passkey_options(request: Request):
        token = enrol_session(request)
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
        token = enrol_session(request)
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
            key["owner"] = state.who(token)
            if not key["owner"]:
                return refuse("not signed in", 401)
        except passkeys.Refused as e:
            return refuse(str(e))
        except Exception as e:  # py_webauthn raises several types, all a failed check
            log(request, "enrolment refused (%s)" % type(e).__name__)
            return refuse("the key's answer did not verify")
        try:
            store.add(key)
        except passkeys.Refused as e:
            return refuse(str(e))
        except OSError as e:
            log(request, "cannot save %s: %s" % (store.path, e))
            return refuse("bits-admin could not save the key (see its log)", 500)
        kind = "synced passkey" if key["synced"] else "device-bound"
        log(request, "key %s (%s, %s) enrolled" % (key["id"][:8], key["label"], kind))
        audit(key["owner"], "admin.key_added", key["id"], label=key["label"], synced=key["synced"])
        if state.how(token) == "invite":  # done: from now on they sign in with the key
            state.close_session(token)
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
        options, challenge = passkeys.authentication_options(rp, [key])
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
            log(request, "passphrase not stored (%s)" % type(e).__name__)
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
            log(request, "cannot save %s: %s" % (store.path, e))
            return refuse("bits-admin could not save it (see its log)", 500)
        log(request, "passphrase stored under key %s" % key_id[:8])
        audit(state.who(token), "admin.passphrase_stored", key_id)
        return JSONResponse({"ok": True}, headers=HEADERS)

    async def box_options(request, purpose, own=False):
        """Options to touch a key that holds the passphrase, for *purpose*; own: one of the
        signed-in admin's own keys."""
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:
            key = store.get(json.loads(await _body(request, 1024)).get("id"))
        except (ValueError, AttributeError):
            return refuse("bad request")
        if not key or not key.get("box"):
            return refuse("no passphrase is stored under this key")
        if own and key.get("owner") != state.who(token):
            return refuse("use one of your own keys")
        if keys_only and key.get("synced"):
            return refuse("this is a synced passkey and this site accepts security keys only")
        options, challenge = passkeys.authentication_options(rp, [key])
        state.put_challenge(token, purpose, (challenge, key["id"]))
        return Response(options, media_type="application/json", headers=HEADERS)

    async def box_open(request, purpose):
        """After a verified touch, the stored copy for the browser to decrypt, and one
        minute (the <purpose>-grant) to send the passphrase on."""
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        started = state.take_challenge(token, purpose)
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
            log(request, "%s refused (%s)" % (purpose, type(e).__name__))
            return refuse("the key's answer did not verify")
        store.update(key_id, sign_count=count)
        state.put_challenge(token, purpose + "-grant", key_id, GRANT_TTL)
        return JSONResponse({"iv": key["box"]["iv"], "ct": key["box"]["ct"]}, headers=HEADERS)

    @app.post("/unlock/options")
    async def unlock_options(request: Request):
        return await box_options(request, "unlock")

    @app.post("/unlock/box")
    async def unlock_box(request: Request):
        return await box_open(request, "unlock")

    @app.get("/secrets")
    def secrets_page(request: Request):
        token = session(request)
        if not token:
            return RedirectResponse("/", status_code=303, headers=HEADERS)
        keys = [k for k in store.load() if k.get("owner") == state.who(token) and k.get("box")
                and not (keys_only and k.get("synced"))]
        return _page("Secrets", "<p><a href=/>Back</a></p>" + secrets_page_view.render(keys))

    @app.post("/secrets/options")
    async def secrets_options(request: Request):
        return await box_options(request, "secrets", own=True)

    @app.post("/secrets/box")
    async def secrets_box(request: Request):
        return await box_open(request, "secrets")

    @app.post("/secrets")
    async def secrets_change(request: Request):
        """One step on the service secrets, with the passphrase the browser opened."""
        token = session(request)
        if not token:
            return refuse("not signed in", 401)
        try:  # before the grant is taken: a bad request does not use it up
            body = json.loads(await _body(request, 16384))
            passphrase, action = body["passphrase"], body.get("action")
            if not isinstance(passphrase, str) or not 0 < len(passphrase.encode()) <= 1024 \
                    or action not in secrets_page_view.ACTIONS:
                raise ValueError("no passphrase or no such step")
        except (ValueError, TypeError, KeyError, AttributeError):
            return refuse("bad request")
        problem = secrets_page_view.step_problem(action, body.get("slot"), body.get("value"))
        if problem:
            return refuse(problem)
        key_id = state.take_challenge(token, "secrets-grant")
        if not key_id:
            return refuse("touch the key first (or it was more than a minute ago)")
        ask = {"op": secrets_page_view.ACTIONS[action], "passphrase": passphrase}
        if action != "list":
            ask["slot"] = body["slot"]
        if action == "set":
            ask["value"] = body.get("value")
        answer = helper(ask)
        if not answer.get("ok"):
            log(request, "secrets %s refused: %s" % (action, answer.get("error")))
            return refuse(str(answer.get("error", "the host helper refused")))
        if action != "list":
            log(request, "secret %s %s with key %s%s" % (ask["slot"], "set" if action == "set" else "removed",
                                                         key_id[:8], "; not pushed" if answer.get("push_error") else ""))
            audit(state.who(token), "admin.secret_set" if action == "set" else "admin.secret_removed",
                  "service-secrets", slot=ask["slot"], key=key_id, pushed=not answer.get("push_error"))
        return JSONResponse({"ok": True, "slots": [str(s) for s in answer.get("slots") or []],
                             "push_error": answer.get("push_error")}, headers=HEADERS)

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
            log(request, "unlock with key %s refused: %s" % (key_id[:8], answer.get("error")))
            error = str(answer.get("error", ""))  # kept forever: a reason, not the helper's text
            reason = ("locked out" if "too many" in error else
                      "wrong passphrase" if "wrong passphrase" in error else
                      "already loaded" if "already holds" in error else "refused")
            audit(state.who(token), "admin.unlock_refused", "signer", key=key_id, reason=reason)
            return refuse("not unlocked: %s" % answer.get("error", "the host helper refused"))
        log(request, "signer unlocked with key %s (signing key %s)"
            % (key_id[:8], answer.get("key_id16")))
        audit(state.who(token), "admin.signer_unlocked", "signer", key=key_id,
              key_id16=str(answer.get("key_id16", "")))
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
            log(request, "key %s removed" % key_id[:8])
            audit(state.who(request.cookies.get(COOKIE)), "admin.key_removed", key_id)
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
    chain = events.Chain()
    outside = events.outside_from_env(chain)
    if outside:
        threading.Thread(target=outside.run, daemon=True).start()
    uvicorn.run(create_app(chain=chain, outside=outside), fd=sock.fileno(), server_header=False,
                proxy_headers=False)
