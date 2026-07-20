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
    r"^(package|license|redistributable):[ \t]*(.*?)[ \t]*$", re.M)


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
    """Scan *recipes_dir* for ``*.sh`` recipes; return the audit dict."""
    out = {"total": 0, "missing_license": [], "licenseref": [],
           "noassertion": [], "restricted": [], "by_package": {}}
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
        # defaults-* are config pseudo-packages: never published, licence-free.
        if not lic and not pkg.startswith("defaults"):
            out["missing_license"].append(name)
        if lic.startswith("LicenseRef-"):
            out["licenseref"].append("%s (%s)" % (name, lic))
        if lic == "NOASSERTION":
            out["noassertion"].append(name)
        if meta.get("redistributable", "").lower() == "false":
            out["restricted"].append(pkg)
            out["by_package"][pkg.lower()]["restricted"] = True
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


def doCompliance(args, parser):
    """CLI entrypoint for ``bits compliance``. Returns the exit code."""
    recipes_dir = os.path.abspath(getattr(args, "recipesDir", None) or ".")
    if not any(f.endswith(".sh") for f in os.listdir(recipes_dir)):
        parser.error("no recipes (*.sh) found in %s — point --recipes at a "
                     "recipe repository (e.g. a lcg.bits checkout)" % recipes_dir)

    banner("Compliance audit: %s", recipes_dir)
    rec = scan_recipes(recipes_dir)
    info("Recipes scanned .......... %d", rec["total"])
    info("  redistributable: false . %d (the CVMFS exclusion list)",
         len(rec["restricted"]))
    debug("  exclusion list: %s", ", ".join(sorted(rec["restricted"])))
    info("  LicenseRef-* ............ %d (custom ids — verify against the "
         "compliance ruling)", len(rec["licenseref"]))
    info("  NOASSERTION ............. %d (system shims)", len(rec["noassertion"]))

    issues = []
    if rec["missing_license"]:
        issues.append("%d recipe(s) missing a license: field: %s"
                      % (len(rec["missing_license"]),
                         ", ".join(rec["missing_license"])))

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
