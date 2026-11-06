# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's Settings page (ADR bits-services 0001, C): every setting of the site in
one place, from the host helper's `settings` answer; secrets only as set or missing.
Edited here, planned and applied by the helper; the profile downloads as site-profile.yaml."""

import time
from html import escape

import yaml

FACTS = ("gitlab_url", "frontend_origin", "backend_url", "console_project", "admins_group")  # site_profile's
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


def _editable(s):
    return not s.get("secret") and s.get("origin") != "withheld"


def _hint(s):
    if s.get("derived") is not None:
        return "the facts imply " + str(s["derived"])
    if s.get("default") not in (None, ""):
        return "default " + str(s["default"])
    return ""


def _input(s):
    current = s.get("value") if s.get("value") is not None else ""
    shown = "<input name=\"set:%s\" value=\"%s\" placeholder=\"%s\" size=38>" % (
        _s(s["key"]), _s(current), _s(_hint(s)))
    if s.get("origin") == "overridden" and s.get("derived") is not None:
        shown += "<br><span class=doc>the facts imply <code>%s</code>; empty follows them</span>" % _s(s["derived"])
    return shown


def changes_from(form, answer):
    """The changes a submitted Settings form asks for, against the helper's answer: facts
    that differ (an emptied fact is kept), settings set to a new value, emptied ones unset
    or, if derived, following the facts again."""
    facts, sets, unsets, follows = {}, {}, [], []
    known = answer.get("facts") or {}
    for name in FACTS:
        value = str(form.get("fact:" + name, known.get(name, ""))).strip()
        if value and value != known.get(name, ""):
            facts[name] = value
    for s in answer.get("settings") or []:
        if not isinstance(s, dict) or not _editable(s) or "set:" + s.get("key", "") not in form:
            continue
        key, value = s["key"], str(form["set:" + s["key"]]).strip()
        current = s.get("value") if s.get("value") is not None else ""
        if value == current:
            continue
        if value:
            sets[key] = value
        elif s.get("derived") is not None:
            follows.append(key)  # the helper derives it from the new facts
        else:
            unsets.append(key)
    return {"facts": facts, "set": sets, "unset": unsets, "follow": follows}


def plan_html(planned, plan_id, profile=False):
    rows = "".join("<tr><td class=k>%s</td><td><code>%s</code></td><td><code>%s</code></td></tr>" % (
        _s(d.get("key")), "<i>unset</i>" if d.get("old") is None else _s(d.get("old")),
        "<i>unset</i>" if d.get("new") is None else _s(d.get("new")))
        for d in planned.get("diff") or [] if isinstance(d, dict))
    notes = "".join("<li>%s</li>" % _s(n) for n in planned.get("notes") or [])
    if not rows and not profile:
        return "<p>Nothing would change.</p><p><a href=/settings>Back</a></p>"
    source = ("<p>From the site-profile.yaml you gave: the settings it does not name go, and it is saved as "
              "<code>site-profile.yaml</code> (the old one kept as a dated copy).</p>" if profile else "")
    return ("<style>%s</style><h2>Plan</h2>%s<p>This is what <code>.env</code> would become. It is written "
            "afresh: its comments and the order of its lines are not kept, but the old file is, as a dated "
            "<code>.env.bak-&hellip;</code>. Apply needs a touch of your key.</p>%s<table class=set><tr><th>Setting"
            "<th>Now<th>Then</tr>%s</table><p><button id=apply data-plan=\"%s\">Apply with your key</button> "
            "<a href=/settings>Back</a></p><p id=status></p><script src=/admin.js></script>"
            % (CSS, source, "<ul>%s</ul>" % notes if notes else "",
               rows or "<tr><td colspan=3>.env would not change</td></tr>", _s(plan_id)))


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
           "Secrets show only as set or missing. Change values below, then Plan. "
           "<a href=/settings/site-profile.yaml>Download site-profile.yaml</a> (no secrets).</p>"
           "<form method=post action=/settings/plan><input type=hidden name=base value=\"%s\">"
           % (CSS, _s(answer.get("base"))),
           "<h2>Facts</h2><p class=doc>What the derived settings follow; one left empty keeps its value.</p><table class=set>%s</table>" % "".join(
               "<tr><td class=k>%s</td><td><input name=\"fact:%s\" value=\"%s\" size=38></td></tr>"
               % (_s(k), _s(k), _s(facts.get(k, ""))) for k in FACTS)]
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
            % (_s(s["key"]), _input(s) if _editable(s) else _value(s), _s(s.get("origin")),
               _s(ORIGINS.get(s.get("origin"), s.get("origin"))),
               _s(", ".join(s.get("services") or [])), _doc(s.get("doc")))
            for s in sorted(groups[name], key=lambda s: s["key"]))
        out.append("<details open><summary><b>%s</b> (%d)</summary><table class=set><tr><th>Setting<th>Value"
                   "<th>Where from<th>Used by<th>About</tr>%s</table></details>" % (_s(name), len(groups[name]), rows))
    out.append("<p><button>Plan</button> (nothing changes before Apply)</p></form>")
    out.append("<h2>Load a site-profile.yaml</h2><form method=post action=/settings/plan>"
               "<input type=hidden name=base value=\"%s\"><p class=doc>The .env it implies replaces this one; "
               "secrets stay as they are. Plan shows the change first.</p><p><input type=file id=profile-file "
               "accept=\".yaml,.yml\"></p><textarea name=profile id=profile rows=12 cols=80 "
               "placeholder=\"or paste it here\"></textarea><p><button>Plan</button></p></form>"
               "<script src=/admin.js></script>" % _s(answer.get("base")))
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
