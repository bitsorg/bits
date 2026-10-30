# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The help chat's documentation index: the markdown files of the bits
repositories on GitHub (bits, the services, CVMFS publishing, the containers and
every community's *.bits, each on its default branch) and the site's console docs
(site `console.docs`), split at headings into a SQLite full-text index (FTS5) in
the orchestrator database.

refresh - the worker's step: every BITS_CHAT_DOCS_REFRESH_S, each repository is
          listed again (conditionally) and only its files whose blob changed are
          read again; a host that cannot be reached, or limits the rate, is left
          for the next pass with what it gave before.
embed_sections - with an embedding model (BITS_CHAT_EMBED_MODEL), the worker's
          too: a vector for each section, so that a question finds sections by
          meaning as well as by its words.
search  - the sections that best match a question, for the API.

The index is derived (made again from the sources at any time), so its tables
are created here, not by a migration, and a missing FTS5 only turns the chat off.
"""

import array
import collections
import hashlib
import json
import logging
import math
import operator
import re
import time
import urllib.parse

from . import db

log = logging.getLogger("bits_orchestrator.chat_docs")

# A source is a repository (owner/repo on GitHub, https://host/group/project on
# GitLab: all its markdown files) or one file (owner/repo/path, https://…/file.md).
DEFAULT_SOURCES = ["bitsorg/" + r for r in (
    "bits", "bits-services", "cvmfs-bits", "cvmfs-testbed", "bits-containers",
    "bits-recipe-tools", "bits-providers", "alice.bits", "atlas.bits", "bits.bits", "cms.bits",
    "common.bits", "key4hep.bits", "lcg.bits", "lhcb.bits", "ship.bits", "stacks.bits",
    "testbed.bits")]
RAW = "https://raw.githubusercontent.com/%s/%s/HEAD/%s"
WEB = "https://github.com/%s/%s/blob/HEAD/%s"
GITHUB_TREE = "https://api.github.com/repos/%s/%s/git/trees/HEAD?recursive=1"
MAX_SECTION = 4000        # characters of one indexed section; longer ones are split
MAX_FILE = 2 * 1024 * 1024
MAX_LISTING = 32 * 1024 * 1024
MAX_FILES = 100           # markdown files of one repository, at most
MAX_PAGES = 50            # of a GitLab tree (100 entries each)
SKIP = re.compile(r"(^|/)(vendor|node_modules|third_party|\.github)/|(^|/)(LICENSE|COPYING|NOTICE)[^/]*$",
                  re.IGNORECASE)
RETRY_S = 300            # a pass with errors: the next this soon (without listing again)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS chat_sources (
        source      TEXT PRIMARY KEY,
        etag        TEXT NOT NULL DEFAULT '',
        sha256      TEXT NOT NULL DEFAULT '',
        sections    INTEGER NOT NULL DEFAULT 0,
        fetched_ms  INTEGER NOT NULL DEFAULT 0,
        changed_ms  INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS chat_sections USING fts5(
        source UNINDEXED, url UNINDEXED, title, body, tokenize = 'porter unicode61')""",
    # A repository's markdown files ({file: blob}) as last listed, kept when it
    # cannot be listed; ok_ms when it last was.
    """CREATE TABLE IF NOT EXISTS chat_repos (
        repo        TEXT PRIMARY KEY,
        etag        TEXT NOT NULL DEFAULT '',
        files       TEXT NOT NULL DEFAULT '{}',
        ok_ms       INTEGER NOT NULL DEFAULT 0
    )""",
    # A section's vector (float32, unit length), by the digest of the embedding
    # model, its title and its text: a section that changes gets a new one.
    """CREATE TABLE IF NOT EXISTS chat_vectors (
        digest      TEXT PRIMARY KEY,
        vec         BLOB NOT NULL
    )""",
]


def ensure(conn):
    with db.transaction(conn):
        for sql in SCHEMA:
            conn.execute(sql)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sources)")}
        if "blob" not in cols:          # the blob a file was read at, from its listing
            conn.execute("ALTER TABLE chat_sources ADD COLUMN blob TEXT NOT NULL DEFAULT ''")


def parse_source(spec):
    """(owner, repo, path) of "owner/repo/path"; ValueError otherwise."""
    parts = spec.strip().strip("/").split("/", 2)
    if len(parts) != 3 or not all(parts) or ".." in parts[2].split("/"):
        raise ValueError("a docs source is owner/repo/path: %r" % spec)
    return tuple(parts)


_RAW_URL = re.compile(r"https://[\w.-]+(?::\d+)?/[\w./%+~-]+\.md")
_GITHUB_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_GITLAB_PROJECT = re.compile(r"https://([\w.-]+(?::\d+)?)/([\w.-]+(?:/[\w.-]+)+)")
_GITLAB_FILE = re.compile(r"https://[^/]+/(?:[^/]+/)*?([^/]+)/-/raw/[^/]+/(.+)")


def is_repo(source):
    return bool(_GITHUB_REPO.fullmatch(source) or (
        _GITLAB_PROJECT.fullmatch(source) and "/-/" not in source and not source.endswith(".md")))


def host_of(source):
    return urllib.parse.urlsplit(source).hostname if source.startswith("https://") else "github.com"


def _name(repo, path):
    """A file's name in titles: its path without .md (and docs/), after its
    repository's name but for bits' own."""
    p = re.sub(r"^docs/", "", urllib.parse.unquote(path)).rsplit(".", 1)[0]
    return p if repo == "bits" else "%s %s" % (repo, p)


def locate(source):
    """(raw address, web address, name) of a file: owner/repo/path on GitHub, or
    the https address of a raw markdown file (GitLab's .../-/raw/<ref>/<path> is
    shown at .../-/blob/<ref>/<path>). The name heads its sections' titles."""
    if source.startswith("https://"):
        if not _RAW_URL.fullmatch(source) or "/../" in source:
            raise ValueError("a docs address is https://host/.../file.md: %r" % source)
        m = _GITLAB_FILE.match(source)
        name = _name(m.group(1), m.group(2)) if m else source.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return source, source.replace("/-/raw/", "/-/blob/", 1), name
    owner, repo, path = parse_source(source)
    q = urllib.parse.quote(path, safe="/")
    return RAW % (owner, repo, q), WEB % (owner, repo, q), _name(repo, path)


def sources_of(settings):
    """The sources to index: BITS_CHAT_DOCS, else the bits repositories and the
    site's console docs (`console.docs`)."""
    if settings.chat_docs:
        return list(settings.chat_docs)
    from bits_helpers import site
    return DEFAULT_SOURCES + [str(u) for u in (site.get("console.docs") or [])]


# --- listing repositories ------------------------------------------------------------

class _HostDown(Exception):
    """The host cannot be reached, or limits the rate: leave it for this pass."""


def _get(session, url, etag="", headers=None, limit=MAX_FILE):
    """(status, body bytes, response headers) of a conditional GET without redirects
    (a private file redirects to a sign-in page); at most *limit* bytes."""
    import requests
    h = dict(headers or {})
    if etag:
        h["If-None-Match"] = etag
    try:
        with session.get(url, headers=h, timeout=(5, 20), stream=True, allow_redirects=False) as r:
            if r.status_code in (403, 429) and (r.headers.get("X-RateLimit-Remaining") == "0"
                                                or r.status_code == 429):
                raise _HostDown("%s limits the rate" % urllib.parse.urlsplit(url).hostname)
            if r.status_code != 200:
                return r.status_code, b"", r.headers
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > limit:
                    raise ValueError("%s is larger than %d bytes" % (url, limit))
            return 200, body, r.headers
    except (requests.ConnectionError, requests.Timeout) as e:
        raise _HostDown("%s cannot be reached (%s)" % (urllib.parse.urlsplit(url).hostname,
                                                       type(e).__name__))


def _markdown(paths):
    """The markdown files to index of a repository's [(path, blob)], the top ones first."""
    md = [(p, b) for p, b in paths if p.lower().endswith(".md") and not SKIP.search(p)
          and ".." not in p.split("/")]
    return sorted(md, key=lambda x: (x[0].count("/"), x[0]))[:MAX_FILES]


def _list_github(session, repo, etag, token):
    """(files {source: blob} or None when unchanged, etag)."""
    owner, name = repo.split("/")
    headers = {"Authorization": "Bearer " + token} if token else {}
    st, body, h = _get(session, GITHUB_TREE % (owner, name), etag, headers, MAX_LISTING)
    if st == 304:
        return None, etag
    if st != 200:
        raise ValueError("HTTP %d" % st)
    tree = json.loads(body)
    if tree.get("truncated"):
        raise ValueError("the tree is truncated")
    paths = [(e["path"], e.get("sha", "")) for e in tree.get("tree") or () if e.get("type") == "blob"]
    return {"%s/%s" % (repo, p): b for p, b in _markdown(paths)}, h.get("ETag", "")


def _list_gitlab(session, repo):
    host, project = _GITLAB_PROJECT.fullmatch(repo).groups()
    api = "https://%s/api/v4/projects/%s" % (host, urllib.parse.quote(project, safe=""))
    st, body, _ = _get(session, api, limit=MAX_LISTING)
    if st != 200:
        raise ValueError("HTTP %d" % st)
    branch = json.loads(body).get("default_branch") or "main"
    paths, page = [], "1"
    for _ in range(MAX_PAGES):
        st, body, h = _get(session, "%s/repository/tree?recursive=true&per_page=100&page=%s"
                           % (api, page), limit=MAX_LISTING)
        if st != 200:
            raise ValueError("HTTP %d" % st)
        paths += [(e["path"], e.get("id", "")) for e in json.loads(body) if e.get("type") == "blob"]
        page = h.get("X-Next-Page") or ""
        if not page:
            break
    else:
        raise ValueError("more than %d pages" % MAX_PAGES)
    q = lambda x: urllib.parse.quote(x, safe="/")
    return {"%s/-/raw/%s/%s" % (repo, q(branch), q(p)): b for p, b in _markdown(paths)}


def expand(conn, session, sources, errors, down, now, every_s, token=""):
    """{file source: blob} of *sources* (blob '' for a file named itself). A repository
    listed less than *every_s* ago, or that cannot be listed now, gives its files as
    last listed (or as indexed, before its first listing)."""
    out = {}
    for src in sources:
        if not is_repo(src):
            out.setdefault(src, "")
            continue
        row = conn.execute("SELECT * FROM chat_repos WHERE repo = ?", (src,)).fetchone()
        kept = json.loads(row["files"]) if row else {r[0]: r[1] for r in conn.execute(
            "SELECT source, blob FROM chat_sources WHERE substr(source, 1, ?) = ?",
            (len(src) + 1, src + "/"))}
        host = host_of(src)
        if host in down or (row and now - row["ok_ms"] < 0.9 * every_s * 1000):
            out.update(kept)
            continue
        try:
            if host == "github.com":
                files, etag = _list_github(session, src, row["etag"] if row else "", token)
                files = kept if files is None else files
            else:
                files, etag = _list_gitlab(session, src), ""
        except _HostDown as e:
            down.add(host)
            errors.append(str(e))
            out.update(kept)
            continue
        except Exception as e:
            errors.append("%s not listed: %s" % (src, e))
            out.update(kept)
            continue
        with db.transaction(conn):
            conn.execute("INSERT INTO chat_repos (repo, etag, files, ok_ms) VALUES (?, ?, ?, ?)"
                         " ON CONFLICT(repo) DO UPDATE SET etag = excluded.etag,"
                         " files = excluded.files, ok_ms = excluded.ok_ms",
                         (src, etag, json.dumps(files), now))
        out.update(files)
    return out


# --- splitting ------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def slug(heading):
    """GitHub's anchor of a heading (before its -1, -2 for repeats)."""
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    h = re.sub(r"[`*]", "", h).strip().lower()
    return re.sub(r"[^\w\- ]", "", h).replace(" ", "-")


def _parts(body):
    """*body* in pieces of at most MAX_SECTION characters, cut between paragraphs."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        while len(para) > MAX_SECTION:
            out.append(para[:MAX_SECTION])
            para = para[MAX_SECTION:]
        if cur and len(cur) + len(para) + 2 > MAX_SECTION:
            out.append(cur)
            cur = ""
        cur = cur + "\n\n" + para if cur else para
    if cur.strip():
        out.append(cur)
    return out


def sections(text, name, url):
    """[(title, url#anchor, body)] of a markdown file, one per heading (and the
    text before the first); the title is its path of headings under *name*."""
    out, path, body, anchor, fence = [], [], [], "", False
    seen = collections.Counter()

    def flush():
        text_ = "\n".join(body).strip()
        if text_:
            title = " › ".join([name] + [h for _, h in path])
            for i, part in enumerate(_parts(text_)):
                out.append((title + (" (%d)" % (i + 1) if i else ""),
                            url + ("#" + anchor if anchor else ""), part))

    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        m = None if fence else _HEADING.match(line)
        if not m:
            body.append(line)
            continue
        flush()
        body = []
        level, heading = len(m.group(1)), m.group(2).strip()
        s = slug(heading)
        anchor = s + ("-%d" % seen[s] if seen[s] else "")
        seen[s] += 1
        path = [p for p in path if p[0] < level] + [(level, heading)]
    flush()
    return out


# --- refresh ----------------------------------------------------------------------

def _replace(conn, source, text, etag, sha, blob, now):
    _, url, name = locate(source)
    secs = sections(text, name, url)
    with db.transaction(conn):
        conn.execute("DELETE FROM chat_sections WHERE source = ?", (source,))
        conn.executemany("INSERT INTO chat_sections (source, url, title, body) VALUES (?, ?, ?, ?)",
                         [(source, u, t, b) for t, u, b in secs])
        conn.execute("INSERT INTO chat_sources (source, etag, sha256, blob, sections, fetched_ms,"
                     " changed_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET"
                     " etag = excluded.etag, sha256 = excluded.sha256, blob = excluded.blob,"
                     " sections = excluded.sections, fetched_ms = excluded.fetched_ms,"
                     " changed_ms = excluded.changed_ms",
                     (source, etag, sha, blob, len(secs), now, now))
    return len(secs)


def refresh(conn, sources, session=None, every_s=3600, now=None, token=""):
    """When due, list the repositories and read the files that changed; returns how
    many were indexed again. Files and repositories no longer listed leave the
    index. Errors are reported (RuntimeError) after the rest is done, and the next
    pass is then in RETRY_S, which reads again only what failed."""
    import requests
    from . import reconcile
    ensure(conn)
    fixed_now = now
    now = now or db.now_ms()
    # float(): a time once stored as "….0" (every_s a float) stopped every pass.
    if now < float(reconcile._kv_get(conn, "chat.docs.next_ms", "0") or 0):
        return 0
    own = session is None
    session = session or requests.Session()
    changed, errors, down = 0, [], set()
    try:
        files = expand(conn, session, sources, errors, down, now, every_s, token)
        with db.transaction(conn):
            for (gone,) in conn.execute("SELECT source FROM chat_sources").fetchall():
                if gone not in files:
                    conn.execute("DELETE FROM chat_sections WHERE source = ?", (gone,))
                    conn.execute("DELETE FROM chat_sources WHERE source = ?", (gone,))
            for (repo,) in conn.execute("SELECT repo FROM chat_repos").fetchall():
                if repo not in sources:
                    conn.execute("DELETE FROM chat_repos WHERE repo = ?", (repo,))
        changed = _read_all(conn, files, session, now, errors, down)
    finally:
        if own:
            session.close()
        end = fixed_now or db.now_ms()     # from the end of the pass: a slow one is not again at once
        with db.transaction(conn):
            reconcile._kv_set(conn, "chat.docs.next_ms",
                              str(int(end + 1000 * (min(RETRY_S, every_s) if errors else every_s))))
    if errors:
        raise RuntimeError("docs not read: " + "; ".join(errors[:10])
                           + ("; and %d more" % (len(errors) - 10) if len(errors) > 10 else ""))
    return changed


def _read_all(conn, files, session, now, errors, down):
    """Read and index the *files* ({source: blob}) whose blob is not the one indexed;
    returns how many changed, appends to *errors*."""
    changed = 0
    for source, blob in files.items():
        host = host_of(source)
        if host in down:
            continue
        row = conn.execute("SELECT * FROM chat_sources WHERE source = ?", (source,)).fetchone()
        if blob and row and row["blob"] == blob:
            continue                       # as listed, unchanged since it was read
        try:
            raw = locate(source)[0]
            status_, body, h = _get(session, raw, row["etag"] if row else "")
        except _HostDown as e:
            down.add(host)
            errors.append(str(e))
            continue
        except Exception as e:
            errors.append("%s: %s" % (source, e))
            continue
        if status_ == 304 and row:
            with db.transaction(conn):
                conn.execute("UPDATE chat_sources SET fetched_ms = ?, blob = ? WHERE source = ?",
                             (now, blob, source))
            continue
        if status_ != 200:
            errors.append("%s: HTTP %s" % (source, status_))
            continue
        text = body.decode("utf-8", "replace")
        sha = hashlib.sha256(text.encode()).hexdigest()
        etag = h.get("ETag", "")
        if row and row["sha256"] == sha:
            with db.transaction(conn):
                conn.execute("UPDATE chat_sources SET fetched_ms = ?, etag = ?, blob = ?"
                             " WHERE source = ?", (now, etag, blob, source))
            continue
        _replace(conn, source, text, etag, sha, blob, now)
        changed += 1
    return changed


# --- search ------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
STOP = frozenset("""a an and are as at be but by can do does for from how i if in into is it
    its me my no not of on or so that the their then there this to use using was what when
    where which who why will with you your should would could get got have has""".split())


def query(text, limit_terms=24):
    """An FTS5 query of the words of *text*: each quoted, any one may match."""
    terms = []
    for w in _WORD.findall(text or ""):
        w = w.strip(".-+").lower()
        if len(w) > 1 and w not in STOP and w not in terms:
            terms.append(w)
    return " OR ".join('"%s"' % t.replace('"', "") for t in terms[:limit_terms])


# Recipe repositories every community's builds draw on: never another community's.
SHARED_RECIPES = frozenset(("common.bits", "stacks.bits", "lcg.bits", "bits.bits"))


def repo_of(source):
    """The repository name of an indexed file (bits, alice.bits, bits-console, …)."""
    m = _GITLAB_FILE.match(source)
    return m.group(1) if m else source.split("/")[1]


SHORT = 600               # characters: a section this short comes with its subsections
RRF_K = 60                # rank merging: a result in both lists beats the top of one
EMBED_BATCH = 16          # sections to the embedding model at once


def _ranked(conn, text, n):
    q = query(text)
    if not q:
        return []
    return conn.execute("SELECT rowid, source, title, url, body FROM chat_sections WHERE"
                        " chat_sections MATCH ? ORDER BY bm25(chat_sections, 0.0, 0.0, 4.0, 1.0)"
                        " LIMIT ?", (q, n)).fetchall()


def _merged(lists):
    """The rows of *lists* (each best first) by reciprocal rank: best in all first."""
    score, rows = collections.Counter(), {}
    for ranked in lists:
        for i, r in enumerate(ranked):
            score[r["rowid"]] += 1.0 / (RRF_K + i)
            rows.setdefault(r["rowid"], r)
    return sorted(rows.values(), key=lambda r: -score[r["rowid"]])


def digest(model, title, body):
    return hashlib.sha256("\0".join((model, title, body)).encode()).hexdigest()


def _unit(vec):
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / n for x in vec))


def embed_sections(conn, model, embed, budget_s=20, now=None):
    """Vectors, from *embed*([(title, body)]) -> [[float]], for the sections that have
    none for *model*, for up to *budget_s* (the rest at the next pass); those of
    sections no longer indexed are dropped. Returns how many were made. After a
    failure (raised) the next try is in RETRY_S."""
    from . import reconcile
    ensure(conn)
    now = now or db.now_ms()
    if now < float(reconcile._kv_get(conn, "chat.vectors.next_ms", "0") or 0):
        return 0
    want = {}
    for r in conn.execute("SELECT title, body FROM chat_sections"):
        want.setdefault(digest(model, r["title"], r["body"]), (r["title"], r["body"]))
    have = {d for (d,) in conn.execute("SELECT digest FROM chat_vectors")}
    if have - set(want):
        with db.transaction(conn):
            conn.executemany("DELETE FROM chat_vectors WHERE digest = ?", [(d,) for d in have - set(want)])
    todo = [(d, s) for d, s in want.items() if d not in have]
    made, t0 = 0, time.monotonic()
    for i in range(0, len(todo), EMBED_BATCH):
        if time.monotonic() - t0 > budget_s:
            break
        batch = todo[i:i + EMBED_BATCH]
        try:
            vecs = embed([s for _, s in batch])
            if len(vecs) != len(batch):
                raise ValueError("the embedding model gave %d vectors for %d sections" % (len(vecs), len(batch)))
        except Exception:
            with db.transaction(conn):
                reconcile._kv_set(conn, "chat.vectors.next_ms", str(int(now + RETRY_S * 1000)))
            raise
        with db.transaction(conn):
            conn.executemany("INSERT OR REPLACE INTO chat_vectors (digest, vec) VALUES (?, ?)",
                             [(d, _unit(v).tobytes()) for (d, _), v in zip(batch, vecs)])
        made += len(batch)
    return made


def _nearest(conn, model, vec, n):
    """The *n* sections nearest to *vec* (a question's, from *model*), best first."""
    q = _unit(vec)
    vecs = dict(conn.execute("SELECT digest, vec FROM chat_vectors").fetchall())
    scored = []
    for r in conn.execute("SELECT rowid, source, title, url, body FROM chat_sections"):
        b = vecs.get(digest(model, r["title"], r["body"]))
        if b and len(b) == 4 * len(q):
            v = array.array("f")
            v.frombytes(b)
            scored.append((sum(map(operator.mul, q, v)), r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:n]]


def _subsections(conn, r, shown):
    """(body, [(rowid, url)]) of a short section with the subsections after it, up to
    MAX_SECTION; rowids in *shown* are left out. A document's own intro (its title is
    the file and its first heading at most) brings none: that would be the whole file.
    A file's sections are stored together, in order."""
    body, got = r["body"], []
    if len(body) >= SHORT or r["title"].count(" › ") < 2:
        return body, got
    prefixes = tuple({r["title"] + " › ", re.sub(r" \(\d+\)$", "", r["title"]) + " › "})
    for c in conn.execute("SELECT rowid, source, title, url, body FROM chat_sections WHERE rowid > ?"
                          " ORDER BY rowid LIMIT 12", (r["rowid"],)):
        if c["source"] != r["source"] or not c["title"].startswith(prefixes):
            break
        if c["rowid"] in shown:
            continue
        head = c["title"].split(" › ", r["title"].count(" › ") + 1)[-1]
        add = "\n\n## %s\n\n%s" % (head, c["body"])
        if len(body) + len(add) > MAX_SECTION:
            break
        body += add
        got.append((c["rowid"], c["url"]))
    return body, got


def search(conn, text, limit=6, budget=24000, community="", near=None):
    """[{title, url, body, also}] best first, at most *limit* and about *budget* characters;
    *also*: the urls of the subsections a short section brought along. *near*: (model,
    the question's vector), whose nearest sections are merged in rank by rank. With *community*,
    its own recipe repository (<community>.bits) comes first and another community's
    comes last, unless the question names that community."""
    try:
        rows = _ranked(conn, text + (" " + community if community else ""), limit * 4)
    except Exception as e:                 # no index yet, or no FTS5
        log.warning("docs search: %s", e)
        return []
    if near:
        try:
            rows = _merged([rows, _nearest(conn, near[0], near[1], limit * 4)])
        except Exception as e:             # no vectors yet: by words only
            log.warning("docs search by meaning: %s", e)
    if community:
        own, words = community.lower() + ".bits", set(_WORD.findall((text or "").lower()))

        def rank(r):
            repo = repo_of(r["source"])
            if repo == own:
                return 0
            other = (repo.endswith(".bits") and repo not in SHARED_RECIPES
                     and repo[:-5] not in words)
            return 2 if other else 1
        rows = sorted(rows, key=rank)          # stable: the search's order within each
    out, used, shown = [], 0, set()        # shown: rowids given, or brought along
    for r in rows:
        if len(out) >= limit:
            break
        if r["rowid"] in shown:
            continue
        body, got = _subsections(conn, r, shown)
        if out and used + len(body) > budget:
            break
        out.append({"title": r["title"], "url": r["url"], "body": body,
                    "also": list(dict.fromkeys(u for _, u in got))})
        shown.update([r["rowid"]] + [i for i, _ in got])
        used += len(body)
    return out


def status(conn):
    """{sources, sections, changed_ms, vectors} of the index; zeros without one."""
    try:
        r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(sections), 0) AS s,"
                         " COALESCE(MAX(changed_ms), 0) AS c FROM chat_sources").fetchone()
    except Exception:
        return {"sources": 0, "sections": 0, "changed_ms": 0, "vectors": 0}
    try:
        v = conn.execute("SELECT COUNT(*) FROM chat_vectors").fetchone()[0]
    except Exception:                      # an index from before the vectors
        v = 0
    return {"sources": r["n"], "sections": r["s"], "changed_ms": r["c"], "vectors": v}
