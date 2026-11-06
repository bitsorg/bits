# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's Hosts page (ADR bits-services 0001, H): where the site's services run and
how they are reached, from the host helper's `hosts` answer. Read-only. Each host is a
card that opens to its services, ports, networks, and the connections to allow."""

import math
import time
from html import escape

LOOPBACK = {"localhost", "127.0.0.1", "::1"}  # a container's own, not the host's

GOOD = {"healthy", "running"}
CSS = ("body{max-width:72rem!important}.card.wide{grid-column:1/-1}.hosts{display:grid;grid-template-columns:repeat(auto-fill,minmax(20rem,1fr));gap:.8rem}"
       ".card{border:1px solid #d1d5db;border-left:6px solid #9ca3af;border-radius:6px;padding:.5rem .8rem}"
       ".card.ok{border-left-color:#16a34a}.card.bad{border-left-color:#dc2626}"
       ".card summary{cursor:pointer;font-weight:600}.card table{width:100%;border-collapse:collapse;"
       "font-size:13px}.card td,.card th{text-align:left;padding:.15rem .3rem;vertical-align:top}"
       ".ok-s{color:#16a34a}.bad-s{color:#dc2626}.off-s{color:#6b7280}.tag{font-size:12px;color:#6b7280}")


def _loopback(host):
    """Loopback, or a *.localhost name: e.g. bits-admin's own address at the tunnel's laptop end."""
    return host in LOOPBACK or host.endswith(".localhost")


def _s(value):
    """Escaped text of any value; nothing for None."""
    return "" if value is None else escape(str(value))


def _state_class(state):
    if state in GOOD:
        return "ok-s"
    return "off-s" if state == "not running" else "bad-s"


def _exposure(port):
    """published (all interfaces), localhost, or the address it is bound to."""
    ip = str(port.get("host_ip") or "")
    if ip in ("", "0.0.0.0", "::"):
        return "published"
    return "localhost" if ip in ("127.0.0.1", "::1") else "on " + escape(ip)


def _card(title, rows, connects, reached, state_class="", note=""):
    def table(items, head):
        if not items:
            return ""
        return "<p><b>%s</b></p><table>%s</table>" % (head, "".join(
            "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in item) for item in items))
    body = rows + table(connects, "Connects to") + table(reached, "Reached by (inbound to allow)")
    return ("<details class=\"card %s\"%s><summary>%s</summary>%s%s</details>"
            % (state_class, " open" if "wide" in state_class else "", title, "<p class=tag>%s</p>" % note if note else "", body))


def _health(up, scrape):
    """A lookup (job, host, port) -> (up, last_up) from VictoriaMetrics' targets; the
    instance is host:port or, for a job of one target in scrape.yml, its only one (relabelled)."""
    targets = [t for t in (up or {}).get("targets") or [] if isinstance(t, dict)]
    in_scrape = {}
    for s in scrape:
        in_scrape[str(s.get("job"))] = in_scrape.get(str(s.get("job")), 0) + 1
    by_instance = {(str(t.get("job")), str(t.get("instance")).lower()): t for t in targets}
    by_job = {}
    for t in targets:
        by_job.setdefault(str(t.get("job")), []).append(t)

    def look(job, host, port):
        host = "[%s]" % host if ":" in str(host) else host  # IPv6, as monitoring writes it
        t = by_instance.get((str(job), "%s:%s" % (host, port)))
        if t is None and in_scrape.get(str(job)) == 1 and len(by_job.get(str(job), [])) == 1:
            t = by_job[str(job)][0]
        return None if t is None else (t.get("up") == 1, t.get("last_up"))
    return look


def _said(health):
    """' · up', ' · down (last answered …)' or nothing."""
    if health is None:
        return ""
    if health[0]:
        return " &middot; <span class=ok-s>up</span>"
    last = health[1]
    when = ("last answered %s UTC" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(last))
            if isinstance(last, (int, float)) and math.isfinite(last) else "not in the past day")
    return " &middot; <span class=bad-s>down</span> (%s)" % when


def _checks_html(checks, at):
    rows = "".join("<tr><td>%s</td><td>%s</td><td class=%s>%s</td><td>%s</td></tr>" % (
        _s(c.get("target")), _s(c.get("probe")), "ok-s" if c.get("ok") is True else "bad-s",
        _s(c.get("said")), "" if c.get("ms") is None else "%s ms" % _s(c.get("ms")))
        for c in checks if isinstance(c, dict))
    ok = isinstance(at, (int, float)) and math.isfinite(at)
    when = time.strftime("%H:%M:%S UTC", time.gmtime(at)) if ok else "?"
    return ("<h2>Check, %s</h2><p class=tag>Results are kept 30 s: a second click within that shows the same."
            "</p><table><tr><th>Target<th>Probe<th>Result<th>Time</tr>%s</table>"
            % (when, rows or "<tr><td colspan=4>nothing to check</td></tr>"))


def render(answer, status, checks=None, at=None):
    """The page body for the helper's *answer* (hosts), *status* (signer, health) and,
    after a Check, its results."""
    checked = {str(c.get("target")).lower(): c for c in checks or [] if isinstance(c, dict)}

    def said(host, port):
        c = checked.get(("%s:%s" % (host, port)).lower())
        if c is None:
            return ""
        return " &middot; check: <span class=%s>%s</span>" % ("ok-s" if c.get("ok") is True else "bad-s",
                                                              _s(c.get("said")))
    host = str(answer.get("host") or "this host")
    me = escape(host)
    here = {host.lower(), "host.docker.internal"}  # this host itself, outside Docker
    services = answer.get("services") or {}
    networks = answer.get("networks") or {}
    links = [l for l in answer.get("links") or [] if isinstance(l, dict) and l.get("host")]
    scrape = [t for t in answer.get("scrape") or [] if isinstance(t, dict) and t.get("host")]
    up = answer.get("up") or {}
    look = _health(up, scrape)

    # This host: its services, their ports and networks
    rows, inbound, bad = [], [], False
    for name, s in sorted(services.items()):
        state = str(s.get("state") or "not running")
        bad |= state not in GOOD and state != "not running"
        ports = ", ".join("%s&rarr;%s %s" % (_s(p.get("published") or "-"), _s(p.get("target")), _exposure(p))
                          for p in s.get("ports") or [] if isinstance(p, dict))
        nets = ", ".join("%s%s" % (escape(n), " (internal)" if networks.get(n) else "")
                         for n in s.get("networks") or []) or escape(s.get("network_mode") or "")
        extra = ""
        signer = status.get("signer")
        if name == "security-proxy" and signer:
            extra = " &middot; " + {"loaded": "key loaded", "no key": "<b>no key</b>: unlock on the main page"}.get(
                signer, "<b>signer %s</b>" % _s(signer))
        rows.append("<tr><td>%s%s</td><td class=%s>%s%s</td><td>%s</td><td>%s</td></tr>" % (
            escape(name), " <span class=tag>(profile %s)</span>" % escape(", ".join(s["profiles"]))
            if s.get("profiles") else "", _state_class(state), escape(state), extra, ports or "-", nets))
        for p in s.get("ports") or []:  # all but loopback: what the network team opens
            if isinstance(p, dict) and p.get("published") and _exposure(p) != "localhost":
                where = "" if _exposure(p) == "published" else " (%s)" % _exposure(p)
                inbound.append(("%s/%s%s" % (_s(p["published"]), _s(p.get("protocol") or "tcp"), where),
                                "clients of " + escape(name)))
    inbound.insert(0, ("22/tcp", "admins: SSH, and the tunnel to bits-admin"))
    internal, on_host, out = set(), set(), {}  # out: host -> {(port, who, said)}
    seen, scraped = {}, set()  # host -> [up or down, per scraped target]; hosts in scrape.yml
    for l in links:
        h, who = str(l["host"]).lower(), "%s (%s)" % (l.get("service", "?"), l.get("key", "?"))
        if h in services:
            internal.add((_s(l.get("service", "?")), "%s:%s" % (_s(h), _s(l.get("port")))))
        elif h in here:
            on_host.add((_s(l.get("port")), escape(who)))
        elif not _loopback(h):
            out.setdefault(h, set()).add((str(l.get("port")), who, ""))
    for t in scrape:
        h, who = str(t["host"]).lower(), "vmagent (job %s)" % t.get("job", "?")
        health = look(t.get("job"), h, t.get("port"))
        if h in here:
            on_host.add((_s(t.get("port")), "monitoring job " + _s(t.get("job", "?")) + _said(health)))
        elif h not in services and not _loopback(h):
            out.setdefault(h, set()).add((str(t.get("port")), who, _said(health)))
            scraped.add(h)
            if health is not None:
                seen.setdefault(h, []).append(health[0])
    internal, on_host = sorted(internal), sorted(on_host)
    table = ("<table><tr><th>Service<th>State<th>Ports (host&rarr;container)<th>Networks</tr>%s</table>"
             % "".join(rows))
    if internal:
        table += "<p><b>Between its services</b></p><table>%s</table>" % "".join(
            "<tr><td>%s</td><td>&rarr; %s</td></tr>" % pair for pair in internal)
    if on_host:
        table += "<p><b>Also on this host, outside Docker</b></p><table>%s</table>" % "".join(
            "<tr><td>%s</td><td>%s</td></tr>" % pair for pair in on_host)
    connects = [("%s:%s" % (escape(h), escape(port)), escape(who) + health + said(h, port))
                for h in sorted(out) for port, who, health in sorted(out[h])]
    cards = [_card("Admins' computers", "<p>They reach this host over SSH; bits-admin through the tunnel "
                   "to its socket.</p>", [("%s:22" % me, "ssh")], [], "ok"),
             _card("%s <span class=tag>bits services host</span>" % me, table, connects, inbound,
                   "wide bad" if bad else "wide ok", note="%d services" % len(services))]
    for h in sorted(out):
        reached = [("%s/tcp" % escape(port), escape(who) + health + said(h, port))
                   for port, who, health in sorted(out[h])]
        health = "" if h not in seen else "bad" if not all(seen[h]) else "ok"
        cards.append(_card(escape(h), "", [], reached, health,
                           note="reached from %s%s" % (me, "" if h in seen else "; no data from monitoring"
                                                       if h in scraped else "; not monitored")))
    if up.get("error"):
        monitoring = "<p class=tag>Health of other hosts: %s</p>" % _s(up["error"])
    elif out and "targets" in up and not up["targets"]:
        monitoring = "<p class=bad-s>Health of other hosts: monitoring has no targets now (is vmagent running?)</p>"
    else:
        monitoring = "<p class=tag>Health of other hosts: VictoriaMetrics' up, as monitoring last scraped it.</p>"
    return ("<style>%s</style><p>Where the site's services run and how they are reached. Open a card "
            "for its services and the connections to allow. Refreshed every minute.</p>%s%s"
            "<div class=hosts>%s</div>" % (CSS, monitoring, "" if checks is None else _checks_html(checks, at),
                                           "".join(cards)))
