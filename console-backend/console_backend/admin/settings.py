# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's Settings page (ADR bits-services 0001, C): every setting of the site in
one place, from the host helper's `settings` answer; secrets only as set or missing.
Read-only; the site profile it implies downloads as site-profile.yaml (no secrets)."""

import time
from html import escape

import yaml

ORIGINS = {"derived": "from the facts", "overridden": "set, not as the facts imply",
           "set": "set in .env", "default": "default", "unset": "not set",
           "withheld": "set; withheld: it looks like it holds a credential"}
CSS = ("body{max-width:72rem!important}table.set{width:100%;border-collapse:collapse;font-size:13px}"
       "table.set td,table.set th{text-align:left;padding:.25rem .4rem;border-bottom:1px solid #e5e7eb;"
       "vertical-align:top}table.set td.k{font-family:ui-monospace,monospace;white-space:nowrap}"
       ".o-derived,.o-set{color:#16a34a}.o-overridden,.o-withheld{color:#b45309}.o-default,.o-unset{color:#6b7280}"
       ".doc{color:#4b5563}.sec{color:#7c3aed}")


def _s(value):
    return "" if value is None else escape(str(value))


def _group(s):
    if s.get("section"):
        return s["section"]
    if s.get("services"):
        return "used by " + s["services"][0]
    return "other"


def _doc(text, short=140):
    """Long notes open on click: the first words, then the rest."""
    text = str(text or "")
    if len(text) <= short:
        return _s(text)
    cut = text.rfind(" ", 0, short)
    cut = cut if cut > 0 else short
    return "<details><summary>%s&hellip;</summary>%s</details>" % (_s(text[:cut]), _s(text[cut:]))


def _value(s):
    if s.get("secret"):
        return "<span class=sec>secret: %s</span>" % ("set" if s.get("set") else "missing")
    if s.get("origin") == "withheld":
        return "<span class=sec>withheld</span>"
    if s.get("value") is not None:
        shown = "<code>%s</code>" % _s(s["value"])
        if s.get("origin") == "overridden" and s.get("derived") is not None:
            shown += "<br><span class=doc>the facts imply <code>%s</code></span>" % _s(s["derived"])
        return shown
    if s.get("derived") is not None:
        return "<span class=doc>the facts imply <code>%s</code></span>" % _s(s["derived"])
    if s.get("default") not in (None, ""):
        return "<span class=doc>default <code>%s</code></span>" % _s(s["default"])
    return ""


def render(answer):
    settings = [s for s in answer.get("settings") or [] if isinstance(s, dict) and s.get("key")]
    groups = {}
    for s in settings:
        groups.setdefault(_group(s), []).append(s)
    facts = answer.get("facts") or {}
    out = ["<style>%s</style><p>Every setting of this site in one place: what docker-compose.yml reads, "
           "its value in <code>.env</code> or its default, and what <code>.env.example</code> says of it. "
           "Secrets show only as set or missing. Read-only for now. "
           "<a href=/settings/site-profile.yaml>Download site-profile.yaml</a> (no secrets).</p>" % CSS,
           "<h2>Facts</h2><table class=set>%s</table>" % ("".join(
               "<tr><td class=k>%s</td><td><code>%s</code></td></tr>" % (_s(k), _s(v))
               for k, v in sorted(facts.items())) or "<tr><td>none found in .env</td></tr>")]
    pf = answer.get("profile_file")
    if isinstance(pf, dict):
        if pf.get("error"):
            out.append("<p class=o-overridden>%s</p>" % _s(pf["error"]))
        else:
            lines = [_s(d) for d in pf.get("diffs") or []] + [_s(n) for n in pf.get("notes") or []]
            out.append("<h2>site-profile.yaml and .env</h2><ul>%s</ul>" % (
                "".join("<li>%s</li>" % line for line in lines) or "<li>they agree</li>"))
    counts = {}
    for s in settings:
        counts[s.get("origin")] = counts.get(s.get("origin"), 0) + 1
    out.append("<p class=doc>%d settings: %s.</p>" % (len(settings), ", ".join(
        "%d %s" % (n, ORIGINS.get(o, _s(o))) for o, n in sorted(counts.items(), key=lambda kv: -kv[1]))))
    for name in sorted(groups, key=lambda g: (g.startswith("used by") or g == "other", g)):
        rows = "".join(
            "<tr><td class=k>%s</td><td>%s</td><td class=\"o-%s\">%s</td><td>%s</td><td class=doc>%s</td></tr>"
            % (_s(s["key"]), _value(s), _s(s.get("origin")), _s(ORIGINS.get(s.get("origin"), s.get("origin"))),
               _s(", ".join(s.get("services") or [])), _doc(s.get("doc")))
            for s in sorted(groups[name], key=lambda s: s["key"]))
        out.append("<details open><summary><b>%s</b> (%d)</summary><table class=set><tr><th>Setting<th>Value"
                   "<th>Where from<th>Used by<th>About</tr>%s</table></details>" % (_s(name), len(groups[name]), rows))
    return "".join(out)


def profile_yaml(answer, now=time.time):
    """site-profile.yaml for this site, from the helper's answer: facts, overrides and the
    other keys; secrets never (the helper leaves them out)."""
    head = ("# site-profile.yaml of this bits-services site, from its .env on %s by bits-admin.\n"
            "# No secrets: they belong to service secrets. tools/site_profile.py derive turns it\n"
            "# into the .env it implies.\n" % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now())))
    withheld = [str(k) for k in answer.get("withheld") or []]
    if withheld:
        head += ("# Left out, as they look like they hold a credential; set them by hand:\n"
                 "#   %s\n" % " ".join(withheld))
    profile = answer.get("profile") or {}
    return head + yaml.safe_dump({k: profile.get(k) or {} for k in ("facts", "overrides", "env")},
                                 sort_keys=True, default_flow_style=False, allow_unicode=True)
