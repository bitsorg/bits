# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forge operations adapter — the backend actuates privileged forge operations
(Phase 1: trigger a build pipeline; later cancel / retry / delete) with a project
access token IT holds, AFTER the caller's identity and authorization have been
checked. The token never reaches the browser.

One interface, one GitLab implementation today; a GitHub/Gitea one slots in behind
the same interface later, which is what keeps the console forge-neutral. The token
lives in the backend env for now (Phase 1); custody moves into the security-proxy
in a later increment — this module is the single place that touches it.
"""

from urllib.parse import quote

import requests


class ForgeError(Exception):
    """A non-2xx (or transport) failure talking to the forge. `status` is the HTTP
    code (0 for transport errors); `message` is a short, non-secret detail."""
    def __init__(self, status, message):
        super().__init__("forge %s: %s" % (status, message))
        self.status = status
        self.message = message


class GitLabForge:
    """GitLab implementation. Holds an api-scope project access token and acts on
    one project (the shared build project)."""

    def __init__(self, api_url, token, project, timeout=15):
        self._api = api_url.rstrip("/")
        self._token = token
        self._project = quote(str(project), safe="")   # numeric id or url-encoded path
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings):
        """None unless the API url, token AND project are all configured."""
        if not (settings.gitlab_api_url and settings.forge_ops_token and settings.forge_project):
            return None
        return cls(settings.gitlab_api_url, settings.forge_ops_token, settings.forge_project)

    def _post(self, path, body):
        try:
            r = requests.post("%s/projects/%s%s" % (self._api, self._project, path),
                              headers={"PRIVATE-TOKEN": self._token,
                                       "Content-Type": "application/json"},
                              json=body, timeout=self._timeout)
        except requests.RequestException as e:
            raise ForgeError(0, str(e)[:200])
        if r.status_code // 100 != 2:
            msg = ""
            try:
                msg = (r.json() or {}).get("message") or r.text[:200]
            except Exception:
                msg = (r.text or "")[:200]
            raise ForgeError(r.status_code, msg)
        try:
            return r.json()
        except Exception:
            return {}

    def trigger_pipeline(self, ref, variables=None, name=None):
        """Create a pipeline on `ref`. `variables` is a list of {key,value} (or a
        dict). Returns {id, web_url, status}. Raises ForgeError on failure."""
        body = {"ref": ref}
        if variables:
            if isinstance(variables, dict):
                variables = [{"key": k, "value": str(v)} for k, v in variables.items()]
            body["variables"] = variables
        if name:
            body["name"] = name
        d = self._post("/pipeline", body)
        return {"id": d.get("id"), "web_url": d.get("web_url"), "status": d.get("status")}
