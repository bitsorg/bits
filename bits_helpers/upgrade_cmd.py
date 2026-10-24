# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""``bits upgrade``: new package versions as a recipe overlay, then back into the
recipe repositories.

    bits upgrade --manifest M (--plan P --wave 0 [--line BUILD_ID] | --to PKG=VER)
                 --recipe-overlay DIR
writes into DIR (recipe_overlay) a copy of each file that sets the version of a
chosen package: its recipe, the defaults profile whose ``overrides:`` pin it, or
the recipe whose requires pin it, with the version (and a tag that spells it)
changed, and records in overlay.yaml where each came from. The recipes are found
as the build (manifest M) found them: ``bits status`` resolution with its
defaults and architecture. New source URLs, git tags and PyPI versions are checked.

    bits upgrade --apply DIR --target BRANCH [--repo NAME=PATH] [--push]
merges each overlay file three-way into its repository's BRANCH, on a new branch
``upgrade/<session>``, one commit per file, with neutral messages.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import yaml

from bits_helpers import recipe_overlay
from bits_helpers.log import banner, info, warning

TIMEOUT_S = 20


class NeedsHand(Exception):
    """The edit cannot be made safely: left for a hand edit (the message says why)."""


# ── edits of recipe text (the YAML header, line by line, keeping its layout) ──

def _header_end(lines):
    return next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == "---"), len(lines))


def _scalar(raw):
    """(value, quote) of a YAML scalar as written after ``key:``."""
    raw = re.sub(r"\s+#.*$", "", raw.strip())
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1], raw[0]
    return raw, ""


def new_tag(tag, old, new):
    """The tag for version *new* of a tag written for version *old*, None when it
    is derived (``%(version)s``) and needs no edit; NeedsHand when unknown."""
    if "%(" in tag:
        return None
    for sep in (".", "_", "-"):
        spelt = old.replace(".", sep)
        if tag.count(spelt) == 1:
            return tag.replace(spelt, new.replace(".", sep))
    raise NeedsHand("tag %r does not spell version %s" % (tag, old))


def _set(line, key, value, quote):
    try:
        plain = isinstance(yaml.safe_load(value), str)
    except yaml.YAMLError:
        plain = False
    if not quote and not plain:
        quote = '"'                     # 1.10 unquoted would read as 1.1
    m = re.match(r"^(\s*%s:\s*)(.*?)(\s+#.*)?$" % re.escape(key), line.rstrip("\n"))
    return "%s%s%s%s%s\n" % (m.group(1), quote, value, quote, m.group(3) or "")


def _edit_block(lines, keys_at, old, new, what):
    """Edit the ``version:``/``tag:`` lines *keys_at* ({key: line index}) from
    *old* to *new*; returns the edits [(field, from, to)]."""
    if "version" not in keys_at:
        raise NeedsHand("%s sets no version" % what)
    i = keys_at["version"]
    cur, q = _scalar(lines[i].split(":", 1)[1])
    if cur != old:
        raise NeedsHand("%s sets version %s, not %s" % (what, cur, old))
    edits = [("version", old, new)]
    tag_line = keys_at.get("tag")
    new_t = None
    if tag_line is not None:
        tag, tq = _scalar(lines[tag_line].split(":", 1)[1])
        new_t = new_tag(tag, old, new)
        if new_t is not None:
            lines[tag_line] = _set(lines[tag_line], "tag", new_t, tq)
            edits.append(("tag", tag, new_t))
    lines[i] = _set(lines[i], "version", new, q)
    return edits


def edit_recipe(text, old, new):
    """(text, edits) of a recipe with its version *old* set to *new*."""
    lines = text.splitlines(keepends=True)
    end = _header_end(lines)
    keys = {}
    for i in range(end):
        m = re.match(r"^(version|tag):", lines[i])
        if m and m.group(1) not in keys:
            keys[m.group(1)] = i
    edits = _edit_block(lines, keys, old, new, "the recipe")
    return "".join(lines), edits


def _indent(line):
    return len(line) - len(line.lstrip())


def _content(line):
    return bool(line.strip()) and not line.lstrip().startswith("#")


def edit_override(text, package, old, new, active=lambda matcher: True):
    """(text, edits) of a defaults profile with the ``overrides:`` entries of
    *package* that are active (*active(matcher)*) set from version *old* to *new*."""
    lines = text.splitlines(keepends=True)
    end = _header_end(lines)
    start = next((i for i in range(end) if re.match(r"^overrides:\s*(#.*)?$", lines[i])), None)
    if start is None:
        raise NeedsHand("the profile has no overrides:")
    stop = next((i for i in range(start + 1, end) if _content(lines[i]) and not _indent(lines[i])), end)
    first = next((i for i in range(start + 1, stop) if _content(lines[i])), None)
    level = _indent(lines[first]) if first is not None else 0
    edits, found = [], False
    for i in range(start + 1, stop):
        if not _content(lines[i]) or _indent(lines[i]) != level:
            continue                                      # only the entries themselves
        m = re.match(r"^\s+-\s*[\"']?(%s)\s*=\s*([^\"'\s:]+)" % re.escape(package), lines[i], re.I)
        if m and m.group(2) == old:                       # list form: "- pkg = 1.2"
            lines[i] = lines[i][:m.start(2)] + new + lines[i][m.end(2):]
            edits.append(("version", old, new))
            found = True
            continue
        m = re.match(r"^\s+[\"']?([^\s\"':#]+)(:[^\"'#]*?)?[\"']?:\s*(#.*)?$", lines[i])
        if not m or m.group(1).lower() != package.lower():
            continue
        matcher = (m.group(2) or "")[1:] or None
        if matcher and not active(matcher):
            continue
        keys, child = {}, None
        for j in range(i + 1, stop):
            if not _content(lines[j]):
                continue
            if _indent(lines[j]) <= level:
                break
            child = _indent(lines[j]) if child is None else child
            k = re.match(r"^\s+(version|tag):", lines[j])
            if k and _indent(lines[j]) == child and k.group(1) not in keys:
                keys[k.group(1)] = j
        if "version" not in keys:
            continue
        edits += _edit_block(lines, keys, old, new, "its override %s" % m.group(1))
        found = True
    if not found:
        raise NeedsHand("no active override of %s sets version %s" % (package, old))
    return "".join(lines), edits


def edit_pin(text, package, old, new):
    """(text, edits) of a recipe whose requires pin *package* to *old* (``- pkg = old``)."""
    lines = text.splitlines(keepends=True)
    edits = []
    for i in range(_header_end(lines)):
        m = re.match(r"^(\s*-\s*[\"']?%s\s*=\s*)([^\"'\s:]+)" % re.escape(package), lines[i], re.I)
        if m and m.group(2) == old:
            lines[i] = m.group(1) + new + lines[i][m.end(2):]
            edits.append(("pin", old, new))
    if not edits:
        raise NeedsHand("no requires pin of %s = %s" % (package, old))
    return "".join(lines), edits


# ── checks of the new version's sources ──────────────────────────────────────

def _url_exists(url):
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        req = urllib.request.Request(url, method=method, headers=dict(headers, **{"User-Agent": "bits"}))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return r.status < 400, "HTTP %d" % r.status
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 501) and method == "HEAD":
                continue
            return False, "HTTP %d" % e.code
        except (urllib.error.URLError, OSError) as e:
            return False, str(getattr(e, "reason", e))
    return False, "no answer"


def _git_tag_exists(source, tag):
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", source, "refs/tags/" + tag],
                             capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if out.returncode:
        return False, (out.stderr.strip().splitlines() or ["git ls-remote failed"])[-1]
    return bool(out.stdout.strip()), "tag %s %s" % (tag, "found" if out.stdout.strip() else "not found")


def _header(text):
    """The YAML header of a recipe, as a dict ({} when it does not parse)."""
    lines = text.splitlines(keepends=True)
    try:
        meta = yaml.safe_load("".join(lines[:_header_end(lines)])) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def check_sources(spec, recipe_text, version, resolve):
    """[{what, ok, detail}] of the sources of the recipe *recipe_text* (of *spec*)
    at *version*: each tarball URL (HEAD), a git source's tag, a PythonPipRecipe's
    version on PyPI. *resolve* expands %(...)s in a string for a spec."""
    meta = _header(recipe_text)
    new = dict(spec, version=version)
    try:
        new["tag"] = resolve(new, str(meta.get("tag") or version))
    except Exception:   # noqa: BLE001 — an unresolvable tag is reported, not fatal
        new["tag"] = ""
    out = []
    sources = meta.get("sources") or []
    for src in sources if isinstance(sources, list) else []:
        url = re.sub(r"^\([^)]*\)", "", str(src).split(",")[0]).strip()
        try:
            url = resolve(new, url)
        except Exception:   # noqa: BLE001 — an unresolvable entry is reported, not fatal
            url = ""
        if not re.match(r"^https?://", url) or "%(" in url or "$(" in url:
            out.append({"what": "source %s" % src, "ok": None, "detail": "not checked"})
            continue
        ok, detail = _url_exists(url)
        out.append({"what": url, "ok": ok, "detail": detail})
    if meta.get("source") and not sources:
        if not new["tag"] or "%(" in new["tag"]:
            out.append({"what": "tag of %s" % meta["source"], "ok": None, "detail": "not checked"})
        else:
            ok, detail = _git_tag_exists(str(meta["source"]), new["tag"])
            out.append({"what": "%s %s" % (meta["source"], new["tag"]), "ok": ok, "detail": detail})
    if "PythonPipRecipe" in recipe_text:
        name = (meta.get("variables") or {}).get("PYPI_NAME") or spec["package"]
        ok, detail = _url_exists("https://pypi.org/pypi/%s/%s/json" % (
            urllib.parse.quote(str(name)), urllib.parse.quote(version)))
        out.append({"what": "PyPI %s %s" % (name, version), "ok": ok, "detail": detail})
    return out


# ── origins ──────────────────────────────────────────────────────────────────

def _git(directory, *argv, check=True):
    r = subprocess.run(["git", "-C", directory] + list(argv), capture_output=True, text=True,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if check and r.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(argv), r.stderr.strip() or r.stdout.strip()))
    return r.stdout.strip() if r.returncode == 0 else None


def _no_userinfo(url):
    return re.sub(r"(https?://)[^/@]+@", r"\1", url or "")


def origin_of(path, provider_dirs):
    """The repository *path* is in: {name, url, path, commit, blob}."""
    d = os.path.dirname(os.path.abspath(path))
    root = _git(d, "rev-parse", "--show-toplevel")
    rel = os.path.relpath(os.path.abspath(path), root)
    commit = _git(root, "rev-parse", "HEAD")
    url = _no_userinfo(_git(root, "remote", "get-url", "origin", check=False) or "")
    name = next((n for pd, (n, _) in (provider_dirs or {}).items()
                 if os.path.abspath(pd) in (d, root)), None)
    name = name or (re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1]) if url else os.path.basename(root))
    blob = _git(root, "rev-parse", "%s:%s" % (commit, rel), check=False)
    if _git(root, "diff", "--quiet", "HEAD", "--", rel, check=False) is None:
        warning("%s has uncommitted changes: they go into the overlay as edits", path)
    return {"name": name, "url": url, "path": rel, "commit": commit, "blob": blob or ""}


# ── making an overlay ────────────────────────────────────────────────────────

def _read_json(path, what, parser):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        parser.error("cannot read the %s %s: %s" % (what, path, e))
    if not isinstance(doc, dict):
        parser.error("the %s %s is not a JSON object" % (what, path))
    return doc


def _wanted(args, parser):
    """{package: version} to upgrade, from --to or the plan's chosen waves."""
    out = {}
    for item in args.to or []:
        name, sep, version = item.partition("=")
        if not sep or not name.strip() or not version.strip():
            parser.error("--to takes PACKAGE=VERSION, not %r" % item)
        out[name.strip()] = version.strip()
    if not args.plan:
        if not out:
            parser.error("give --plan with --wave (or --converged), or --to PACKAGE=VERSION")
        return out
    plan = _read_json(args.plan, "plan", parser)
    lines = [r for r in plan.get("releases") or [] if isinstance(r, dict)]
    if args.line:
        lines = [r for r in lines if r.get("build_id") == args.line]
    if len(lines) != 1:
        parser.error("the plan has %s release lines%s: choose one with --line BUILD_ID (%s)" % (
            len(lines), " for --line %s" % args.line if args.line else "",
            ", ".join(str(r.get("build_id", "?")) for r in plan.get("releases") or [])))
    line = lines[0]
    if args.converged:
        for c in plan.get("converged") or []:
            mine = [r for r in c.get("releases") or [] if r.get("build_id") == line.get("build_id")]
            if mine and mine[0].get("action") == "upgrade" and c.get("name") and c.get("version"):
                out.setdefault(c["name"], c["version"])
            elif mine and mine[0].get("action") == "pinned":
                warning("%s: pinned by %s in this line, left at %s", c.get("name"),
                        mine[0].get("pinned_by"), mine[0].get("version"))
        return out
    if not args.waves:
        parser.error("choose the waves (--wave 0 --wave 1), or --converged")
    for w in line.get("waves") or []:
        if w.get("wave") in args.waves:
            for e in w.get("packages") or []:
                if e.get("name") and e.get("target"):
                    out.setdefault(e["name"], e["target"])
    return out


def _load_index(directory):
    path = os.path.join(directory, recipe_overlay.INDEX)
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_index(directory, index):
    path = os.path.join(directory, recipe_overlay.INDEX)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(index, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


def _config_dir(manifest, args):
    """-c, else the build's recipe repository when it is here, else as bits build."""
    if args.configDir:
        return args.configDir
    if manifest.get("config_dir") and os.path.isdir(manifest["config_dir"]):
        return manifest["config_dir"]
    return os.environ.get("BITS_REPO_DIR", ".")


def _status_args(manifest, args):
    """A ``bits status`` argument namespace for the build of *manifest*."""
    from bits_helpers.args import doParseArgs
    arch = args.buildArchitecture or manifest.get("raw_architecture") or _strip_qualifiers(manifest)
    argv = ["bits", "status", *manifest["requested_packages"],
            "--defaults", "::".join(manifest.get("defaults") or ["release"]),
            "--architecture", arch, "-c", _config_dir(manifest, args), "-w", args.workDir]
    for v in args.variables or []:
        argv += ["--set", v]
    if args.searchPath:
        argv += ["--search-path", args.searchPath]
    saved = sys.argv
    try:
        sys.argv = argv
        return doParseArgs()
    finally:
        sys.argv = saved


def _strip_qualifiers(manifest):
    """The architecture before the defaults' append_arch, for a manifest that does
    not record it: profile names appended (-gcc15, -opt) are taken off."""
    arch = manifest.get("architecture") or ""
    for d in reversed(manifest.get("defaults") or []):
        if d != "release" and arch.endswith("-" + d):
            arch = arch[:-len(d) - 1]
    return arch


def make_overlay(args, parser):
    manifest = _read_json(args.manifest, "build manifest", parser)
    if recipe_overlay.is_trial(manifest):
        parser.error("%s is the manifest of a trial build: start from the build to upgrade"
                     % args.manifest)
    if not manifest.get("requested_packages"):
        parser.error("%s names no requested_packages: not a build manifest" % args.manifest)
    wanted = _wanted(args, parser)
    overlay = os.path.abspath(args.recipeOverlay)
    os.makedirs(overlay, exist_ok=True)
    index = _load_index(overlay)
    session = index.get("session") or args.session or os.path.basename(overlay)
    index.update({"session": session, "community": index.get("community") or args.community or "",
                  "build": index.get("build") or {
                      "manifest": os.path.basename(args.manifest),
                      "architecture": manifest.get("architecture"),
                      "defaults": manifest.get("defaults"),
                      "requested_packages": manifest.get("requested_packages")},
                  "files": index.get("files") or {}})
    _save_index(overlay, index)          # an overlay from the start (overlay.yaml)

    os.environ.pop(recipe_overlay.ENV, None)       # the repositories' own files
    from bits_helpers.matchers import _matcher_active
    from bits_helpers.paths import resolveDefaultsFilename, resolveFilename
    from bits_helpers.status import resolve_specs
    from bits_helpers.utilities import resolve_spec_data
    sargs, sparser = _status_args(manifest, args)
    r = resolve_specs(sargs, sparser)
    specs = {k.lower(): v for k, v in r["specs"].items()}
    defaults = sargs.defaults
    variables = (r["defaults_meta"] or {}).get("variables") or {}

    def resolve(spec, text):
        return resolve_spec_data(spec, text, defaults, r["branch_basename"], r["branch_stream"],
                                 default_vars=variables, strict=False)

    report = []
    for name, version in sorted(wanted.items(), key=lambda kv: kv[0].lower()):
        spec = specs.get(name.lower())
        row = {"package": name, "to": version}
        report.append(row)
        if spec is None:
            row.update(status="not-in-build", detail="%s is not in this build" % name)
            continue
        built = str(spec.get("version") or "")
        row["from"] = built
        if built == version and not any(spec["package"] in (f.get("packages") or [])
                                         for f in index["files"].values()):
            row.update(status="unchanged", detail="already %s" % version)
            continue
        pin = spec.get("pinned_by") or ""
        try:
            if pin.startswith("defaults:"):
                path = resolveDefaultsFilename(pin.split(":", 1)[1], sargs.configDir, failOnError=False)
                def edit(t, old):
                    return edit_override(t, spec["package"], old, version, lambda m: _matcher_active(
                        m, r["raw_architecture"], defaults, variables, old))
            elif pin.startswith("requires:"):
                owner = specs.get(pin.split(":", 1)[1].lower())
                path = owner and resolveFilename(r["taps"], owner["package"].lower(), sargs.configDir, {})[0]
                def edit(t, old):
                    return edit_pin(t, spec["package"], old, version)
            else:
                path = resolveFilename(r["taps"], spec["package"].lower(), sargs.configDir, {})[0]
                def edit(t, old):
                    return edit_recipe(t, old, version)
            if not path or not os.path.isfile(path):
                raise NeedsHand("no file to edit (%s)" % (pin or "generated recipe"))
            base_name = os.path.basename(path)
            mine = os.path.join(overlay, base_name)
            known = index["files"].get(base_name)
            if known is None:
                known = {"origin": origin_of(path, r["provider_dirs"]), "packages": [], "edits": []}
                with open(path) as f:
                    text = f.read()
            elif os.path.isfile(mine):
                with open(mine) as f:
                    text = f.read()
            else:
                raise NeedsHand("%s is in overlay.yaml but not in the overlay" % base_name)
            # Upgraded before in this overlay: its copy says the version given then.
            now = next((e["to"] for e in known["edits"] if e.get("package") == spec["package"]), built)
            if now == version:
                row.update(status="unchanged", detail="already %s" % version)
                continue
            text, edits = edit(text, now)
        except (NeedsHand, RuntimeError) as e:     # RuntimeError: git, the file is in no repository
            row.update(status="needs-hand", detail=str(e))
            continue
        row.update(status="written", file=base_name, origin=known["origin"]["name"], pinned_by=pin,
                   edits=[{"field": f_, "from": a, "to": b} for f_, a, b in edits])
        if not args.no_check and not pin:
            row["checks"] = check_sources(spec, text, version, resolve)
        with open(mine, "w") as f:
            f.write(text)
        known["packages"] = sorted(set(known["packages"]) | {spec["package"]})
        known["edits"] = [e for e in known["edits"] if e.get("package") != spec["package"]] + [
            {"package": spec["package"], "from": built, "to": version}]
        index["files"][base_name] = known
        _save_index(overlay, index)
    _print_report(report, overlay)
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"overlay": overlay, "session": session, "packages": report}, f, indent=1)
    left = [row["package"] for row in report if row["status"] in ("needs-hand", "not-in-build")]
    if left:
        warning("Not written: %s (see above)", ", ".join(left))
    return 1 if left else 0


def _print_report(report, overlay):
    banner("Recipe overlay %s" % overlay)
    for row in report:
        line = "%-24s %-14s -> %-14s %s" % (row["package"], row.get("from", "?"), row["to"], row["status"])
        if row.get("detail"):
            line += ": " + row["detail"]
        info(line)
        for c in row.get("checks") or []:
            if c["ok"] is False:
                warning("  %s: %s (the trial build will stop there)", c["what"], c["detail"])


# ── copy-back ────────────────────────────────────────────────────────────────

def _merge(current, base, ours):
    """(text, conflicts) of *ours* merged into *current* against *base* (None: the
    file is new in the overlay)."""
    if current is None or current == ours:
        return ours, 0
    if base is None:
        base = ""                       # on the target too, since: all of it conflicts
    if current == base:
        return ours, 0
    if ours == base:
        return current, 0
    with tempfile.TemporaryDirectory(prefix="bits-upgrade-") as tmp:
        paths = []
        for n, t in (("current", current), ("base", base), ("ours", ours)):
            paths.append(os.path.join(tmp, n))
            with open(paths[-1], "w") as f:
                f.write(t)
        r = subprocess.run(["git", "merge-file", "-p", "-L", "target", "-L", "base", "-L", "overlay"]
                           + paths, capture_output=True, text=True)
        if r.returncode < 0 or r.returncode > 127:
            raise RuntimeError("git merge-file: %s" % r.stderr.strip())
        return r.stdout, r.returncode


def _repo_for(origin, args, session, target):
    """A worktree of *origin*'s repository on a new branch from *target*: (repo, path, branch)."""
    work = os.path.abspath(os.path.join(args.workDir, "UPGRADE", session))
    given = dict(x.split("=", 1) for x in args.repo or [] if "=" in x)
    repo = given.get(origin["name"])
    if repo is None:
        repo = os.path.join(work, origin["name"])
        if not os.path.isdir(os.path.join(repo, ".git")):
            if not origin.get("url"):
                raise RuntimeError("%s has no remote URL: give --repo %s=PATH" % (origin["name"], origin["name"]))
            os.makedirs(work, exist_ok=True)
            r = subprocess.run(["git", "clone", "-q", "--no-checkout", origin["url"], repo],
                               capture_output=True, text=True, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
            if r.returncode:
                raise RuntimeError("git clone %s: %s" % (origin["url"], r.stderr.strip()))
    _git(repo, "fetch", "-q", "origin", target)
    tip = _git(repo, "rev-parse", "FETCH_HEAD")
    if _git(repo, "cat-file", "-e", origin["commit"] + "^{commit}", check=False) is None:
        _git(repo, "fetch", "-q", "origin", origin["commit"], check=False)
    if _git(repo, "cat-file", "-e", origin["commit"] + "^{commit}", check=False) is None:
        raise RuntimeError("the overlay's base commit %s is not in %s: push it first, or give "
                           "--repo %s=PATH with a clone that has it"
                           % (origin["commit"][:12], origin.get("url") or repo, origin["name"]))
    branch = "upgrade/%s" % session if len(args.targets) == 1 else "upgrade/%s-%s" % (session, target)
    if _git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + branch, check=False):
        raise RuntimeError("%s already has a branch %s" % (origin["name"], branch))
    tree = os.path.join(work, "%s-%s" % (origin["name"], re.sub(r"[^\w.-]", "_", target)))
    if os.path.exists(tree):
        raise RuntimeError("%s exists: remove it, or the branch, first" % tree)
    _git(repo, "worktree", "add", "-q", "-b", branch, tree, tip)
    return repo, tree, branch


def _show(repo, commit, path):
    """The text of *path* at *commit*, exactly (None when it is not there)."""
    r = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (commit, path)],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _patches_of(text):
    """The patch names a recipe's header lists."""
    return [str(p).split(",")[0].strip() for p in _header(text).get("patches") or [] if isinstance(p, str)]


def _unlisted(overlay, index):
    """The overlay's files that copy-back would not take: not in overlay.yaml, or
    a patch no listed recipe names."""
    listed = set(index["files"]) | {recipe_overlay.INDEX}
    for name in index["files"]:
        if os.path.isfile(os.path.join(overlay, name)):
            with open(os.path.join(overlay, name)) as f:
                listed |= {os.path.join("patches", p) for p in _patches_of(f.read())}
    return sorted(p for p in recipe_overlay.files_of(overlay) if p not in listed)


def apply_overlay(args, parser):
    overlay = os.path.abspath(args.apply)
    index = _load_index(overlay)
    if not index.get("files"):
        parser.error("%s is not a recipe overlay with files (overlay.yaml)" % args.apply)
    if not args.targets:
        parser.error("give the branch to merge into: --target BRANCH (repeatable)")
    failed = False
    left = _unlisted(overlay, index)
    if left:
        warning("Not in overlay.yaml, so not copied back: %s", ", ".join(left))
        failed = True
    session = re.sub(r"[^\w.-]", "_", str(index.get("session") or os.path.basename(overlay)))
    by_repo = {}
    for name, meta in index["files"].items():
        by_repo.setdefault(meta["origin"]["name"], []).append((name, meta))
    for repo_name, items in sorted(by_repo.items()):
        origin = items[0][1]["origin"]
        for target in args.targets:
            try:
                repo, tree, branch = _repo_for(origin, args, session, target)
                done, conflicts = _apply_files(repo, tree, overlay, items)
                if conflicts:
                    warning("%s %s: conflicts in %s; resolve them in %s (branch %s, %d files "
                            "committed before) and commit", repo_name, target,
                            ", ".join(conflicts), tree, branch, done)
                    failed = True
                    continue
                info("%s %s: %d commits on %s (%s)", repo_name, target, done, branch, tree)
                if args.push and done:
                    _git(tree, "push", "-q", "-u", "origin", branch)
                    info("%s: pushed %s", repo_name, branch)
            except RuntimeError as e:
                warning("%s %s: %s", repo_name, target, e)
                failed = True
    return 1 if failed else 0


def _apply_files(repo, tree, overlay, items):
    """Merge and commit each overlay file of one repository into *tree*: (commits,
    files in conflict). A conflict stops there, its merged text (with markers)
    uncommitted."""
    commits = 0
    for name, meta in sorted(items):
        origin = meta["origin"]
        with open(os.path.join(overlay, name)) as f:
            ours = f.read()
        files = [(origin["path"], _show(repo, origin["commit"], origin["path"]), ours)]
        for p in _patches_of(ours):              # the overlay's own patches, next to it
            mine = os.path.join(overlay, "patches", p)
            if os.path.isfile(mine):
                rel = os.path.join(os.path.dirname(origin["path"]), "patches", p)
                with open(mine) as f:
                    files.append((rel, _show(repo, origin["commit"], rel), f.read()))
        changed, conflicts = [], []
        for rel, base, mine in files:
            dest = os.path.join(tree, rel)
            current = None
            if os.path.isfile(dest):
                with open(dest) as f:
                    current = f.read()
            text, n = _merge(current, base, mine)
            if text == current:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as f:
                f.write(text)
            if n:
                conflicts.append(rel)
            changed.append(rel)
        if conflicts:
            return commits, conflicts
        if not changed:
            continue
        _git(tree, "add", "--", *changed)
        message = ", ".join("%s %s" % (e["package"], e["to"]) for e in meta.get("edits") or []) \
            or "%s: update" % name
        _git(tree, "commit", "-q", "-m", message)
        commits += 1
    return commits, []


def doUpgrade(args, parser):
    if args.apply:
        sys.exit(apply_overlay(args, parser))
    if not args.manifest or not args.recipeOverlay:
        parser.error("give --manifest (the build to upgrade) and --recipe-overlay (where to write)")
    sys.exit(make_overlay(args, parser))
