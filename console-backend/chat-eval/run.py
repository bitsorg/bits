#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Evaluate the help chat on cases.yaml: whether the docs search finds each case's
sources and, with --model, the model's answers (judged by the model itself with
--judge, else for a person to read in the report).

    python chat-eval/run.py                                  # the search, docs as the service has them
    python chat-eval/run.py --local ~/src                    # docs from checkouts in ~/src/<repo or project>
    python chat-eval/run.py --model gemma4:12b --judge --out report.md
    python chat-eval/run.py --model qwen3.5:9b --judge --judge-model gemma4:12b --sample 30
"""

import argparse
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time
import types

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from console_backend.orch import chat, chat_docs, db  # noqa: E402

JUDGE = ("Here is an answer to a user's question.\n\nAnswer:\n<<<\n%s\n>>>\n\n"
         "Does the answer state or show the following? It counts when said in other words, or "
         "shown in a command, example or configuration of the answer. Reply with only yes or no."
         "\n\n%s")


def _local_root(local, src):
    """The checkout in LOCAL of a source's repository (bits: this one)."""
    m = re.match(r"https://[^/]+/(?:[^/]+/)*?([^/]+?)(?:/-/raw/.*)?$", src)
    repo = m.group(1) if m else src.split("/")[1]
    return os.path.dirname(os.path.dirname(HERE)) if repo == "bits" else os.path.join(local, repo)


def _local_files(local, src):
    """[(file source, its path in the checkout)] of a source, from a checkout in LOCAL."""
    root = _local_root(local, src)
    if not chat_docs.is_repo(src):
        path = (re.match(r".*/-/raw/[^/]+/(.+)", src).group(1) if src.startswith("https://")
                else chat_docs.parse_source(src)[2])
        return [(src, os.path.join(root, path))]
    try:
        paths = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True,
                               check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        paths = [os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs]
    prefix = "%s/-/raw/main/" % src if src.startswith("https://") else src + "/"
    return [(prefix + p, os.path.join(root, p)) for p, _ in chat_docs._markdown([(p, "") for p in paths])]


def _copy(conn, path):
    """The docs index of the orchestrator's database at *path*, read only."""
    live = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = live.execute("SELECT source, url, title, body FROM chat_sections").fetchall()
        try:
            vecs = live.execute("SELECT digest, vec FROM chat_vectors").fetchall()
        except sqlite3.OperationalError:   # an index from before the vectors
            vecs = []
    finally:
        live.close()
    conn.executemany("INSERT INTO chat_sections (source, url, title, body) VALUES (?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO chat_vectors (digest, vec) VALUES (?, ?)", vecs)


def index(local, faq, live=""):
    conn = db.connect(":memory:")          # the orchestrator's schema: refresh keeps state in kv
    db.migrate(conn)
    chat_docs.ensure(conn)
    sources = chat_docs.sources_of(_Settings())
    if live:
        _copy(conn, live)
    elif local:
        for src in sources:
            for fsrc, path in _local_files(local, src):
                try:
                    chat_docs._replace(conn, fsrc, open(path, encoding="utf-8").read(), "", "", "", 0)
                except OSError as e:
                    print("skipped %s: %s" % (fsrc, e), file=sys.stderr)
    else:
        try:
            chat_docs.refresh(conn, sources, token=os.environ.get("BITS_GITHUB_TOKEN", ""))
        except RuntimeError as e:          # a part missing would score as wrong answers
            sys.exit("%s: nothing evaluated (--db or --local reads them elsewhere)" % e)
    if not faq:                            # its entries are these cases: it would answer them
        conn.execute("DELETE FROM chat_sections WHERE source LIKE '%/docs/FAQ.md'")
    return conn


def _key(url):
    """'docs/USERGUIDE.md#platforms' of a section's link (bits), else '<repo>/README.md#…'."""
    m = re.match(r"https://[^/]+/(?:[^/]+/)*?([^/]+)/-/blob/[^/]+/(.+)", url)
    if m:                                  # GitLab
        return "%s/%s" % m.groups()
    rest = url.split("github.com/", 1)[-1]
    owner, repo, _blob, _ref, path = rest.split("/", 4)
    return path if repo == "bits" else "%s/%s" % (repo, path)


def found(expected, docs, titles):
    """Whether *docs* has an *expected* section (a key), one of its subsections, or a
    short section that brought it along; *titles*: {key: title} of the index."""
    got = {_key(u) for d in docs for u in [d["url"]] + d.get("also", [])}
    heads = [re.sub(r" \(\d+\)$", "", d["title"]) for d in docs]
    return any(s in got or (s in titles and any(h.startswith(titles[s] + " › ") for h in heads))
               for s in expected)


def sample(cases, n):
    """*n* of *cases*, in their order: those whose names hash lowest, so the same each
    run, and adding or removing a case changes at most one of them."""
    if not n or n >= len(cases):
        return cases
    keep = set(sorted((c["id"] for c in cases),
                      key=lambda i: hashlib.sha256(i.encode()).hexdigest())[:n])
    return [c for c in cases if c["id"] in keep]


class _Settings:
    chat_docs = [d.strip() for d in os.environ.get("BITS_CHAT_DOCS", "").split(",") if d.strip()]


def record(build):
    lines = ["community: %s · package: %s · platform: %s" % (
        build.get("community"), build.get("package"), build.get("platform")),
        "outcome: failed · stopped in: %s" % build.get("failed_in", "")]
    if build.get("failure_reason"):
        lines.append("failed jobs: bits-prepub-build (%s)" % build["failure_reason"])
    lines += build.get("changed") or []          # as what_changed() gives them
    lines.append("log excerpt (redacted):\n" + chat._clean(build.get("excerpt"), chat.MAX_EXCERPT))
    return chat._clean("\n".join(lines), 4 * chat.MAX_EXCERPT)


def judged(a, answer, fact):
    reply = ask(a.url, a.judge_model or a.model, [{"role": "user", "content": JUDGE % (answer, fact)}],
                a.context, 8)
    return reply.strip().lstrip("*\"' ").lower().startswith("yes")


def ask(url, model, msgs, ctx, tokens, think=False):
    r = requests.post(url + "/api/chat", timeout=300, json={
        "model": model, "messages": msgs, "stream": False, "think": think,
        "options": {"num_ctx": ctx, "num_predict": tokens, "temperature": 0.2}})
    r.raise_for_status()
    return r.json()["message"]["content"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", default=os.path.join(HERE, "cases.yaml"))
    ap.add_argument("--local", help="read the docs from checkouts in LOCAL/<repo> (bits: this one)")
    ap.add_argument("--embed-model", default=os.environ.get("BITS_CHAT_EMBED_MODEL", ""),
                    help="search by meaning too, as the chat does with BITS_CHAT_EMBED_MODEL (the default)")
    ap.add_argument("--db", default=os.environ.get("BITS_ORCH_DB", ""),
                    help="the orchestrator's database, whose docs index is used (default $BITS_ORCH_DB);"
                         " without one, or with --local, the docs are read from GitHub or LOCAL")
    ap.add_argument("--url", default=os.environ.get("BITS_CHAT_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--model", help="also ask this model each case")
    ap.add_argument("--judge", action="store_true", help="the model checks each must / must_not")
    ap.add_argument("--judge-model", help="this model checks them instead")
    ap.add_argument("--faq", action="store_true", help="index docs/FAQ.md too (made from these cases)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--sample", type=int, help="this many cases, the same each run (adding a case changes one at most)")
    ap.add_argument("--context", type=int, default=16384, help="as BITS_CHAT_CONTEXT")
    ap.add_argument("--answer-tokens", type=int, default=1500, help="as BITS_CHAT_ANSWER_TOKENS")
    ap.add_argument("--think", action="store_true", help="let a thinking model think first")
    ap.add_argument("--out", help="write a markdown report here")
    a = ap.parse_args()

    cases = yaml.safe_load(open(a.cases))
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    cases = sample(cases, a.sample)
    live = a.db if a.db and not a.local and os.path.exists(a.db) else ""
    conn = index(a.local, a.faq, live)
    n = conn.execute("SELECT COUNT(*) FROM chat_sections").fetchone()[0]
    if not n:                              # every answer would be "not covered": say so instead
        sys.exit("no docs indexed%s: nothing to evaluate" % (" in " + live if live else ""))
    print("%d sections, from %s" % (n, live or a.local or "GitHub"), file=sys.stderr)
    embedder = types.SimpleNamespace(chat_url=a.url, chat_embed_model=a.embed_model)
    budget = chat.budgets(types.SimpleNamespace(chat_context=a.context, chat_answer_tokens=a.answer_tokens),
                          a.think)[0]
    if a.embed_model:                      # vectors the service has, else made here
        try:
            made = chat_docs.embed_sections(conn, chat.vector_key(embedder), lambda s: chat.embed_docs(embedder, s),
                                        10 ** 9)
        except (chat.ChatError, ValueError) as e:
            sys.exit("%s: %s" % (a.embed_model, e))
        print("%s: %d vectors made here" % (a.embed_model, made), file=sys.stderr)
    titles = {_key(r["url"]): re.sub(r" \(\d+\)$", "", r["title"])
              for r in conn.execute("SELECT url, title FROM chat_sections")}
    hits = word_hits = total = facts = facts_ok = wrong = unembedded = 0
    report = ["# Help chat evaluation", "",
              "model: %s · %s" % (a.model or "(search only)", time.strftime("%Y-%m-%d %H:%M")), ""]
    for c in cases:
        rec = record(c["build"]) if c.get("build") else None
        words = c["question"] + " " + (chat._error_lines(c["build"].get("excerpt")) if rec else "")
        q = chat.near(embedder, words)
        unembedded += bool(a.embed_model and not q)
        docs = chat_docs.search(conn, words, chat.SECTIONS_BY_MEANING if q else chat.SECTIONS, budget, near=q)
        got = [_key(d["url"]) for d in docs]
        hit = None
        if c.get("sources"):
            total += 1
            hit = found(c["sources"], docs, titles)
            hits += hit
            word_hits += found(c["sources"], chat_docs.search(conn, words, chat.SECTIONS, budget),
                               titles) if q else hit
        report += ["## %s" % c["id"], "", "**Q:** " + c["question"], "",
                   "search: %s" % ("found" if hit else "missed" if hit is False else "n/a")
                   + ("" if hit or not c.get("sources") else " (expected %s; got %s)"
                      % (", ".join(c["sources"]), ", ".join(got[:3]) or "nothing")), ""]
        if a.model:
            t0 = time.monotonic()
            answer = ask(a.url, a.model, chat.messages(c["question"], docs, rec), a.context,
                         a.answer_tokens, a.think)
            report += ["**A** (%.0f s): %s" % (time.monotonic() - t0, answer), ""]
            for kind, items in (("must", c.get("must") or []), ("must_not", c.get("must_not") or [])):
                for fact in items:
                    mark = "?"
                    if a.judge:
                        said = judged(a, answer, fact)
                        ok = said if kind == "must" else not said
                        mark = "✓" if ok else "✗"
                        if kind == "must":
                            facts += 1
                            facts_ok += ok
                        else:
                            wrong += not ok
                    report.append("- %s %s: %s" % (mark, kind, fact))
            report.append("")
        print("%-50s %s" % (c["id"][:50], {True: "found", False: "MISSED", None: "-"}[hit]),
              file=sys.stderr)
    summary = "search found the expected section for %d of %d cases (%.0f%%)" % (
        hits, total, 100.0 * hits / max(total, 1))
    if a.embed_model:
        summary += " (by words only, %d sections: %d%s)" % (
            chat.SECTIONS, word_hits, "; %d questions not embedded" % unembedded if unembedded else "")
    if a.judge:
        summary += "; answers stated %d of %d facts, and %d wrong claims" % (facts_ok, facts, wrong)
    print(summary)
    if a.out:
        report.insert(3, summary + "\n")
        open(a.out, "w").write("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
