# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits compliance — audit recipe licence metadata and the binary store.

Summarises the licence-compliance state in one command:

  * RECIPES — scan a recipe directory's YAML front-matter: how many carry a
    ``license:``, which are missing it, which still use unverified custom ids
    (``LicenseRef-*``), and which declare ``redistributable: false`` (the CVMFS
    exclusion list — see compliance.md in the workspace / docs).

  * STORE — probe whether the S3 store is anonymously readable (a "no
    redistribution" licence covers BOTH source and binaries, so a restricted
    object in a world-readable bucket is public redistribution regardless of
    any CVMFS gate), then walk the per-build BOMs under ``MANIFESTS/`` and the
    signed ``common-manifest-*`` files and report every stored / certified
    package whose CURRENT recipe flags it ``redistributable: false``.

Exit status: 0 = no issues, 1 = issues found (missing licences or restricted
packages exposed), so the command can gate CI. Read-only: audits, never fixes.
"""

import json
import os
import re
import urllib.request

from bits_helpers.log import banner, debug, error, info, warning

# Front-matter keys read from each recipe. Values may be quoted (the header
# auto-quoting writes them back quoted); strip one level of quotes.
_KEY_RE = re.compile(
    r"^(package|license|redistributable|provides_repository):[ \t]*(.*?)[ \t]*$",
    re.M)


def _recipe_meta(path):
    """Return {package, license, redistributable} parsed from a recipe header.

    Only the YAML front-matter (up to the first column-0 ``---``) is scanned,
    so a ``license:`` in the shell body can never shadow the metadata. Regex
    (not YAML) on purpose: the audit must not crash on the one recipe with an
    exotic header, and these three keys are single-line scalars by convention.
    """
    header = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.rstrip("\n") == "---":
                break
            header.append(line)
    meta = {}
    for key, val in _KEY_RE.findall("".join(header)):
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        meta.setdefault(key, val)
    return meta


def scan_recipes(recipes_dir):
    """Scan *recipes_dir* for ``*.sh`` recipes; return the audit dict.

    ``restricted`` lists packages whose BINARIES may not be redistributed
    (``redistributable: sources|none`` — the store/CVMFS exclusion list);
    ``restricted_sources`` those whose SOURCE archives may not
    (``binaries|none``). One enum key covers both forms.
    """
    from bits_helpers.sync import redistributable_forms
    out = {"total": 0, "missing_license": [], "licenseref": [],
           "noassertion": [], "restricted": [], "restricted_sources": [],
           "by_package": {}}
    for name in sorted(os.listdir(recipes_dir)):
        if not name.endswith(".sh"):
            continue
        meta = _recipe_meta(os.path.join(recipes_dir, name))
        pkg = meta.get("package")
        if not pkg:
            continue                      # not a recipe (helper script)
        out["total"] += 1
        lic = meta.get("license", "")
        out["by_package"][pkg.lower()] = {"license": lic, "recipe": name}
        # defaults-* are config pseudo-packages and repository providers are
        # meta-recipes pointing at a recipe repo: never published, licence-free.
        if not lic and not pkg.startswith("defaults") \
           and not meta.get("provides_repository"):
            out["missing_license"].append(name)
        if lic.startswith("LicenseRef-"):
            out["licenseref"].append("%s (%s)" % (name, lic))
        if lic == "NOASSERTION":
            out["noassertion"].append(name)
        if "redistributable" in meta:
            forms = redistributable_forms(meta["redistributable"])
            if "binaries" not in forms:
                out["restricted"].append(pkg)
                out["by_package"][pkg.lower()]["restricted"] = True
            if "sources" not in forms:
                out["restricted_sources"].append(pkg)
    return out


def resolve_group_specs(args, parser):
    """Resolve the dependency closure of ``args.packages`` for a group.

    Follows the exact repository-discovery path of ``bits build`` (and
    ``bits deps``): the primary config dir, the defaults profile, always-on
    providers (bits-providers) and repository providers discovered iteratively
    along the dependency walk. Returns ``(specs, system_pkgs, config_paths)``
    where *specs* maps every package in the closure to its resolved spec —
    conditional requires (``pkg:(?var)`` / arch gates) already evaluated for
    the selected --defaults/--architecture.
    """
    from bits_helpers.cmd import getstatusoutput
    from bits_helpers.repo_provider import (
        fetch_repo_providers_iteratively, load_always_on_providers)
    from bits_helpers.defaults import parseDefaults, readDefaults, validateDefaults
    from bits_helpers.packages import getPackageList
    from bits_helpers.paths import getConfigPaths
    from bits_helpers.matchers import resolve_variables

    config_dir = os.path.abspath(args.configDir)

    def defaultsReader():
        meta, body = readDefaults(config_dir, args.defaults, parser.error,
                                  args.architecture)
        meta["variables"] = resolve_variables(meta.get("variables"), {},
                                              args.architecture, args.defaults)
        return meta, body

    err, overrides, taps, defaultsMeta = parseDefaults(args.disable,
                                                       defaultsReader, debug)
    if err:
        parser.error(err)

    work_dir = getattr(args, "workDir", None) or os.environ.get(
        "BITS_WORK_DIR", "sw")
    prov = dict(
        config_dir        = config_dir,
        work_dir          = work_dir,
        reference_sources = getattr(args, "referenceSources", None)
                            or os.path.join(work_dir, "MIRROR"),
        fetch_repos       = getattr(args, "fetchRepos", True),
        taps              = taps,
        provider_policy   = getattr(args, "provider_policy", {}) or {},
    )
    always_on = load_always_on_providers(
        bits_providers=getattr(args, "bits_providers", None), **prov)
    provider_dirs = fetch_repo_providers_iteratively(
        packages     = list(args.packages)
                       + list(defaultsMeta.get("requires", []))
                       + list(defaultsMeta.get("build_requires", [])),
        overrides    = overrides,
        defaults     = args.defaults,
        default_vars = defaultsMeta.get("variables"),
        **prov)
    provider_dirs.update(always_on)

    def performCheck(pkg, cmd):
        return getstatusoutput(cmd)

    specs = {}
    system_pkgs, _own, failed, _valid = getPackageList(
        packages                = list(args.packages),
        specs                   = specs,
        configDir               = config_dir,
        preferSystem            = False,
        noSystem                = None,
        architecture            = args.architecture,
        disable                 = args.disable,
        defaults                = args.defaults,
        performPreferCheck      = performCheck,
        performRequirementCheck = performCheck,
        performValidateDefaults = lambda spec: validateDefaults(spec, args.defaults),
        overrides               = overrides,
        taps                    = taps,
        log                     = debug,
        provider_dirs           = provider_dirs,
        defaults_meta           = defaultsMeta)
    if failed:
        warning("compliance: could not resolve: %s", ", ".join(sorted(failed)))
    return specs, system_pkgs, getConfigPaths(config_dir)


def scan_specs(specs):
    """Audit a resolved dependency closure — same classification as
    ``scan_recipes``, but over discovery-resolved specs, so the result covers
    exactly the packages of the selected group (conditionals evaluated,
    first-repo-wins already applied)."""
    from bits_helpers.sync import redistributable_forms
    out = {"total": 0, "missing_license": [], "licenseref": [],
           "noassertion": [], "restricted": [], "restricted_sources": [],
           "by_package": {}}
    for pkg in sorted(specs):
        spec = specs[pkg]
        name = "%s.sh" % pkg.lower()
        out["total"] += 1
        lic = str(spec.get("license") or "")
        out["by_package"][pkg.lower()] = {"license": lic, "recipe": name}
        # defaults-* are config pseudo-packages; repository providers are
        # meta-recipes pointing at a recipe repo — neither is published.
        if not lic and not pkg.startswith("defaults") \
           and not spec.get("provides_repository"):
            out["missing_license"].append(name)
        if lic.startswith("LicenseRef-"):
            out["licenseref"].append("%s (%s)" % (name, lic))
        if lic == "NOASSERTION":
            out["noassertion"].append(name)
        if "redistributable" in spec:
            forms = redistributable_forms(spec.get("redistributable"))
            if "binaries" not in forms:
                out["restricted"].append(pkg)
                out["by_package"][pkg.lower()]["restricted"] = True
            if "sources" not in forms:
                out["restricted_sources"].append(pkg)
    return out


def _anonymous_store_access(endpoint, bucket):
    """True when the bucket answers an UNAUTHENTICATED listing request."""
    url = "%s/%s/?max-keys=1" % (endpoint.rstrip("/"), bucket)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status == 200 and b"ListBucketResult" in resp.read(4096)
    except Exception as exc:              # pylint: disable=broad-except
        debug("compliance: anonymous probe failed (%s) — treating as private", exc)
        return False


def _s3_client(store_url):
    """Return ``(s3, bucket, endpoint)`` for *store_url*.

    Uses the standard credential chain (env / ~/.bits/s3keys) when available;
    with NO credentials it degrades to an UNSIGNED client — an audit must be
    runnable from anywhere, and anonymous access working at all is itself one
    of the findings (it means the bucket is world-readable).
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config
    from bits_helpers.publish import _normalize_s3_store
    from bits_helpers.sync import resolve_and_export_s3_config
    store = _normalize_s3_store(store_url)
    bucket = store.split("://", 1)[-1].strip("/")
    resolve_and_export_s3_config()
    endpoint = os.environ.get("S3_ENDPOINT_URL") or os.environ.get(
        "BITS_S3_ENDPOINT_URL") or "https://s3.cern.ch"
    style = os.environ.get("S3_ADDRESSING_STYLE", "path")
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        cfg = Config(s3={"addressing_style": style})
    else:
        info("no S3 credentials found — auditing anonymously")
        cfg = Config(signature_version=UNSIGNED, s3={"addressing_style": style})
    return boto3.client("s3", endpoint_url=endpoint, config=cfg), bucket, endpoint


def audit_store(store_url, restricted, work_dir):
    """Walk the store's manifests; return the store-side findings dict.

    *restricted* is a set of lower-cased package names whose CURRENT recipe
    says ``redistributable: false`` — the recipes are the source of truth, the
    store is what is audited against them.
    """
    s3, bucket, endpoint = _s3_client(store_url)

    found = {"bucket": bucket, "public": _anonymous_store_access(endpoint, bucket),
             "boms": 0, "signed": 0, "stored": [], "certified": []}

    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix="MANIFESTS/"):
        keys += [o["Key"] for o in page.get("Contents", []) or []]

    def _load(key):
        try:
            return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as exc:          # pylint: disable=broad-except
            warning("compliance: unreadable manifest %s (%s)", key, exc)
            return None

    for key in keys:
        leaf = key.split("/")[-1]
        if not leaf.endswith(".json"):
            continue
        if "/rev-index/" in key:
            continue
        signed = leaf.startswith("common-manifest")
        doc = _load(key)
        if not isinstance(doc, dict):
            continue
        found["signed" if signed else "boms"] += 1
        for e in doc.get("packages") or []:
            pkg = str(e.get("package", ""))
            if pkg.lower() not in restricted:
                continue
            hit = "%s %s-%s %s" % (pkg, e.get("version", "?"),
                                   e.get("revision", "?"),
                                   e.get("effective_architecture", "?"))
            bucket_list = found["certified" if signed else "stored"]
            if hit not in bucket_list:
                bucket_list.append(hit)
    return found


def _recipe_source_prefixes(recipes_dir, rec, restricted):
    """SOURCES/cache/ prefixes for the restricted packages' source archives.

    Resolves each restricted recipe's ``source:``/``sources:`` URLs (best-effort
    ``%(version)s``/``%(tag)s`` substitution — the audit reports what it cannot
    resolve rather than guessing) to the store's checksum-addressed
    ``SOURCES/cache/<h2>/<url_md5>/`` prefix. Returns ``(prefixes, unresolved)``.
    """
    from bits_helpers.download import getUrlChecksum
    url_re = re.compile(r"^(source|version|tag):[ \t]*(.*?)[ \t]*$", re.M)
    prefixes, unresolved = [], []
    for pkg in sorted(restricted):
        name = rec["by_package"].get(pkg, {}).get("recipe")
        if not name:
            continue
        header = []
        with open(os.path.join(recipes_dir, name), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                if line.rstrip("\n") == "---":
                    break
                header.append(line)
        text = "".join(header)
        meta = dict((k, v.strip().strip("\"'")) for k, v in url_re.findall(text))
        urls = []
        m = re.search(r"^sources:\n((?:[ \t]+-[ \t]+.*\n)+)", text, re.M)
        if m:
            urls += [ln.strip().lstrip("-").strip().strip("\"'")
                     for ln in m.group(1).splitlines()]
        if meta.get("source"):
            urls.append(meta["source"])
        from bits_helpers.checksum import parse_entry
        for url in urls:
            url, _cs = parse_entry(url)       # strip a ',algo:hex' checksum suffix
            for key in ("version", "tag"):
                url = url.replace("%%(%s)s" % key,
                                  meta.get(key) or meta.get("version") or "")
            if "%(" in url:
                unresolved.append("%s: %s" % (name, url))
                continue
            if url.startswith(("git://", "git+")) or url.endswith(".git"):
                continue                       # git sources are not archived here
            h = getUrlChecksum(url)
            prefixes.append(("SOURCES/cache/%s/%s/" % (h[:2], h), name))
    return prefixes, unresolved


def enforce_store(store_url, rec, recipes_dir, key_pem=None, dry_run=False,
                  work_dir="sw"):
    """Remove non-compliant packages from the store and its manifests.

    The admin action behind ``bits compliance --enforce``:

      1. delete every store object of a restricted package (all files under its
         ``TARS/<arch>/store/<h2>/<hash>/`` prefix) and its rev-index markers;
      2. rewrite the per-build BOMs without the offending entries (a BOM left
         empty is deleted);
      3. delete the restricted packages' ``SOURCES/cache/`` archives resolved
         from their recipes;
      4. with *key_pem*, re-certify the affected architectures from the
         rewritten BOMs (per-platform scoping; an arch left without packages
         gets an EMPTY signed manifest — revocation). Without a key the next
         CI certification self-heals (missing objects are dropped), but the
         signed manifests are stale until then.

    With *dry_run* every action is printed and nothing is touched. Returns the
    number of issues remaining (0 = store clean after enforcement).
    """
    restricted = {p.lower() for p in rec["restricted"]}
    restricted_src = {p.lower() for p in rec["restricted_sources"]}
    s3, bucket, _endpoint = _s3_client(store_url)
    if not dry_run and not (os.environ.get("AWS_ACCESS_KEY_ID")
                            and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        error("--enforce needs S3 write credentials (a dry run does not)")
        return 1
    act = "[dry-run] would" if dry_run else "will"

    def _delete_prefix(prefix, why):
        n = 0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []) or []:
                info("  %s delete %s (%s)", act, o["Key"], why)
                if not dry_run:
                    s3.delete_object(Bucket=bucket, Key=o["Key"])
                n += 1
        return n

    from bits_helpers.utilities import resolve_store_path
    banner("Enforcing licence compliance on %s%s", bucket,
           " (dry run)" if dry_run else "")

    # 1+2 — walk the BOMs, delete offending objects, rewrite the BOMs.
    paginator = s3.get_paginator("list_objects_v2")
    keys = [o["Key"] for page in paginator.paginate(Bucket=bucket,
                                                    Prefix="MANIFESTS/")
            for o in page.get("Contents", []) or []]
    affected_archs, remaining_boms, removed, repo_prune = set(), [], 0, []
    for key in keys:
        leaf = key.split("/")[-1]
        if (not leaf.endswith(".json") or "/rev-index/" in key
                or leaf.startswith("common-manifest")):
            continue
        try:
            doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as exc:          # pylint: disable=broad-except
            warning("enforce: unreadable manifest %s (%s)", key, exc)
            continue
        pkgs = doc.get("packages") or []
        offending = [e for e in pkgs
                     if str(e.get("package", "")).lower() in restricted]
        if not offending:
            remaining_boms.append(doc)
            continue
        for e in offending:
            arch = e.get("effective_architecture") or ""
            affected_archs.add(arch)
            removed += _delete_prefix(
                resolve_store_path(arch, e.get("hash", "")) + "/",
                "%s — redistributable: false" % e.get("package"))
            removed += _delete_prefix(
                "MANIFESTS/rev-index/%s/%s/" % (arch, e.get("package")),
                "rev-index marker")
        kept = [e for e in pkgs if e not in offending]
        repo_prune.append(leaf)
        if kept:
            doc["packages"] = kept
            info("  %s rewrite %s (%d -> %d packages)", act, key,
                 len(pkgs), len(kept))
            if not dry_run:
                s3.put_object(Bucket=bucket, Key=key,
                              Body=json.dumps(doc, indent=1).encode())
            remaining_boms.append(doc)
        else:
            info("  %s delete %s (no packages left)", act, key)
            if not dry_run:
                s3.delete_object(Bucket=bucket, Key=key)

    # 3 — source archives, for the SOURCE-restricted set (redistributable:
    # binaries|none), resolved from the recipes themselves.
    prefixes, unresolved = _recipe_source_prefixes(recipes_dir, rec, restricted_src)
    for prefix, why in prefixes:
        removed += _delete_prefix(prefix, "source archive of %s" % why)
    for u in unresolved:
        warning("enforce: could not resolve a source URL, clean up manually: %s", u)

    info("Enforcement: %d object(s) %s removed, %d build manifest(s) affected, "
         "architectures: %s", removed, "would be" if dry_run else "",
         len(repo_prune), ", ".join(sorted(affected_archs)) or "none")
    if repo_prune:
        warning("Certification re-derives from the bits-manifests git repo — "
                "prune the matching BOM file(s) there too: %s",
                ", ".join(sorted(repo_prune)))

    # 4 — re-certify the affected platforms from the rewritten BOMs.
    if affected_archs and key_pem and not dry_run:
        import tempfile
        from bits_helpers import certify as _certify
        out = os.path.join(tempfile.mkdtemp(prefix="bits-enforce-"),
                           "common-manifest.json")
        outputs = _certify.certify_by_arch(
            remaining_boms, key_pem, out,
            probe=_certify.make_s3_probe(store_url, work_dir, "enforce"),
            only_archs=sorted(affected_archs))
        for op, sp, arch in outputs:
            for src, dst in ((op, "MANIFESTS/common-manifest-%s.json" % arch),
                             (sp, "MANIFESTS/common-manifest-%s.json.sig" % arch)):
                s3.upload_file(src, bucket, dst)
            info("  re-certified %s -> MANIFESTS/common-manifest-%s.json(.sig)",
                 arch, arch)
    elif affected_archs and not dry_run:
        warning("no --key given: the signed manifests for %s still reference "
                "the removed objects until the next certification (which drops "
                "them as missing). Re-run with --key to re-sign now.",
                ", ".join(sorted(affected_archs)))
    return 0


def doCompliance(args, parser):
    """CLI entrypoint for ``bits compliance``. Returns the exit code."""
    roots = list(getattr(args, "packages", []) or [])
    if roots:
        # Group mode: follow the build's repository-discovery path and audit
        # exactly the resolved dependency closure of the given roots.
        if getattr(args, "recipesDir", None):
            parser.error("--recipes scans a single directory; drop it when "
                         "giving PACKAGE roots (group mode discovers the "
                         "repositories itself)")
        specs, system_pkgs, config_paths = resolve_group_specs(args, parser)
        recipes_dir = os.path.abspath(args.configDir)
        banner("Compliance audit: %s (defaults: %s, architecture: %s)",
               " ".join(roots), "::".join(args.defaults), args.architecture)
        for d in config_paths:
            info("  repository: %s", d)
        rec = scan_specs(specs)
        info("Packages in closure ...... %d", rec["total"])
        if system_pkgs:
            info("  system-provided ......... %d (taken from the host, not "
                 "distributed)", len(system_pkgs))
    else:
        recipes_dir = os.path.abspath(getattr(args, "recipesDir", None) or ".")
        if not any(f.endswith(".sh") for f in os.listdir(recipes_dir)):
            parser.error("no recipes (*.sh) found in %s — point --recipes at a "
                         "recipe repository (e.g. a lcg.bits checkout), or give "
                         "PACKAGE roots for a group-wide audit" % recipes_dir)
        banner("Compliance audit: %s", recipes_dir)
        rec = scan_recipes(recipes_dir)
        info("Recipes scanned .......... %d", rec["total"])
    info("  binaries restricted ..... %d (redistributable: sources|none — the "
         "CVMFS/store exclusion list)", len(rec["restricted"]))
    info("  sources restricted ...... %d (redistributable: binaries|none — "
         "never mirrored to SOURCES/)", len(rec["restricted_sources"]))
    debug("  exclusion list: %s", ", ".join(sorted(rec["restricted"])))
    info("  LicenseRef-* ............ %d (custom ids — verify against the "
         "compliance ruling)", len(rec["licenseref"]))
    info("  NOASSERTION ............. %d (system shims)", len(rec["noassertion"]))

    issues = []
    if rec["missing_license"]:
        issues.append("%d recipe(s) missing a license: field: %s"
                      % (len(rec["missing_license"]),
                         ", ".join(rec["missing_license"])))

    if getattr(args, "enforce", False):
        if getattr(args, "noStoreCheck", False):
            parser.error("--enforce audits and cleans the store; it cannot be "
                         "combined with --no-store-check")
        return enforce_store(args.complianceStore, rec, recipes_dir,
                             key_pem=getattr(args, "enforceKey", None),
                             dry_run=getattr(args, "dryRun", False),
                             work_dir=os.path.abspath(args.workDir)) or (
                                 1 if issues else 0)

    if not getattr(args, "noStoreCheck", False):
        restricted = {p.lower() for p in rec["restricted"]}
        try:
            st = audit_store(args.complianceStore, restricted,
                             os.path.abspath(args.workDir))
        except Exception as exc:          # pylint: disable=broad-except
            error("store audit failed: %s (use --no-store-check to audit "
                  "recipes only)", exc)
            return 1
        info("Store %s: %d build manifest(s), %d signed manifest(s)",
             st["bucket"], st["boms"], st["signed"])
        info("  anonymous access ........ %s",
             "WORLD-READABLE (unauthenticated listing succeeds)"
             if st["public"] else "no (authenticated only)")
        if st["stored"]:
            sev = ("PUBLICLY EXPOSED (bucket is world-readable — this is "
                   "redistribution)" if st["public"] else
                   "present (private store: reuse only, do not publish)")
            line = "%d restricted package build(s) in the store — %s:\n    %s" % (
                len(st["stored"]), sev, "\n    ".join(sorted(st["stored"])))
            if st["public"]:
                issues.append(line)
            else:
                info("  %s", line)
        if st["certified"]:
            issues.append(
                "%d restricted package(s) in SIGNED manifests (certified for "
                "reuse — review): %s"
                % (len(st["certified"]), "; ".join(sorted(st["certified"]))))

    if issues:
        banner("Compliance issues: %d", len(issues))
        for i, msg in enumerate(issues, 1):
            error("[%d] %s", i, msg)
        return 1
    banner("Compliance: no issues found")
    return 0
