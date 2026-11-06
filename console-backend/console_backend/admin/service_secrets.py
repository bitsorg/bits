# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's Secrets page: the service secrets (bits-services tools/service-secrets.py)
by slot name only; set, replaced or removed through the host helper, one touch each."""

import re
from html import escape

SLOT = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")  # as the host helper checks it (fullmatch)
VALUE_MAX = 8192
ACTIONS = {"list": "secrets-list", "set": "secret-set", "remove": "secret-remove"}  # the helper's ops
SLOTS = (("gitlab-read", "read_api token: pipelines, jobs, logs, bits-manifests"),
         ("gitlab-ops", "the project token for pipeline actions and variables"),
         ("gitlab-groups", "read_api token that can read the admin groups' members"),
         ("gitlab-mirror", "bits-manifests token that may push to main (the mirror)"),
         ("github-read", "a public-read GitHub token (no scopes): the catalog and the chat's docs"),
         ("s3-access-<bucket>", "the store's access key id"),
         ("s3-secret-<bucket>", "the store's secret key"))


def step_problem(action, slot, value):
    """Why the helper would refuse this step, checked before the touch's grant is used."""
    if action != "list" and not (isinstance(slot, str) and SLOT.fullmatch(slot)):
        return "a slot is a name like gitlab-read or s3-access-<bucket>"
    if action == "set" and not (isinstance(value, str) and value.strip() and len(value) <= VALUE_MAX
                                and value.strip().isprintable()):
        return "a value is one line of at most %d characters" % VALUE_MAX
    return None


def render(keys):
    """The page for an admin whose *keys* hold the passphrase."""
    known = "<table>%s</table>" % "".join(
        "<tr><td><code>%s</code></td><td>%s</td></tr>" % (escape(n), escape(d)) for n, d in SLOTS)
    intro = ("<p>The GitLab tokens and store keys that the credentials proxy adds to the services' "
             "requests. They are kept in <code>secrets/service-secrets.enc</code>, encrypted with the "
             "passphrase; bits-admin never keeps them and shows their names only. Each step needs a "
             "touch of your key, which opens the passphrase stored under it.</p>")
    if not keys:
        return intro + ("<p>None of your keys holds the passphrase: store it under one on the "
                        "<a href=/>main page</a> first.</p>") + known
    return intro + (
        "<p>Your key: <select id=secrets-key>%s</select></p>"
        "<h2>Set or replace one</h2><form id=secret-set><input id=secret-slot list=slots size=22 "
        "autocomplete=off placeholder=gitlab-read> <input id=secret-value type=password size=36 "
        "autocomplete=off placeholder=value> <button>Set</button></form><datalist id=slots>%s</datalist>"
        "<h2>Which are set</h2><form id=secrets-list><button>Show</button></form><ul id=slot-list></ul>"
        "<p id=status></p><h2>Slots</h2>%s<script src=/admin.js></script>" % (
            "".join("<option value=\"%s\">%s</option>" % (escape(k["id"]), escape(k.get("label", "")))
                    for k in keys),
            "".join("<option value=\"%s\">" % escape(n) for n, _ in SLOTS if "<" not in n), known))
