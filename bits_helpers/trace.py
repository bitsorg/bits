# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits trace FILE: who built, published and approved a file on CVMFS.

From the file: its package, the .meta.json above it (package, version, the build
that compiled it, the recipes' commit, bits). From the console backend's
orchestrator (GET /ops/trace): the pipelines that published and built it, who
requested each and its passkey approval, with the approval's event in the
orchestrator's audit chain. Needs a GitLab token, as bits certify does.
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from bits_helpers import site


def split_cvmfs(path, roots=None):
    """(repo, repo-relative path) of /cvmfs/<repo>/... (or under /cvmfs's real
    path: on macOS a link to /Users/Shared/cvmfs), or None."""
    path = os.path.normpath(path)
    for root in roots or dict.fromkeys(("/cvmfs", os.path.realpath("/cvmfs"))):
        if path.startswith(root.rstrip(os.sep) + os.sep):
            parts = path[len(root.rstrip(os.sep)) + 1:].split(os.sep)
            if parts[0]:
                return parts[0], "/".join(parts[1:])
    return None


def package_meta(path, top=None):
    """(directory, parsed .meta.json) of the package holding *path*, searched
    upwards within its repository (*top*); (None, None) when there is none."""
    where = split_cvmfs(path)
    top = top or (path[:len(path) - len(where[1])].rstrip(os.sep) if where and where[1]
                  else path if where else os.sep)
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d.startswith(top):
        try:
            with open(os.path.join(d, ".meta.json")) as fh:
                return d, json.load(fh)
        except (OSError, ValueError):
            pass
        if d == top:
            break
        d = os.path.dirname(d)
    return None, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token goes to the configured backend only, never where it redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirected to %s: not followed"
                                     % newurl, headers, fp)


def query(backend, token, repo, path, build_id, cafile=None, timeout=30):
    """The backend's answer for *path* (GET /ops/trace); exits with a message on failure."""
    host = urllib.parse.urlsplit(backend).hostname or ""
    if not backend.startswith("https://") and host not in ("localhost", "127.0.0.1", "::1"):
        sys.exit("bits trace: %s: the GitLab token is sent over https only" % backend)
    url = "%s/ops/trace?%s" % (backend.rstrip("/"), urllib.parse.urlencode(
        {"repo": repo, "path": path, "build_id": build_id}))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                               "Accept": "application/json"})
    ctx = ssl.create_default_context(cafile=cafile) if cafile else None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        e.close()
        hint = {401: "the backend did not accept the GitLab token",
                404: "this backend has no /ops/trace (an older bits-console backend?)"}
        sys.exit("bits trace: %s: HTTP %s%s" % (backend, e.code,
                                                ": " + hint[e.code] if e.code in hint else ""))
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit("bits trace: %s: %s" % (backend, getattr(e, "reason", e)))


def _who(p):
    """The requester and approval of one pipeline, as a line."""
    if not p.get("known", True):
        return "pipeline %s (not known to the orchestrator)" % p.get("pipeline")
    if not p.get("visible", True):
        return "pipeline %s (of a community you do not administer)" % p.get("pipeline")
    head = "pipeline %s  %s %s %s  %s" % (p["pipeline"], p.get("community", ""),
                                          p.get("package", ""), p.get("version", ""),
                                          p.get("web_url", ""))
    a = p.get("approval")
    lines = [head, "requested by %s%s" % (p.get("requested_by") or "?", " (%s)"
                                          % p["requested_by_from"] if p.get("requested_by_from") else "")]
    if a:
        ev = a.get("event") or {}
        lines.append("approved by %s at %s (passkey, %s)%s%s" % (
            a["approver"], a["approved_at"], a.get("via", ""),
            "; audit event %s %s" % (ev["seq"], ev["hash"][:16]) if ev else "",
            "; REVOKED at %s" % a["revoked_at"] if a.get("revoked_at") else ""))
    else:
        lines.append("no passkey approval recorded")
    return "\n           ".join(lines)


def report(target, pkgdir, meta, info):
    """The human-readable answer."""
    out = ["File       %s" % target]
    if meta:
        pkg = meta.get("package") or {}
        out += ["Package    %s %s-%s  (%s)" % (pkg.get("name", "?"), pkg.get("version", "?"),
                                              pkg.get("revision", "?"),
                                              meta.get("effective_architecture") or meta.get("architecture", "")),
                "           %s" % pkgdir,
                "           hash %s; build %s" % (pkg.get("hash", "?"), meta.get("build_id", "?")),
                "           recipes %s; bits %s" % (((meta.get("dist") or {}).get("commit") or "?")[:12],
                                                     meta.get("bits_version", "?"))]
    else:
        out.append("Package    none found: no .meta.json above this path")
    if info is None:
        return "\n".join(out)
    for p in info.get("built_by_pipelines") or []:
        out.append("Built      " + _who(p))
    if meta and not info.get("built_by_pipelines"):
        out.append("Built      build %s: no pipeline of it in the orchestrator"
                   % meta.get("build_id", "?"))
    for p in info.get("publishes") or []:
        out.append("Published  %s  %s%s\n           %s" % (
            p.get("published_at", "(time not shown)"), info.get("published_path", ""),
            "  (replaced)" if p.get("replaced") else "", _who(p)))
    if not info.get("publishes"):
        out.append("Published  not in the orchestrator's publish index"
                   + ("" if info.get("indexed") else " (it has none: BITS_ORCH_PREPUB_URLS)"))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bits trace", description="Who built, published and approved a file on CVMFS: "
        "its package (.meta.json) and, from the console backend, the pipelines and approvals.")
    ap.add_argument("file", help="a path under /cvmfs/<repo>/ (need not be mounted)")
    ap.add_argument("--backend", default=None,
                    help="console backend URL (default: $BITS_BACKEND_URL, else the site "
                         "configuration's console.backend_url)")
    ap.add_argument("--cafile", default=None, help="CA bundle for the backend's certificate")
    ap.add_argument("--json", action="store_true", help="print the answer as JSON")
    a = ap.parse_args(argv)
    target = os.path.realpath(a.file) if os.path.lexists(a.file) else os.path.abspath(a.file)
    where = split_cvmfs(target)
    if not where:
        sys.exit("bits trace: %s is not under /cvmfs/<repo>/" % target)
    pkgdir, meta = package_meta(target)
    backend = a.backend or os.environ.get("BITS_BACKEND_URL") or site.get("console.backend_url")
    info, note = None, ""
    if not backend:
        note = "no backend: give --backend or set console.backend_url in a site configuration file"
    else:
        from bits_helpers.forge import resolve_gitlab_token
        token = resolve_gitlab_token(None)
        if not token:
            note = "no GitLab token (~/.bits/gitlab-token or $BITS_GITLAB_TOKEN): package only"
        else:
            info = query(backend, token, where[0], where[1], (meta or {}).get("build_id", ""),
                         cafile=a.cafile)
    if a.json:
        print(json.dumps({"file": target, "package_dir": pkgdir, "meta": meta, "trace": info,
                          **({"note": note} if note else {})}, indent=1))
    else:
        print(report(target, pkgdir, meta, info))
        if note:
            print("Note       " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
