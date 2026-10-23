# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""SBOM export of a bits build manifest: CycloneDX 1.6 and SPDX 2.3 (JSON).

The build manifest already records what an SBOM needs (see manifest.py): each
package's version, build hash, tarball sha256, sources with checksums, git
origin, patches, SPDX licence and, from schema v4, its direct dependencies and
the packages taken from the system. This module only maps those fields onto
the two standard formats.

Output is deterministic for a given manifest and bits version: ids, serial
number and timestamps come from the manifest, never from the clock.

* A dependency on a package taken from the system (``system_packages``) is a
  component marked ``bits:provided_by = system``; any other dependency absent
  from the manifest (a failed build, a recipe repository) is left out.
* A recipe ``license:`` that is not a valid SPDX expression is kept verbatim:
  as a licence *name* in CycloneDX and as a declared ``LicenseRef-bits-…``
  (with the original text) in SPDX, so both documents stay valid.
* URLs lose credentials (``user:pw@``, token query parameters); a source that
  is not a URL (a local development checkout) is not exported.
* A v3 manifest has no dependency edges: the SBOM then lists components only.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

try:
    from bits_helpers import __version__
except ImportError:
    __version__ = None

from bits_helpers.spdx_ids import EXCEPTIONS, LICENSES
from bits_helpers.sync import redistributable_forms
from bits_helpers import site

CDX_SPEC = "1.6"
SPDX_VERSION = "SPDX-2.3"
NAMESPACE = str(site.get("sbom.namespace") or "https://spdx.org/spdxdocs/bits")   # SPDX documentNamespace base

_ALGS = {"md5": ("MD5", "MD5", 32), "sha1": ("SHA-1", "SHA1", 40),
         "sha256": ("SHA-256", "SHA256", 64), "sha384": ("SHA-384", "SHA384", 96),
         "sha512": ("SHA-512", "SHA512", 128)}          # name: (CycloneDX, SPDX, hex length)
_GITHUB = re.compile(r"(?:://|@)(?:www\.)?github\.com[/:]([^/]+)/([^/?#]+?)(?:\.git)?(?:[/?#]|$)")
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s]")
_SECRET_KEY = re.compile(r"token|secret|passw|signature|^sig$|key$|auth|credential", re.I)
# SPDX 2.3 downloadLocation grammar (the specification's own pattern).
_SPDX_URL = (r"(http://www\.|https://www\.|http://|https://|ssh://|git://|svn://|sftp://|ftp://)?"
             r"([\w\-.!~*'()%;:&=+$,]+@)?[a-z0-9]+([\-.][a-z0-9]+){0,100}\.[a-z]{2,5}"
             r"(:[0-9]{1,5})?(/.*)?")
_SPDX_DOWNLOAD = re.compile(r"^((git|hg|svn|bzr)\+)?%s$" % _SPDX_URL, re.I)
_NO_LICENCE = ("NOASSERTION", "NONE")                 # SPDX's own special values
_LIC_BY_LOWER = {i.lower(): i for i in LICENSES}
_EXC_BY_LOWER = {i.lower(): i for i in EXCEPTIONS}


# ── Field helpers ────────────────────────────────────────────────────────────

def clean_url(url):
    """*url* without credentials, or "" when it is not a URL at all."""
    url = str(url or "").strip()
    if not _URL.match(url):
        return ""
    url = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#]*@", r"\1", url)   # up to the last @
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in q if not _SECRET_KEY.search(k)]
    if len(kept) != len(q):   # rebuilt only when a secret was removed
        url = urlunsplit(parts._replace(query=urlencode(kept)))
    return url


def _checksum(value):
    """(alg, hex) from "sha256:<hex>" with a digest of the right length, else None."""
    alg, _, hexd = str(value or "").partition(":")
    alg, hexd = alg.lower().replace("-", ""), hexd.lower()
    if alg in _ALGS and re.fullmatch("[0-9a-f]{%d}" % _ALGS[alg][2], hexd):
        return alg, hexd
    return None


def spdx_expression(text):
    """The canonical SPDX licence expression for *text*, or None if it is not
    one (unknown ids, free text, local LicenseRef with bad characters, or a
    DocumentRef, which would need an external document)."""
    tokens = re.findall(r"\(|\)|[^\s()]+", str(text or ""))
    pos, out = 0, []

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def simple():
        nonlocal pos
        tok = peek()
        if tok is None:
            return False
        pos += 1
        plus = tok.endswith("+") and len(tok) > 1
        base = tok[:-1] if plus else tok
        if re.fullmatch(r"LicenseRef-[A-Za-z0-9.-]+", tok):
            out.append(tok)
        elif base.lower() in _LIC_BY_LOWER:
            out.append(_LIC_BY_LOWER[base.lower()] + ("+" if plus else ""))
        else:
            return False
        if (peek() or "").upper() == "WITH":
            pos += 1
            exc = peek()
            if exc is None or exc.lower() not in _EXC_BY_LOWER:
                return False
            pos += 1
            out.extend(["WITH", _EXC_BY_LOWER[exc.lower()]])
        return True

    def term():
        nonlocal pos
        if peek() == "(":
            pos += 1
            out.append("(")
            if not expr() or peek() != ")":
                return False
            pos += 1
            out.append(")")
            return True
        return simple()

    def expr():
        nonlocal pos
        if not term():
            return False
        while (peek() or "").upper() in ("AND", "OR"):
            out.append(peek().upper())
            pos += 1
            if not term():
                return False
        return True

    if not tokens or not expr() or pos != len(tokens):
        return None
    return " ".join(out).replace("( ", "(").replace(" )", ")")


def _full_version(e):
    ver, rev = str(e.get("version") or ""), str(e.get("revision") or "")
    return ver + ("-" + rev if rev else "")


def _commit(e):
    c = str(e.get("commit_hash") or "")
    return "" if c in ("", "0") else c


def _source_urls(e):
    """(url, checksum) of the source archives, credentials removed."""
    out = []
    for s in e.get("source_checksums") or []:
        if isinstance(s, dict) and clean_url(s.get("url")):
            out.append((clean_url(s.get("url")), _checksum(s.get("checksum"))))
    return out


def _references(e, prefix):
    """The recipe's references (manifest ``references``) that start with *prefix*."""
    return [r for r in e.get("references") or [] if isinstance(r, str) and r.startswith(prefix)]


def _purl(e):
    """A package URL: the recipe's own (e.g. pkg:pypi), else pkg:github for
    GitHub-hosted code, else pkg:generic."""
    own = _references(e, "pkg:")
    if own:
        return own[0]
    name, ver = str(e["package"]), str(e.get("version") or "")
    for u in [clean_url(e.get("source"))] + [u for u, _ in _source_urls(e)]:
        m = _GITHUB.search(u or "")
        if m:
            commit = _commit(e)
            ref = commit if re.fullmatch(r"[0-9a-f]{40}", commit) else (e.get("tag") or ver)
            return "pkg:github/%s/%s@%s" % (m.group(1).lower(), m.group(2).lower(),
                                           quote(str(ref), safe=".-_~"))
    return "pkg:generic/%s@%s" % (quote(name, safe=".-_~"), quote(ver, safe=".-_~"))


def _download(e):
    """Where the code came from: the first source archive, or the git origin."""
    urls = _source_urls(e)
    if urls:
        return urls[0][0]
    src = clean_url(e.get("source"))
    if src:
        ref = _commit(e) or str(e.get("tag") or "")
        return "git+%s%s" % (src, "@" + ref if ref else "")
    return ""


# ── Model shared by both formats ─────────────────────────────────────────────

def _skip(name):
    return str(name).startswith("defaults-")


def components(manifest):
    """The release as component dicts — built packages in manifest order, then
    the system-provided ones — each with a unique ``ref`` and resolved
    ``deps`` (runtime) / ``build_deps`` refs."""
    built, by_name, refs = [], {}, set()

    def unique(ref):
        base, n = ref, 1
        while ref in refs:
            n += 1
            ref = "%s~%d" % (base, n)
        refs.add(ref)
        return ref

    for e in manifest.get("packages") or []:
        if not isinstance(e, dict) or not e.get("package") or _skip(e["package"]) \
                or e.get("provides_repository"):
            continue
        name, key = str(e["package"]), e.get("hash") or _full_version(e)
        if any((c["name"], c["key"]) == (name, key) for c in built):
            continue   # listed twice (e.g. several build passes)
        ident = e.get("hash", "")[:16] if e.get("hash") else _full_version(e) or "unversioned"
        c = {"name": name, "key": key, "entry": e, "system": False,
             "ref": unique("%s@%s" % (name, ident))}
        built.append(c)
        by_name.setdefault(name, []).append(c)
    system_names = set(manifest.get("system_packages") or [])
    system = {}
    for c in built:
        for field in ("requires", "build_requires"):
            for d in c["entry"].get(field) or []:
                if d not in by_name and d in system_names and d not in system:
                    system[d] = {"name": d, "key": "", "entry": {"package": d}, "system": True,
                                 "ref": unique("%s@system" % d), "deps": [], "build_deps": []}
    for d in sorted(system):
        by_name[d] = [system[d]]
    for c in built:
        for field, out in (("requires", "deps"), ("build_requires", "build_deps")):
            seen = []
            for d in c["entry"].get(field) or []:
                seen += [x["ref"] for x in by_name.get(d, []) if x["ref"] not in seen]
            c[out] = seen
    return built + [system[d] for d in sorted(system)]


def _roots(manifest, comps):
    wanted = set(manifest.get("requested_packages") or [])
    roots = [c["ref"] for c in comps if c["name"] in wanted and not c["system"]]
    return roots or [c["ref"] for c in comps if not c["system"]][-1:]


def _digest(manifest):
    """Stable digest of the whole manifest."""
    body = json.dumps(manifest, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _when(manifest):
    """The manifest's own time as UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
    for key in ("updated_at", "created_at", "published_at"):
        t = str(manifest.get(key) or "")
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        d = d.replace(tzinfo=d.tzinfo or timezone.utc).astimezone(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    return "1970-01-01T00:00:00Z"


def _tool_version():
    return __version__ or "unknown"


def _shipped(e):
    return "binaries" in redistributable_forms(e.get("redistributable"))


# ── CycloneDX 1.6 ────────────────────────────────────────────────────────────

def _props(c):
    e = c["entry"]
    if c["system"]:
        return [{"name": "bits:provided_by", "value": "system"}]
    out = [("bits:hash", e.get("hash")), ("bits:revision", e.get("revision")),
           ("bits:effective_architecture", e.get("effective_architecture")),
           ("bits:pkg_family", e.get("pkg_family")), ("bits:outcome", e.get("outcome")),
           ("bits:redistributable", e.get("redistributable")), ("bits:commit", _commit(e))]
    out += [("bits:cpe", x) for x in _references(e, "cpe:2.3:")[1:]]
    out += [("bits:build_requires", d) for d in e.get("build_requires") or []]
    out += [("bits:patch", ("%s %s" % (p.get("name"), p.get("checksum") or "")).strip())
            for p in e.get("patches") or [] if isinstance(p, dict)]
    return [{"name": k, "value": str(v)} for k, v in out if v not in (None, "")]


def to_cyclonedx(manifest, build_id="unknown"):
    """CycloneDX 1.6 BOM (a dict) for *manifest*."""
    comps = components(manifest)
    out = []
    for c in comps:
        e = c["entry"]
        comp = {"type": "library", "bom-ref": c["ref"], "name": c["name"]}
        if not c["system"]:
            # The upstream version, as scanners compare it; the bits revision is
            # a property (bits:revision).
            comp["version"] = str(e.get("version") or "")
            comp["purl"] = _purl(e)
            cpes = _references(e, "cpe:2.3:")
            if cpes:
                comp["cpe"] = cpes[0]
            sha = _checksum(e.get("tarball_sha256"))
            if sha:
                comp["hashes"] = [{"alg": _ALGS[sha[0]][0], "content": sha[1]}]
            lic = str(e.get("license") or "").strip()
            if lic and lic.upper() not in _NO_LICENCE:
                expr = spdx_expression(lic)
                comp["licenses"] = ([{"expression": expr}] if expr else
                                    [{"license": {"name": lic}}])
            refs = []
            for url, sha in _source_urls(e):
                ref = {"type": "distribution", "url": url}
                if sha:
                    ref["hashes"] = [{"alg": _ALGS[sha[0]][0], "content": sha[1]}]
                refs.append(ref)
            if clean_url(e.get("source")):
                refs.append({"type": "vcs", "url": clean_url(e.get("source"))})
            if refs:
                comp["externalReferences"] = refs
        comp["properties"] = _props(c)
        out.append(comp)
    release = {"type": "application", "bom-ref": "release", "name": build_id,
               "properties": [{"name": k, "value": str(v)} for k, v in (
                   ("bits:architecture", manifest.get("architecture")),
                   ("bits:defaults", "::".join(manifest.get("defaults") or [])),
                   ("bits:config_commit", manifest.get("config_commit")),
                   ("bits:dist_hash", manifest.get("bits_dist_hash"))) if v]}
    deps = [{"ref": "release", "dependsOn": _roots(manifest, comps)}]
    deps += [{"ref": c["ref"], "dependsOn": c["deps"]} for c in comps]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CDX_SPEC,
        "serialNumber": "urn:uuid:%s" % uuid.uuid5(
            uuid.NAMESPACE_URL, "%s/%s/%s" % (NAMESPACE, build_id, _digest(manifest))),
        "version": 1,
        "metadata": {
            "timestamp": _when(manifest),
            "tools": {"components": [{"type": "application", "name": "bits",
                                      "version": _tool_version()}]},
            "component": release,
        },
        "components": out,
        "dependencies": deps,
    }


# ── SPDX 2.3 ─────────────────────────────────────────────────────────────────

def to_spdx(manifest, build_id="unknown"):
    """SPDX 2.3 document (a dict) for *manifest*."""
    comps = components(manifest)
    ids, used = {}, set()
    for c in comps:   # SPDXRef-[A-Za-z0-9.-]+, unique even where sanitising collides
        base = "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]+", "-", c["ref"])
        sid, n = base, 1
        while sid in used:
            n += 1
            sid = "%s-%d" % (base, n)
        used.add(sid)
        ids[c["ref"]] = sid
    extracted = {}   # LicenseRef id -> original text (None: the recipe's own ref)
    reserved = {r for c in comps
                for r in re.findall(r"LicenseRef-[A-Za-z0-9.-]+",
                                    spdx_expression(c["entry"].get("license")) or "")}

    def declared(text):
        expr = spdx_expression(text)
        if expr:
            for r in re.findall(r"LicenseRef-[A-Za-z0-9.-]+", expr):
                extracted.setdefault(r, None)
            return expr
        # Not SPDX: keep the recipe's words as a declared LicenseRef-bits-*
        # (a prefix recipes do not use, so it never collides with theirs).
        slug = re.sub(r"[^A-Za-z0-9.-]+", "-", re.sub(r"^LicenseRef-", "", text)).strip("-.")
        ref = "LicenseRef-bits-" + (slug[:64] or "unknown")
        if ref in reserved or extracted.get(ref, text) != text:   # taken
            ref = "%s-%s" % (ref, hashlib.sha1(text.encode()).hexdigest()[:8])
        extracted[ref] = text
        return ref

    packages = []
    for c in comps:
        e = c["entry"]
        pkg = {"SPDXID": ids[c["ref"]], "name": c["name"],
               "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
               "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
               "copyrightText": "NOASSERTION"}
        if c["system"]:
            pkg["comment"] = "Provided by the host system; not built by bits."
        else:
            pkg["versionInfo"] = str(e.get("version") or "")
            loc = _download(e)
            pkg["downloadLocation"] = loc if _SPDX_DOWNLOAD.match(loc) else "NOASSERTION"
            lic = str(e.get("license") or "").strip()
            if lic.upper() in _NO_LICENCE:
                pkg["licenseDeclared"] = lic.upper()
            elif lic:
                pkg["licenseDeclared"] = declared(lic)
            sha = _checksum(e.get("tarball_sha256"))
            if sha:
                pkg["checksums"] = [{"algorithm": _ALGS[sha[0]][1], "checksumValue": sha[1]}]
            pkg["externalRefs"] = [{"referenceCategory": "PACKAGE-MANAGER",
                                    "referenceType": "purl", "referenceLocator": _purl(e)}]
            pkg["externalRefs"] += [{"referenceCategory": "SECURITY", "referenceType": "cpe23Type",
                                     "referenceLocator": x} for x in _references(e, "cpe:2.3:")]
            info = ["bits build hash %s" % e["hash"]] if e.get("hash") else []
            if _commit(e):
                info.append("commit %s" % _commit(e))
            info += [("patch %s %s" % (p.get("name"), p.get("checksum") or "")).strip()
                     for p in e.get("patches") or [] if isinstance(p, dict)]
            if info:
                pkg["sourceInfo"] = "; ".join(info)
            if not _shipped(e):
                pkg["comment"] = ("Binaries not redistributed (redistributable: %s)."
                                  % e.get("redistributable"))
        packages.append(pkg)
    rels = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
             "relatedSpdxElement": ids[r]} for r in _roots(manifest, comps)]
    for c in comps:
        rels += [{"spdxElementId": ids[c["ref"]], "relationshipType": "DEPENDS_ON",
                  "relatedSpdxElement": ids[d]} for d in c["deps"]]
        rels += [{"spdxElementId": ids[d], "relationshipType": "BUILD_DEPENDENCY_OF",
                  "relatedSpdxElement": ids[c["ref"]]} for d in c["build_deps"]]
    doc = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": build_id,
        "documentNamespace": "%s/%s-%s" % (NAMESPACE, quote(build_id, safe=".-_~"),
                                           _digest(manifest)[:16]),
        "creationInfo": {"created": _when(manifest),
                         "creators": ["Tool: bits-%s" % _tool_version()]},
        "packages": packages,
        "relationships": rels,
    }
    if extracted:   # every LicenseRef used must be declared in the document
        doc["hasExtractedLicensingInfos"] = [
            {"licenseId": r, "name": (t or r[len("LicenseRef-"):]),
             "extractedText": (t if t else "See the package's licence files and the "
                                           "release NOTICE.")}
            for r, t in sorted(extracted.items())]
    return doc


# ── Files and CLI ────────────────────────────────────────────────────────────

FILES = {"cyclonedx": "sbom.cdx.json", "spdx": "sbom.spdx.json"}


def render(manifest, fmt, build_id="unknown"):
    """The SBOM as JSON text (sorted keys, so byte-stable). Raises ValueError
    for a manifest without packages."""
    if not components(manifest):
        raise ValueError("the manifest lists no packages")
    doc = to_cyclonedx(manifest, build_id) if fmt == "cyclonedx" else to_spdx(manifest, build_id)
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def has_dependency_graph(manifest):
    return int(manifest.get("schema_version") or 0) >= 4


def doSbom(args, parser):  # noqa: N802
    """`bits sbom MANIFEST [--format …] [-o DIR]`: write the SBOM file(s)."""
    import os
    import sys
    from bits_helpers.log import info, warning
    from bits_helpers.provenance import build_id_from_manifest
    try:
        with open(args.manifest) as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        parser.error("cannot read manifest %s: %s" % (args.manifest, exc))
    if not has_dependency_graph(manifest):
        warning("%s is a schema v%s manifest: no dependency edges, the SBOM lists "
                "components only (rebuild with this bits for the graph)",
                args.manifest, manifest.get("schema_version", "?"))
    # A published BOM names its release; a build manifest's id is recomputed.
    build_id = (args.buildId or manifest.get("build_id")
                or build_id_from_manifest(manifest) or "unknown")
    fmts = list(FILES) if args.format == "both" else [args.format]
    if args.outDir == "-" and len(fmts) != 1:
        parser.error("-o - (stdout) needs a single --format")
    try:
        texts = [(fmt, render(manifest, fmt, build_id)) for fmt in fmts]
    except ValueError as exc:
        parser.error("%s: %s" % (args.manifest, exc))
    if args.outDir == "-":
        sys.stdout.write(texts[0][1])
        return 0
    os.makedirs(args.outDir, exist_ok=True)
    for fmt, text in texts:
        path = os.path.join(args.outDir, FILES[fmt])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        info("SBOM (%s) -> %s", fmt, path)
    return 0
