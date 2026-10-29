# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The help chat's documentation index: the bits docs and the community READMEs
from GitHub (each repository's default branch), split at headings into a SQLite
full-text index (FTS5) in the orchestrator database.

refresh - the worker's step: every BITS_CHAT_DOCS_REFRESH_S, each source is read
          again when it changed (ETag) and its sections replaced.
search  - the sections that best match a question, for the API.

The index is derived (made again from the sources at any time), so its tables
are created here, not by a migration, and a missing FTS5 only turns the chat off.
"""

import collections
import hashlib
import logging
import re

from . import db

log = logging.getLogger("bits_orchestrator.chat_docs")

# owner/repo/path, read from the repository's default branch (HEAD).
DEFAULT_SOURCES = (
    ["bitsorg/bits/" + p for p in ("README.md", "docs/FAQ.md", "docs/USERGUIDE.md",
                                   "docs/WORKFLOWS.md", "docs/COOKBOOK.md",
                                   "docs/REFERENCE.md", "CHANGELOG.md")]
    + ["bitsorg/%s/README.md" % r for r in (
        "alice.bits", "atlas.bits", "bits.bits", "bits-providers", "cms.bits", "key4hep.bits",
        "lcg.bits", "lhcb.bits", "ship.bits", "stacks.bits", "testbed.bits")])
RAW = "https://raw.githubusercontent.com/%s/%s/HEAD/%s"
WEB = "https://github.com/%s/%s/blob/HEAD/%s"
MAX_SECTION = 4000        # characters of one indexed section; longer ones are split
MAX_FILE = 2 * 1024 * 1024
RETRY_S = 300            # a source that could not be read: tried again this soon

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
]


def ensure(conn):
    with db.transaction(conn):
        for sql in SCHEMA:
            conn.execute(sql)


def parse_source(spec):
    """(owner, repo, path) of "owner/repo/path"; ValueError otherwise."""
    parts = spec.strip().strip("/").split("/", 2)
    if len(parts) != 3 or not all(parts) or ".." in parts[2].split("/"):
        raise ValueError("a docs source is owner/repo/path: %r" % spec)
    return tuple(parts)


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

def _fetch(session, url, etag):
    """(status, text, etag) of a conditional GET, at most MAX_FILE bytes read."""
    headers = {"If-None-Match": etag} if etag else {}
    with session.get(url, headers=headers, timeout=(5, 20), stream=True) as r:
        if r.status_code != 200:
            return r.status_code, "", etag
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_FILE:
                raise ValueError("%s is larger than %d bytes" % (url, MAX_FILE))
        return 200, body.decode("utf-8", "replace"), r.headers.get("ETag", "")


def _replace(conn, source, url, text, etag, sha, now):
    name = source.split("/", 2)[2].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    repo = source.split("/")[1]
    name = name if repo == "bits" else "%s %s" % (repo, name)
    secs = sections(text, name, url)
    with db.transaction(conn):
        conn.execute("DELETE FROM chat_sections WHERE source = ?", (source,))
        conn.executemany("INSERT INTO chat_sections (source, url, title, body) VALUES (?, ?, ?, ?)",
                         [(source, u, t, b) for t, u, b in secs])
        conn.execute("INSERT INTO chat_sources (source, etag, sha256, sections, fetched_ms, changed_ms)"
                     " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET"
                     " etag = excluded.etag, sha256 = excluded.sha256, sections = excluded.sections,"
                     " fetched_ms = excluded.fetched_ms, changed_ms = excluded.changed_ms",
                     (source, etag, sha, len(secs), now, now))
    return len(secs)


def refresh(conn, sources, session=None, every_s=3600, now=None):
    """When due, fetch every source again if it changed (ETag) and index the ones
    whose text changed; returns how many. Sources no longer listed leave the index.
    One that cannot be read keeps its sections, is tried again in RETRY_S, and the
    step reports it."""
    import requests
    from . import reconcile
    ensure(conn)
    fixed_now = now
    now = now or db.now_ms()
    if now < int(reconcile._kv_get(conn, "chat.docs.next_ms", "0") or 0):
        return 0
    own = session is None
    session = session or requests.Session()
    listed = set(sources)
    with db.transaction(conn):
        for (gone,) in conn.execute("SELECT source FROM chat_sources").fetchall():
            if gone not in listed:
                conn.execute("DELETE FROM chat_sections WHERE source = ?", (gone,))
                conn.execute("DELETE FROM chat_sources WHERE source = ?", (gone,))
    changed, errors = 0, []
    try:
        changed = _read_all(conn, sources, session, now, errors)
    finally:
        if own:
            session.close()
    end = fixed_now or db.now_ms()     # from the end of the pass: a slow one is not again at once
    with db.transaction(conn):
        reconcile._kv_set(conn, "chat.docs.next_ms",
                          str(end + 1000 * (min(RETRY_S, every_s) if errors else every_s)))
    if errors:
        raise RuntimeError("docs not read: " + "; ".join(errors))
    return changed


def _read_all(conn, sources, session, now, errors):
    """Fetch and index *sources*; returns how many changed, appends to *errors*."""
    import requests
    changed = 0
    for source in sources:
        row = conn.execute("SELECT * FROM chat_sources WHERE source = ?", (source,)).fetchone()
        owner, repo, path = parse_source(source)
        try:
            status_, text, etag = _fetch(session, RAW % (owner, repo, path),
                                         row["etag"] if row else "")
        except (requests.ConnectionError, requests.Timeout) as e:
            errors.append("GitHub cannot be reached (%s); the rest next time" % type(e).__name__)
            break                      # one wait, not one per source
        except Exception as e:
            errors.append("%s: %s" % (source, e))
            continue
        if status_ == 304 and row:
            with db.transaction(conn):
                conn.execute("UPDATE chat_sources SET fetched_ms = ? WHERE source = ?", (now, source))
            continue
        if status_ != 200:
            errors.append("%s: HTTP %s" % (source, status_))
            continue
        sha = hashlib.sha256(text.encode()).hexdigest()
        if row and row["sha256"] == sha:
            with db.transaction(conn):
                conn.execute("UPDATE chat_sources SET fetched_ms = ?, etag = ? WHERE source = ?",
                             (now, etag, source))
            continue
        _replace(conn, source, WEB % (owner, repo, path), text, etag, sha, now)
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


def search(conn, text, limit=6, budget=24000):
    """[{title, url, body}] best first, at most *limit* and about *budget* characters."""
    q = query(text)
    if not q:
        return []
    try:
        rows = conn.execute("SELECT title, url, body FROM chat_sections WHERE chat_sections MATCH ?"
                            " ORDER BY bm25(chat_sections, 0.0, 0.0, 4.0, 1.0) LIMIT ?",
                            (q, limit * 3)).fetchall()
    except Exception as e:                 # no index yet, or no FTS5
        log.warning("docs search: %s", e)
        return []
    out, used = [], 0
    for r in rows:
        if len(out) >= limit or (out and used + len(r["body"]) > budget):
            break
        if any(o["url"] == r["url"] and o["body"] == r["body"] for o in out):
            continue
        out.append({"title": r["title"], "url": r["url"], "body": r["body"]})
        used += len(r["body"])
    return out


def status(conn):
    """{sources, sections, changed_ms} of the index; zeros without one."""
    try:
        r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(sections), 0) AS s,"
                         " COALESCE(MAX(changed_ms), 0) AS c FROM chat_sources").fetchone()
    except Exception:
        return {"sources": 0, "sections": 0, "changed_ms": 0}
    return {"sources": r["n"], "sections": r["s"], "changed_ms": r["c"]}
