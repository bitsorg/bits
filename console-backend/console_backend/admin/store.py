# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's Store page (ADR bits-services 0004, K): bits-store's config.json as JSON,
the keys its volumes name (set or missing; a new value typed here, kept only until the
apply) and the console's store-token key. Planned by the host helper, which has
bits-store check the result; applied with a touch of the admin's own key."""

import json
from html import escape

from .settings import CSS

# For a store without a configuration yet: one private volume, one binding.
TEMPLATE = {"issuer": "https://bits-store.example.org", "audience": "example-site/store", "keys": {},
            "volumes": {"main": {"endpoint": "https://s3.example.org", "bucket": "bits-store",
                                 "region": "us-east-1", "access_key_env": "BITS_STORE_MAIN_ACCESS_KEY",
                                 "secret_key_env": "BITS_STORE_MAIN_SECRET_KEY"}},
            "bindings": [{"prefix": "/store", "volume": "main", "key_prefix": "TARS"}]}
CONSOLE_KID = "console-1"


def key_names(config):
    """The key names the config's volumes name, sorted."""
    vols = config.get("volumes") if isinstance(config, dict) and isinstance(config.get("volumes"), dict) else {}
    return sorted({v.get(k) for v in vols.values() if isinstance(v, dict)
                   for k in ("access_key_env", "secret_key_env") if isinstance(v.get(k), str) and v.get(k)})


def trusts(config, x):
    keys = config.get("keys") if isinstance(config, dict) and isinstance(config.get("keys"), dict) else {}
    return any(isinstance(k, dict) and k.get("x") == x for k in keys.values())


def config_from(form, console_key):
    """(config, None) from the form's JSON, the console's key added if asked; (None, why)."""
    try:
        config = json.loads(form.get("config") or "")
    except ValueError as e:
        return None, "the configuration is not JSON: %s" % e
    if not isinstance(config, dict):
        return None, "the configuration is a JSON object"
    if form.get("trust_console") and console_key and not trusts(config, console_key):
        if not isinstance(config.get("keys"), dict):
            config["keys"] = {}
        config["keys"][CONSOLE_KID] = {"x": console_key, "signer": "user"}
    return config, None


def credentials_from(form):
    """{NAME: value}: the keys given a value on the form (empty fields change nothing)."""
    out = {k[4:]: v for k, v in form.items() if k.startswith("key:") and v}
    name, value = (form.get("new_key_name") or "").strip(), form.get("new_key_value") or ""
    if name and value:
        out[name] = value
    return out


def render(answer):
    config = answer.get("config")
    text = json.dumps(config if isinstance(config, dict) else TEMPLATE, indent=1)
    have, console = set(answer.get("credentials") or []), answer.get("console_key")
    names = key_names(config if isinstance(config, dict) else TEMPLATE)
    rows = "".join("<tr><td class=k>%s</td><td>%s</td><td><input type=password name=\"key:%s\" size=30 "
                   "autocomplete=off placeholder=\"%s\"></td></tr>" % (
                       escape(n), "set" if n in have else "<b>missing</b>", escape(n),
                       "unchanged" if n in have else "value") for n in names)
    if console and trusts(config, console):
        trust = "<p>The store trusts the console's store-token key.</p>"
    elif console:
        trust = ("<p><label><input type=checkbox name=trust_console value=1 checked> Trust the console's "
                 "store-token key as <code>%s</code> (<code>%s</code>)</label></p>" % (CONSOLE_KID, escape(console)))
    else:
        trust = "<p>The store-token signer did not answer: its key cannot be added from here.</p>"
    first = "" if isinstance(config, dict) else (
        "<p>There is no <code>%s</code> yet: this is a template to start from.</p>" % escape(str(answer.get("path"))))
    return ("<style>%s</style><p>bits-store's configuration, <code>%s</code>: who may sign tokens "
            "(<code>keys</code>), the buckets (<code>volumes</code>) and which logical paths reach them "
            "(<code>bindings</code>). The running store reloads it within seconds of a change; bits-store "
            "checks it before it is written. The S3 keys the volumes name are kept apart, in "
            "<code>secrets/bits-store.env</code>: shown here as set or missing, never read back.</p>%s"
            "<form method=post action=/store/plan><input type=hidden name=base value=\"%s\">"
            "<h2>Configuration</h2><textarea name=config rows=24 cols=90 spellcheck=false "
            "style=\"font:13px ui-monospace,monospace;width:100%%\">%s</textarea>%s"
            "<h2>Keys</h2><p>A value typed here replaces the key; empty leaves it as it is. New keys take "
            "effect when the store restarts (<code>make store-up</code> on the host).</p><table class=set>"
            "<tr><th>Name<th>Now<th>New value</tr>%s<tr><td><input name=new_key_name size=34 "
            "placeholder=\"BITS_STORE_..._KEY (another)\"></td><td></td><td><input type=password "
            "name=new_key_value size=30 autocomplete=off placeholder=value></td></tr></table>"
            "<p><button>Plan</button></p></form>" % (
                CSS, escape(str(answer.get("path") or "config.json")), first, escape(str(answer.get("base", ""))),
                escape(text), trust, rows))


def plan_html(planned, plan_id):
    diff = "\n".join(str(d) for d in planned.get("diff") or [])
    keys = planned.get("credentials") or {}
    if not diff and not keys:
        return "<p>Nothing would change.</p><p><a href=/store>Back</a></p>"
    return ("<style>%s</style><h2>Plan</h2><p>bits-store takes this configuration. Apply writes it (the "
            "old one kept as a dated <code>config.json.bak-&hellip;</code>) and needs a touch of your key.</p>"
            "<pre>%s</pre>%s%s<p><button id=apply data-path=/store data-plan=\"%s\">Apply with your key</button> "
            "<a href=/store>Back</a></p><p id=status></p><script src=/admin.js></script>" % (
                CSS, escape(diff) if diff else "config.json would not change",
                "<p>Keys: %s.</p>" % escape(", ".join("%s %s" % (n, w) for n, w in sorted(keys.items())))
                if keys else "",
                "<p>New keys take effect when the store restarts: <code>make store-up</code> on the host.</p>"
                if planned.get("restart") else "", escape(plan_id)))
