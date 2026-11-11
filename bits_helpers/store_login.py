# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits login: a store token for one group, approved with a passkey.

Asks bits-console for a token, shows the approve link (QR) and the code to compare,
waits, and writes the token to ~/.bits/token (bits-store docs/TOKEN.md) for clients
of the store to send as `Authorization: Bearer`. The token is collected with a poll
secret only this process knows: the approve link alone does not give it out.
"""

import argparse
import datetime
import getpass
import os
import socket
import sys
import tempfile
import time
import urllib.parse

from bits_helpers import sign_console

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".bits", "token")


def login(console, group, token_file=TOKEN_FILE, timeout=600, cafile=None, insecure=False):
    """Get *group*'s store token from *console* and write it to *token_file*.
    Returns the user it was issued to; exits on refusal, expiry or timeout."""
    base = console.rstrip("/")
    sign_console._check_console_url(base, insecure)
    sign_console._set_tls(cafile, insecure)
    # The token is handed out once: make sure it can be written before asking.
    fd, tmp = _open_token_file(token_file)
    try:
        return _login(base, group, fd, tmp, token_file, timeout)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _login(base, group, fd, tmp, token_file, timeout):
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    code, resp = sign_console._json_call(base + "/store/cli/request", {
        "group": group, "host": socket.gethostname().split(".")[0], "user": user})
    rid, poll = resp.get("request_id"), resp.get("poll")
    if code != 200 or not (isinstance(rid, str) and rid and isinstance(poll, str) and poll):
        raise SystemExit("store token request to %s failed (HTTP %s): %s"
                         % (base, code or "unreachable", resp.get("detail", "")))
    print("Store token for group %s" % group)
    print("Code to compare on the approve page:  %s" % resp.get("code", "?"))
    sign_console._print_qr(base + "/approve?store=" + urllib.parse.quote(rid))
    print("Waiting for approval (Ctrl-C to cancel) ...", flush=True)
    deadline = time.monotonic() + timeout
    while True:
        code, res = sign_console._json_call(
            base + "/store/cli/%s/result" % urllib.parse.quote(rid), {"poll": poll})
        status = res.get("status") if code == 200 else None
        if status == "approved" and isinstance(res.get("token"), str):
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(res["token"] + "\n")
                os.replace(tmp, token_file)
            except OSError as exc:
                raise SystemExit("cannot write %s: %s" % (token_file, exc))
            exp = res.get("expires")
            until = (datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
                     if isinstance(exp, int) else "?")
            print("Logged in to %s as %s until %s -> %s" % (group, res.get("user"), until, token_file))
            return res.get("user") or ""
        if status == "refused":
            raise SystemExit("the store token was refused (see the approve page)")
        if status == "delivered":
            raise SystemExit("the token was already collected (an answer was lost?): run bits login again")
        if code == 404:
            raise SystemExit("the store token request expired or is unknown")
        if 400 <= code < 500:
            raise SystemExit("store token polling failed (HTTP %s): %s" % (code, res.get("detail", "")))
        # pending / approving / transient errors: keep waiting; an approval being
        # verified gets a short grace past the deadline.
        if time.monotonic() >= deadline + (60 if status == "approving" else 0):
            raise SystemExit("timed out waiting for approval")
        time.sleep(2)


def _open_token_file(path):
    """(fd, name) of a new file beside *path*, mode 0600 (mkstemp), to be
    renamed over *path* once the token is written: never readable by others."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.path.isdir(path):
            raise IsADirectoryError("is a directory")
        return tempfile.mkstemp(prefix=".token-", dir=d)
    except OSError as exc:
        raise SystemExit("cannot write %s: %s" % (path, exc))


def main(argv=None):
    from bits_helpers import orchestrated
    ap = argparse.ArgumentParser(
        prog="bits login",
        description=("Get a bits-store token for a group, approved with a passkey on "
                     "bits-console (community admins), and write it to ~/.bits/token."))
    ap.add_argument("--group", required=True, help="the group (community), e.g. lcg")
    ap.add_argument("--console", default=None,
                    help=("bits-console URL (default: $BITS_CONSOLE_URL, then the site's "
                          "console.backend_url)"))
    ap.add_argument("--token-file", default=TOKEN_FILE, help="where to write it (default: %(default)s)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for approval")
    ap.add_argument("--console-cafile", default=None,
                    help="CA bundle for the console when it is not in this host's trust store")
    ap.add_argument("--console-insecure", action="store_true",
                    help="allow http:// / skip TLS verification (trusted testbed only)")
    a = ap.parse_args(argv)
    console = orchestrated.console_url(a.console)
    if not console:
        ap.error("no bits-console URL: give --console or set $BITS_CONSOLE_URL")
    login(console, a.group, a.token_file, a.timeout, a.console_cafile, a.console_insecure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
