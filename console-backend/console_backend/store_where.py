# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where a build's packages are kept in bits-store, from its CycloneDX SBOM: each
package's tarball, source archives, pip file and git origin, as the folders bits
writes them to (bits-store's README, the bindings), asked of its admin API at once."""

import re
from urllib.parse import urlsplit

_SEG = re.compile(r"[A-Za-z0-9._~+!@=,:-]{1,255}")    # a path segment bits-store takes
_HEX = re.compile(r"[0-9a-f]{64}")
_PIP_FILE = re.compile(r"/files/([^/]+)/([0-9a-f]{64})/[^/]+$")    # a store's pypi file, as pip read it
WHERE_MAX = 2000                                     # places per question (bits-store's)
PLACES_MAX = 20000                                   # per SBOM: ten questions


def _list(x):
    return x if isinstance(x, list) else []


def _split(url):
    try:
        u = urlsplit(url)
        return u, u.port                     # a port that is not a number raises too
    except ValueError:
        return None, None


def _props(x):
    out, props = {}, x.get("properties") if isinstance(x, dict) else None
    for p in _list(props):
        if isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("value"), str):
            out.setdefault(p["name"], p["value"])
    return out


def _folder(*segs):
    """The segments as a folder bits-store takes; None when one is not a segment."""
    if all(isinstance(s, str) and _SEG.fullmatch(s) and s not in (".", "..") for s in segs):
        return "/".join(segs)
    return None


def _sha256(ref):
    for h in _list(ref.get("hashes")):
        if isinstance(h, dict) and h.get("alg") == "SHA-256" and _HEX.fullmatch(str(h.get("content", "")).lower()):
            return str(h["content"]).lower()
    return ""


def _repo(url):
    """host[:port]/path of a git origin, as bits-store's Git page names it; '' for another URL."""
    u, port = _split(url)
    path = u.path.strip("/") if u else ""
    if not path or u.scheme not in ("http", "https") or not u.hostname:
        return ""
    return "%s%s/%s" % (u.hostname.lower(), ":%d" % port if port else "", path[:-4] if path.endswith(".git") else path)


def _project(url):
    """The project a pip file of *url* belongs to, by its name (PEP 503)."""
    f = url.rsplit("/", 1)[-1]
    name = f.split("-", 1)[0] if f.endswith(".whl") else re.sub(r"\.(tar\.gz|tar\.bz2|zip)$", "", f).rsplit("-", 1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def looks(doc, bindings, community, url_checksum):
    """(places, questions) for the SBOM *doc*: places[ref] lists each thing kept,
    {what, url?, repo?, ask: [question indexes]}; questions are bits-store's
    {binding, under}. *bindings* are bits-store's (access view); *url_checksum*
    is bits' getUrlChecksum (a source's folder in a sources binding)."""
    tars = [b for b in bindings if b.get("format") == "tarballs"]
    srcs = [b for b in bindings if b.get("format") == "sources"]
    pypi = [b for b in bindings if b.get("format") == "pypi"]
    meta = (doc.get("metadata") or {}).get("component") if isinstance(doc.get("metadata"), dict) else None
    arch0 = _props(meta).get("bits:architecture", "")
    questions, places = [], {}

    def ask(bs, folder):
        out = []
        for b in bs if folder else []:
            under = folder
            if b.get("gate"):                      # its folders are the communities'
                if not _folder(community):
                    continue
                under = community + "/" + folder
            out.append(len(questions))
            questions.append({"binding": b["prefix"], "under": under})
        return out

    for c in _list(doc.get("components")):
        if not isinstance(c, dict) or not isinstance(c.get("bom-ref"), str):
            continue
        p, kept = _props(c), []
        if p.get("bits:provided_by") == "system":
            places[c["bom-ref"]] = [{"what": "host"}]
            continue
        h = p.get("bits:hash", "")
        if h:
            kept.append({"what": "build", "ask": ask(tars, _folder(p.get("bits:effective_architecture") or arch0,
                                                                     "store", h[:2], h))})
        for r in _list(c.get("externalReferences")):
            if not isinstance(r, dict) or not isinstance(r.get("url"), str):
                continue
            if r.get("type") == "vcs":
                if _repo(r["url"]):
                    kept.append({"what": "git", "url": r["url"], "repo": _repo(r["url"])})
            elif r.get("type") == "distribution":
                u, sha = _split(r["url"])[0], _sha256(r)
                if u is None:
                    continue
                m = _PIP_FILE.search(u.path)
                if m or (u.hostname or "").lower() == "files.pythonhosted.org":
                    name, sha = m.groups() if m else (_project(u.path), sha)
                    kept.append({"what": "pip", "url": r["url"],
                                 "ask": ask(pypi, _folder("files", name, sha) if sha else None)})
                else:
                    md5 = url_checksum(r["url"])
                    kept.append({"what": "source", "url": r["url"], "ask": ask(srcs, _folder(md5[:2], md5))})
        places[c["bom-ref"]] = kept
    return places, questions


def answer(places, questions, found, repos, bindings):
    """places with what bits-store found: each thing's objects (binding, public, key,
    size, reads, read_at, written, origin, by), a git origin's mirror (repo, kept,
    refreshed) or none. *found* answers *questions* in order; *repos* is the Git view's."""
    public = {b["prefix"]: bool(b.get("public")) for b in bindings}
    mirrored = {r.get("repo"): r for r in repos if isinstance(r, dict)}
    out = {}
    for ref, kept in places.items():
        rows = []
        for k in kept:
            row = {key: v for key, v in k.items() if key != "ask"}
            if k["what"] == "git":
                m = mirrored.get(k["repo"])
                row["mirror"] = {f: m.get(f) for f in ("binding", "kept", "refreshed")} if m else None
            elif "ask" in k:
                row["asked"] = bool(k["ask"])
                row["objects"] = [dict(o, binding=b, public=public.get(b, False))
                                  for i in k["ask"] for b in (questions[i]["binding"],) for o in (found[i] or [])]
            rows.append(row)
        out[ref] = rows
    return out
