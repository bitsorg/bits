# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""``bits upgrade``: set new versions of packages in the recipes, in place.

    bits upgrade PACKAGE... [plan.csv] [--defaults D] [-a ARCH] [-c DIR]
                 [--wave N ...] [--line LINE] [--to PKG=VER ...]

resolves PACKAGE as ``bits build`` would (``bits status``), finds for each
package the file that sets its version (its recipe, the defaults profile whose
``overrides:`` pin it, or the recipe whose requires pin it) and edits its
``version:`` (and a ``tag:`` that spells it). A file read from a repository
provider's checkout (a cache under the work directory) is first copied, with its
patches, into the recipe directory -c, which is searched first. The versions
come from a plan exported as CSV from bits-console (by default the newest
upgrade-plan-*.csv in the current directory) and --to. A new version with no
tarball is taken from a git tag instead.
Then build, fix what is left, and commit the files to a branch of their
repositories.
"""

import csv
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

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
        out.append({"what": url, "ok": ok, "detail": detail, "tarball": True})
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


# ── a git tag for a version without a tarball ────────────────────────────────

_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$")


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": "bits"}))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def _github(url):
    m = _GITHUB.match(str(url or "").strip())
    return "https://github.com/%s/%s" % m.groups() if m and m.group(1) != "sponsors" else None


def _repos(meta, package):
    own = str(meta.get("source") or "")
    if own and "%(" not in own:
        yield own, False
    for ref in meta.get("references") or []:
        m = re.match(r"^pkg:pypi/([^@?#\s]+)", str(ref))
        about = (_get_json("https://pypi.org/pypi/%s/json" % urllib.parse.quote(m.group(1))) or {}
                 ).get("info") if m else None
        if isinstance(about, dict):
            urls = list((about.get("project_urls") or {}).values()) + [about.get("home_page")]
            yield from ((g, False) for g in map(_github, urls) if g)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    found = _get_json("https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": "%s in:name" % package, "sort": "stars", "per_page": 5}), headers) or {}
    items = [i for i in found.get("items") or [] if isinstance(i, dict)
             and isinstance(i.get("html_url"), str) and isinstance(i.get("name"), str)]
    items.sort(key=lambda i: i["name"].lower() != package.lower())     # same name first
    yield from ((i["html_url"], True) for i in items)


def candidate_repos(meta, package):
    """(repository, guessed), lazily: the recipe's own git source, the GitHub projects
    PyPI names for its pkg:pypi references, then a GitHub search by name (a guess)."""
    seen = set()
    for repo, guessed in _repos(meta, package):
        key = re.sub(r"\.git$", "", repo.rstrip("/").lower())
        if key not in seen:
            seen.add(key)
            yield repo, guessed


def _tags(repo):
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", repo], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.split("refs/tags/", 1)[1].replace("^{}", "") for ln in out.stdout.splitlines()
            if "refs/tags/" in ln} if out.returncode == 0 else set()


def find_tag(tags, package, version):
    """The tag of *tags* for *version* of *package*, in a usual spelling, or None."""
    forms = (version, version.replace(".", "_"), version.replace(".", "-"))
    prefixes = ["", "v", "release-", "rel-"] + [n + sep for n in dict.fromkeys(
        (package, package.lower())) for sep in ("-", "_", "-v")]
    return next((p + f for f in forms for p in prefixes if p + f in tags), None)


def to_git(text, repo, tag, version):
    """*text* with ``source: repo``, ``tag:`` (built from %(version)s when it can
    be), and its ``sources:`` and ``patches:`` commented out."""
    if tag.count(version) == 1:
        tag = tag.replace(version, "%(version)s")
    lines = text.splitlines(keepends=True)
    end, i, noted = _header_end(lines), 0, False
    while i < end:
        if re.match(r"^(sources|patches):", lines[i]):
            j = i + 1
            while j < end:
                k = next((k for k in range(j, end) if _content(lines[k])), end)
                if k == end or lines[k][:1] not in (" ", "\t", "-"):
                    break                  # blank lines and comments up to the next key stay
                j = k + 1
            block = ["# " + ln for ln in lines[i:j]]
            if not noted:
                block.insert(0, "# bits upgrade: no tarball of %s, built from the git tag\n" % version)
                noted = True
            lines[i:j] = block
            end += len(block) - (j - i)
            i += len(block)
            continue
        i += 1
    for key, value, quote in (("source", repo, ""), ("tag", tag, '"')):
        at = next((k for k in range(end) if re.match(r"^%s:" % key, lines[k])), None)
        if at is not None:
            lines[at] = _set(lines[at], key, value, quote)
        else:
            anchor = ("source", "version") if key == "tag" else ("version",)
            after = next((k for a in anchor for k in range(end) if re.match(r"^%s:" % a, lines[k])), -1)
            lines.insert(after + 1, "%s: %s%s%s\n" % (key, quote, value, quote))
            end += 1
    return "".join(lines)


AUTORECONF = ("[ -e configure ] || [ ! -e configure.ac ] || autoreconf -fi"
              "   # a git checkout has no generated configure")


def generate_configure(text):
    """(text, "made"|"check"|None): an autotools recipe made to generate
    ``configure``, which a git checkout lacks (release tarballs ship it), with
    ``autoreconf -fi`` first in its Configure(), or in a Configure() added for
    AutoToolsRecipe's default; "check" when it runs a configure some other way."""
    lines = text.splitlines(keepends=True)
    start = _header_end(lines) + 1
    code = [ln for ln in lines[start:] if _content(ln)]
    runs = any(re.search(r"/configure([\s;\"')]|$)", ln) for ln in code)
    if any("autoreconf" in ln for ln in code) or not (runs or any("AutoToolsRecipe" in ln for ln in code)):
        return text, None
    defs = [i for i in range(start, len(lines))
            if re.match(r"^\s*(function\s+Configure\b|Configure\s*\()", lines[i])]
    at = []
    for i in defs:
        m = re.match(r"^(\s*)(function\s+)?Configure\s*(\(\s*\))?\s*(\{)?\s*(#.*)?$", lines[i])
        brace = i if m and m.group(4) else i + 1
        if not m or brace >= len(lines) or not (m.group(4) or lines[brace].strip() == "{"):
            return text, "check"              # a Configure() written otherwise
        at.append((brace, m.group(1)))
    for brace, indent in reversed(at):         # every definition (one per platform, say)
        lines.insert(brace + 1, indent + "  " + AUTORECONF + "\n")
    if at:
        return "".join(lines), "made"
    if not any("AutoToolsRecipe" in ln for ln in code):
        return text, "check"
    body = "".join(lines[start:])
    lines.append(("\n" if body and not body.endswith("\n") else "") +
                 "\nfunction Configure() {\n  %s\n  [ -f autogen.sh ] && ./autogen.sh\n"
                 "  ./configure --prefix=\"$INSTALLROOT\"\n}\n" % AUTORECONF)
    return "".join(lines), "made"


def git_instead(spec, text, version):
    """(text, repository, tag, guessed) of the recipe built from a git tag of
    *version*, or None when no repository has one."""
    meta = _header(text)
    for repo, guessed in candidate_repos(meta, spec["package"]):
        tag = find_tag(_tags(repo), spec["package"], version)
        if tag:
            return to_git(text, repo, tag, version), repo, tag, guessed
    return None


# ── what to upgrade ──────────────────────────────────────────────────────────

PLAN_GLOB = "upgrade-plan-*.csv"     # the name bits-console gives its CSV exports


def newest_plan(directory="."):
    """The newest CSV export of bits-console's plan in *directory*, or None."""
    import glob
    found = glob.glob(os.path.join(directory, PLAN_GLOB))
    return max(found, key=os.path.getmtime) if found else None


def split_plan(args, parser):
    """A ``.csv`` among the PACKAGE arguments is the plan (as --plan)."""
    plans = [p for p in args.pkgname if p.lower().endswith(".csv")]
    args.pkgname = [p for p in args.pkgname if p not in plans]
    if len(plans) + bool(args.plan) > 1:
        parser.error("give one plan")
    if not args.pkgname:
        parser.error("give the package(s) the build builds, e.g. bits upgrade LCG plan.csv")
    args.plan = args.plan or (plans[0] if plans else None)


def wanted(args, parser):
    """{package: version} from --to, and from the console's CSV export of a plan
    (--plan, else the newest in the current directory): per release line (wave,
    package, from, to, ...) the rows of the --wave(s), every wave by default, or
    converged (package, version, ..., line, ..., action) --line's "upgrade" rows."""
    out = {}
    for item in args.to or []:
        name, sep, version = item.partition("=")
        if not sep or not name.strip() or not version.strip():
            parser.error("--to takes PACKAGE=VERSION, not %r" % item)
        out[name.strip()] = version.strip()
    if out and not (args.plan or args.waves or args.line):
        return out                        # only --to
    args.plan = args.plan or newest_plan()
    if not args.plan:
        parser.error("no plan: export it as CSV from bits-console's Plan view into this directory "
                     "(%s), give --plan FILE, or --to PACKAGE=VERSION" % PLAN_GLOB)
    info("Plan: %s", args.plan)
    try:
        with open(args.plan, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if None not in r.values()]
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        parser.error("cannot read the plan %s: %s" % (args.plan, e))
    head = set(rows[0]) if rows else set()
    plan = {}
    if {"wave", "package", "to"} <= head:
        for r in rows:                     # waves in order: a later one's target wins
            if r["wave"].isdigit() and (not args.waves or int(r["wave"]) in args.waves) and r["to"]:
                plan[r["package"]] = r["to"]
    elif {"package", "version", "line", "action"} <= head:
        lines = sorted({r["line"] for r in rows})
        if args.line not in lines:
            parser.error("a converged plan: choose --line (%s)" % ", ".join(lines))
        for r in rows:
            if r["line"] == args.line and r["action"] == "upgrade":
                if plan.get(r["package"], r["version"]) != r["version"]:
                    parser.error("the plan gives %s two versions for %s" % (r["package"], args.line))
                plan[r["package"]] = r["version"]
    else:
        parser.error("%s is not a plan exported as CSV from the console" % args.plan)
    if not plan:
        parser.error("nothing to upgrade in what was chosen from %s" % args.plan)
    return dict(plan, **out)            # --to wins


# ── where each version is set ────────────────────────────────────────────────

def _status_args(args):
    """A ``bits status`` argument namespace for the build *args* describe."""
    from bits_helpers.args import doParseArgs
    argv = ["bits", "status", *args.pkgname, "--defaults", args.defaults,
            "--architecture", args.architecture, "-c", args.configDir, "-w", args.workDir]
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


def _cached(path):
    """Is *path* in a repository provider's checkout (bits' cache, not to edit)?"""
    return os.path.exists(os.path.join(os.path.dirname(os.path.realpath(path)), ".bits_provider_ok"))


def _patches_of(text):
    """The patch files a recipe header names (matchers and checksums taken off)."""
    from bits_helpers.matchers import _parse_patch_entry
    return [_parse_patch_entry(p)[0].strip() for p in _header(text).get("patches") or []
            if isinstance(p, str)]


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _bring_in(path, config_dir, package, dry):
    """A file of a provider's cached checkout: its copy in *config_dir* (which is
    searched first), with the patches and the checksum file it uses. Returns
    (the copy, [what was copied]); NeedsHand when a copy would replace a file."""
    src_dir = os.path.dirname(path)
    dest = os.path.join(config_dir, os.path.basename(path))
    if os.path.exists(dest):
        raise NeedsHand("%s exists: it would be replaced by %s's" % (dest, path))
    with open(path) as f:
        text = f.read()
    todo = [(path, dest)]
    for p in _patches_of(text) + ["../checksums/%s.checksum" % package.lower()]:
        rel = os.path.normpath(os.path.join("patches", p))
        src, mine = os.path.join(src_dir, rel), os.path.join(config_dir, rel)
        if "%(" in p and rel.startswith("patches"):
            warning("%s: patch %s is named by a variable: copy it by hand", package, p)
        elif os.path.isfile(src) and not os.path.exists(mine):
            todo.append((src, mine))
        elif os.path.isfile(src) and _bytes(src) != _bytes(mine):
            warning("%s: %s differs from %s's: kept", package, mine, path)
    if not dry:
        for src, mine in todo:
            os.makedirs(os.path.dirname(mine), exist_ok=True)
            shutil.copyfile(src, mine)
    return dest, [os.path.relpath(m, config_dir) for _, m in todo]


def _recipe_file(taps, spec):
    """The recipe file *spec* was read from (in its own directory), or None."""
    from bits_helpers.paths import checkForFilename
    path = checkForFilename(taps, spec["package"].lower(), spec.get("pkgdir") or "")
    return path if os.path.isfile(path) and path.endswith(".sh") else None


def _origin(path, provider_dirs):
    """The repository a file was read from: its provider's name, else its git root's."""
    d = os.path.dirname(os.path.abspath(path))
    for pd, (name, commit) in (provider_dirs or {}).items():
        if os.path.abspath(pd) == d:
            return "%s@%s" % (name, str(commit)[:10])
    r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.basename(r.stdout.strip()) if r.returncode == 0 else d


def doUpgrade(args, parser):
    split_plan(args, parser)
    want = wanted(args, parser)
    from bits_helpers.matchers import _matcher_active
    from bits_helpers.paths import resolveDefaultsFilename
    from bits_helpers.status import resolve_specs
    from bits_helpers.utilities import resolve_spec_data
    sargs, sparser = _status_args(args)
    r = resolve_specs(sargs, sparser)
    specs = {k.lower(): v for k, v in r["specs"].items()}
    variables = (r["defaults_meta"] or {}).get("variables") or {}
    dry = bool(getattr(args, "dryRun", False))

    def resolve(spec, text):
        return resolve_spec_data(spec, text, sargs.defaults, r["branch_basename"], r["branch_stream"],
                                 default_vars=variables, strict=False)

    seen = {}
    for name, version in want.items():
        if seen.setdefault(name.lower(), version) != version:
            parser.error("%s is given two versions" % name)
    rows, copies, texts = [], {}, {}       # cached file -> its copy; file -> its text now
    for name, version in sorted(want.items(), key=lambda kv: kv[0].lower()):
        spec = specs.get(name.lower())
        row = {"package": name, "to": version}
        rows.append(row)
        if spec is None:
            row.update(status="not in the build")
            continue
        old = row["from"] = str(spec.get("version") or "")
        if old == version:
            row.update(status="unchanged")
            continue
        pin = spec.get("pinned_by") or ""
        try:
            if pin.startswith("defaults:"):
                path = resolveDefaultsFilename(pin.split(":", 1)[1], sargs.configDir, failOnError=False)
                if path and _cached(path):
                    raise NeedsHand("its profile is in a provider's cached checkout (%s): check that"
                                    " repository out and build with it" % _origin(path, r["provider_dirs"]))
                def edit(t):
                    return edit_override(t, spec["package"], old, version, lambda m: _matcher_active(
                        m, r["raw_architecture"], sargs.defaults, variables, old))
            elif pin.startswith("requires:"):
                owner = specs.get(pin.split(":", 1)[1].lower())
                path = owner and _recipe_file(r["taps"], owner)
                def edit(t):
                    return edit_pin(t, spec["package"], old, version)
            else:
                path = _recipe_file(r["taps"], spec)
                def edit(t):
                    return edit_recipe(t, old, version)
            if not path or not os.path.isfile(path):
                raise NeedsHand("no recipe file to edit (%s)" % (pin or "a generated recipe?"))
            target = copies.get(path, path)        # edited before in this run: its new text
            if target not in texts:
                with open(path) as f:
                    texts[target] = f.read()
            text, edits = edit(texts[target])
            where = _origin(path, r["provider_dirs"])
            if path in copies:
                row["detail"] = "in the copy made above"
            elif _cached(path):           # a provider's cached checkout: work on a copy
                target, copied = _bring_in(path, sargs.configDir, os.path.basename(path)[:-3], dry)
                copies[path], texts[target] = target, texts.pop(path)
                row["detail"] = "copied %s from %s" % (", ".join(copied), where)
            else:
                row["detail"] = "in %s" % where
        except NeedsHand as e:
            row.update(status="edit by hand", detail=str(e))
            continue
        if not args.no_check and not pin:
            checks = check_sources(spec, text, version, resolve)
            missing = [c for c in checks if c["ok"] is False]
            tarballs = [c["ok"] for c in checks if c.get("tarball")]
            git = tarballs and not any(tarballs) and git_instead(spec, text, version)
            if git:                       # no tarball of the version at all: a git tag
                text, repo, tag, guessed = git
                missing = [c for c in missing if not c.get("tarball")]
                edits.append(("source", "tarballs", "%s %s" % (repo, tag)))
                row["detail"] += "; sources:%s commented out" % (
                    " and patches:" if _header(texts[target]).get("patches") else "")
                text, made = generate_configure(text)
                if made == "made":
                    row["detail"] += "; configure generated (autoreconf -fi)"
                elif made == "check":
                    row["detail"] += "; a git checkout has no configure: check how it is made"
                    row["check"] = True
                if guessed:
                    row["detail"] += "; %s was found by a GitHub search: check it" % repo
                    row["check"] = True
            elif tarballs and not any(tarballs):
                row["detail"] += "; no git repository found with a tag of %s" % version
            row["missing"] = missing
        path, texts[target] = target, text
        if not dry:
            with open(path, "w") as f:
                f.write(text)
        row.update(status="edited", file=os.path.relpath(path),
                   edits=", ".join("%s %s -> %s" % e for e in edits))
    banner("Upgrade%s" % (" (dry run: nothing written)" if dry else ""))
    for row in rows:
        info("%-24s %-14s -> %-14s %s%s", row["package"], row.get("from", "?"), row["to"], row["status"],
             ": %s" % row["file"] if row.get("file") else "")
        for k in ("edits", "detail"):
            if row.get(k):
                info("%26s%s", "", row[k])
        for c in row.get("missing") or []:
            warning("  %s: %s (the build will stop there)", c["what"], c["detail"])
    left = [row["package"] for row in rows if row["status"] in ("edit by hand", "not in the build")]
    check = [row["package"] for row in rows if row.get("missing") or row.get("check")]
    if left:
        warning("Not edited: %s", ", ".join(left))
    if check:
        warning("Edited, to check: %s", ", ".join(check))
    sys.exit(1 if left or check else 0)
