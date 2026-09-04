# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign a manifest via the bits-console (cross-device, human-approved).

Submits the manifest to the console backend, shows a QR/URL to approve on a
phone with a passkey, polls for the signature, and writes the .sig envelope.
The CLI is a thin requester — all authorization and signing happen server-side.
"""

import argparse
import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from bits_helpers import trust

_TIMEOUT = 30
# TLS context for backend calls. None = the system default (verifies). Set by
# sign_via_console when --cafile / --insecure is given (e.g. the CERN CA isn't in
# this host's Python trust store).
_CTX = None


def _urlopen(req_or_url):
    """Open a request and parse JSON, turning a backend 4xx/5xx into a clean
    message (the FastAPI ``detail``) instead of a urllib traceback."""
    try:
        with urllib.request.urlopen(req_or_url, timeout=_TIMEOUT, context=_CTX) as fh:
            return json.load(fh)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.load(e) or {}).get("detail", "")
        except Exception:
            pass
        raise SystemExit("signing backend error (HTTP %s): %s" % (e.code, detail or e.reason))


def _post(url, data, ctype="application/octet-stream"):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype})
    return _urlopen(req)


def _get(url):
    return _urlopen(url)


def _print_qr(url):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        pass   # qrcode optional; the URL below is always printed
    print("\nApprove on your phone: %s\n" % url)


def sign_via_console(backend, manifest_path, sig_path=None, timeout=300,
                     cafile=None, insecure=False):
    """Submit *manifest_path* to *backend*, wait for a human passkey approval, and
    write the signature envelope to *sig_path* (default: <manifest>.sig). The approve
    URL/QR is built from *backend* + /approve — the backend serves the approve page
    on its own origin, so the approver needs no login. *cafile*/*insecure* control
    TLS verification when the backend's CA is not in this host's default trust store."""
    global _CTX
    if insecure:
        _CTX = ssl._create_unverified_context()
        print("WARNING: TLS verification disabled (--insecure).", file=sys.stderr)
    elif cafile:
        _CTX = ssl.create_default_context(cafile=cafile)
    base = backend.rstrip("/")
    with open(manifest_path, "rb") as fh:
        body = fh.read()
    local_digest = hashlib.sha256(body).hexdigest()
    resp = _post(base + "/sign/cli/request", body)
    # Cross-check the digest the backend reports against our own bytes.
    if resp.get("digest") != local_digest:
        raise SystemExit("backend digest %s != local %s" % (resp.get("digest"), local_digest))
    print("digest %s   groups: %s"
          % (local_digest, ", ".join(resp.get("groups", []))))
    # Build the approve URL ourselves from the backend we connected to; the approve
    # page is served there at /approve. Nothing origin-related comes from the server.
    approve_url = base + "/approve?approve=" + urllib.parse.quote(resp["request_id"])
    _print_qr(approve_url)
    print("Waiting for approval (Ctrl-C to cancel) ...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _get(base + "/sign/cli/%s/result" % resp["request_id"])
        status = res.get("status")
        if status == "signed":
            env = res["envelope"]
            # Do NOT trust the backend blindly: verify the envelope over OUR bytes
            # against the shipped trust anchor before writing it.
            kid = trust.verify_bytes(body, env, trust.load_trusted_keys())
            if not kid:
                raise SystemExit("signature does not verify against the trust anchor")
            out = sig_path or (manifest_path + ".sig")
            with open(out, "w") as fh:
                json.dump(env, fh)
            print("signed by %s (key %s) -> %s" % (res.get("signed_by"), kid, out))
            return out
        if status == "denied":
            raise SystemExit("approval was denied")
        time.sleep(2)
    raise SystemExit("timed out waiting for approval")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Sign a manifest via bits-console (human passkey approval).")
    ap.add_argument("manifest", help="the common-manifest JSON to sign")
    ap.add_argument("--console", required=True,
                    help="signing backend URL (e.g. https://bits.cern.ch); the "
                         "approve page is served there at /approve")
    ap.add_argument("-o", "--sig", default=None,
                    help="output .sig path (default: <manifest>.sig)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait")
    ap.add_argument("--cafile", default=None,
                    help="CA bundle to verify the backend cert (e.g. the CERN CA "
                         "chain) when it is not in this host's default trust store")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (testing only, on a trusted network)")
    a = ap.parse_args(argv)
    sign_via_console(a.console, a.manifest, a.sig, a.timeout, a.cafile, a.insecure)


if __name__ == "__main__":
    main()
