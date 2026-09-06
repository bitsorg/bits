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

    def _send(self, method, path):
        """POST/DELETE with no body (pipeline lifecycle actions)."""
        try:
            r = requests.request(method, "%s/projects/%s%s" % (self._api, self._project, path),
                                 headers={"PRIVATE-TOKEN": self._token}, timeout=self._timeout)
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

    def _get(self, path):
        try:
            r = requests.get("%s/projects/%s%s" % (self._api, self._project, path),
                             headers={"PRIVATE-TOKEN": self._token}, timeout=self._timeout)
        except requests.RequestException as e:
            raise ForgeError(0, str(e)[:200])
        if r.status_code // 100 != 2:
            raise ForgeError(r.status_code, "GET %s -> %s" % (path, r.status_code))
        return r.json()

    def _json(self, method, path, body):
        """A request carrying a JSON body (repo file create/update/delete)."""
        try:
            r = requests.request(method, "%s/projects/%s%s" % (self._api, self._project, path),
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

    def _file_exists(self, path, ref):
        try:
            self._get("/repository/files/%s?ref=%s" % (quote(path, safe=""),
                                                       quote(ref, safe="")))
            return True
        except ForgeError as e:
            if e.status == 404:
                return False
            raise

    def put_file(self, path, branch, content_b64, message):
        """Create or update a repo file (base64 content) on `branch`. Upsert: PUT
        when it exists, else POST. Raises ForgeError on failure."""
        p = "/repository/files/%s" % quote(path, safe="")
        method = "PUT" if self._file_exists(path, branch) else "POST"
        return self._json(method, p, {"branch": branch, "content": content_b64,
                                      "encoding": "base64", "commit_message": message})

    def delete_file(self, path, branch, message):
        p = "/repository/files/%s" % quote(path, safe="")
        return self._json("DELETE", p, {"branch": branch, "commit_message": message})

    def set_ci_variable(self, key, value):
        """Upsert a project CI/CD variable: PUT if it exists, else POST. Kept
        unprotected/unmasked (these are refs/flags, not secrets)."""
        try:
            return self._json("PUT", "/variables/%s" % quote(key, safe=""),
                              {"value": value})
        except ForgeError as e:
            if e.status == 404:
                return self._json("POST", "/variables",
                                  {"key": key, "value": value,
                                   "protected": False, "masked": False})
            raise

    def pipeline_community(self, pid):
        """The pipeline's COMMUNITY variable (lowercased), or '' if none — used to
        authorize a lifecycle action against the pipeline's own community."""
        for v in (self._get("/pipelines/%s/variables" % pid) or []):
            if isinstance(v, dict) and str(v.get("key", "")).upper() == "COMMUNITY":
                return str(v.get("value", "")).strip().lower()
        return ""

    def cancel_pipeline(self, pid):
        return self._send("POST", "/pipelines/%s/cancel" % pid)

    def retry_pipeline(self, pid):
        return self._send("POST", "/pipelines/%s/retry" % pid)

    def delete_pipeline(self, pid):
        return self._send("DELETE", "/pipelines/%s" % pid)

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
