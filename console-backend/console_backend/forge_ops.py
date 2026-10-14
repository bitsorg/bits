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

    def __init__(self, api_url, token, project, timeout=15, resolve=None, proxied=False):
        # *resolve* () -> (api_url, token), asked at each request: the credentials
        # proxy's port and gate token change on its restart and rotate daily.
        self._resolve = resolve or (lambda: (api_url, token))
        self._proxied = proxied
        self._project = self._seg(project)   # numeric id or url-encoded path
        self._timeout = timeout

    def _seg(self, value):
        """One path segment, %-encoded (a project path's / as %2F). Through the
        credentials proxy encoded twice: the proxy decodes the path once before
        it forwards it, and a %2F decoded to / is another URL to GitLab."""
        v = quote(str(value), safe="")
        return quote(v, safe="") if self._proxied else v

    def _creds(self):
        try:
            api, token = self._resolve()
        except OSError as e:          # the credentials proxy cannot be reached
            raise ForgeError(503, str(e)[:200])
        return (api or "").rstrip("/"), token

    @property
    def _api(self):
        return self._creds()[0]

    @property
    def _token(self):
        return self._creds()[1]

    @classmethod
    def from_settings(cls, settings, kind="ops", project=None):
        """None unless GitLab, a credential of *kind* (ops, read, groups) and
        the project are configured. Through the credentials proxy when it is
        configured (credproxy), else with the environment's token."""
        from . import credproxy
        project = project or settings.forge_project
        if not (project and credproxy.has(settings, kind)):
            return None
        if not (credproxy.proxied(settings) or settings.gitlab_api_url):
            return None
        return cls(None, None, project, resolve=lambda: credproxy.gitlab(settings, kind),
                   proxied=credproxy.proxied(settings))

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
            self._get("/repository/files/%s?ref=%s" % (self._seg(path),
                                                       quote(ref, safe="")))
            return True
        except ForgeError as e:
            if e.status == 404:
                return False
            raise

    def file_exists(self, path, ref):
        return self._file_exists(path, ref)

    def create_commit(self, branch, actions, message):
        """One commit of *actions* (the commits API's create/update/delete) on
        *branch*; returns it (its id is the sha)."""
        return self._json("POST", "/repository/commits",
                          {"branch": branch, "commit_message": message, "actions": actions})

    def put_file(self, path, branch, content_b64, message):
        """Create or update a repo file (base64 content) on `branch`. Upsert: PUT
        when it exists, else POST. Raises ForgeError on failure."""
        p = "/repository/files/%s" % self._seg(path)
        method = "PUT" if self._file_exists(path, branch) else "POST"
        return self._json(method, p, {"branch": branch, "content": content_b64,
                                      "encoding": "base64", "commit_message": message})

    def delete_file(self, path, branch, message):
        p = "/repository/files/%s" % self._seg(path)
        return self._json("DELETE", p, {"branch": branch, "commit_message": message})

    def set_ci_variable(self, key, value):
        """Upsert a project CI/CD variable: PUT if it exists, else POST. Kept
        unprotected/unmasked (these are refs/flags, not secrets)."""
        try:
            return self._json("PUT", "/variables/%s" % self._seg(key),
                              {"value": value})
        except ForgeError as e:
            if e.status == 404:
                return self._json("POST", "/variables",
                                  {"key": key, "value": value,
                                   "protected": False, "masked": False})
            raise

    def _get_all(self, path, per_page=100, max_pages=50):
        """Every page of a list endpoint (GitLab pages by `page`/`per_page`)."""
        sep = "&" if "?" in path else "?"
        out = []
        for page in range(1, max_pages + 1):
            got = self._get("%s%spage=%d&per_page=%d" % (path, sep, page, per_page)) or []
            out.extend(got)
            if len(got) < per_page:
                break
        return out

    def list_pipelines(self, updated_after, page=1, per_page=100):
        """One page of the project's pipelines updated after *updated_after*
        (ISO 8601), oldest update first."""
        return self._get("/pipelines?updated_after=%s&order_by=updated_at&sort=asc"
                         "&page=%d&per_page=%d" % (quote(updated_after, safe=""),
                                                   page, per_page)) or []

    def list_pipelines_before(self, updated_before, status="failed", page=1, per_page=100):
        """One page of the project's pipelines in *status* updated before
        *updated_before* (ISO 8601), newest update first (the failure backfill)."""
        return self._get("/pipelines?updated_before=%s&status=%s&order_by=updated_at&sort=desc"
                         "&page=%d&per_page=%d" % (quote(updated_before, safe=""),
                                                   quote(status, safe=""), page, per_page)) or []

    def finished_pipelines(self, ref, per_page=50, scope="finished"):
        """The newest pipelines of branch *ref* in *scope* (finished, running,
        pending), newest first."""
        return self._get("/pipelines?ref=%s&scope=%s&order_by=id&sort=desc&per_page=%d"
                         % (quote(ref, safe=""), quote(scope, safe=""), per_page)) or []

    def commit(self, sha):
        return self._get("/repository/commits/%s" % self._seg(sha))

    def _stream(self, path, headers=None, hops=3):
        """GET a raw response, streamed. A redirect (job artifacts are often served
        from object storage) is followed WITHOUT the token: requests drops only
        Authorization on a cross-host redirect, never PRIVATE-TOKEN."""
        url = "%s/projects/%s%s" % (self._api, self._project, path)
        send = dict(headers or {}, **{"PRIVATE-TOKEN": self._token})
        for _ in range(hops + 1):
            try:
                r = requests.get(url, headers=send, timeout=self._timeout, stream=True,
                                 allow_redirects=False)
            except requests.RequestException as e:
                raise ForgeError(0, str(e)[:200])
            if r.status_code not in (301, 302, 303, 307, 308) or not r.headers.get("Location"):
                return r
            r.close()
            from urllib.parse import urljoin, urlsplit
            nxt = urljoin(url, r.headers["Location"])
            # The token only to the API's own scheme and host (the credentials
            # proxy rewrites its upstream's redirects to itself).
            here, there = urlsplit(self._api), urlsplit(nxt)
            if (there.scheme, there.netloc) != (here.scheme, here.netloc):
                send = dict(headers or {})
            url = nxt
        raise ForgeError(0, "GET %s: too many redirects" % path)

    def _tail(self, path, max_bytes, max_total=64 << 20, max_seconds=60):
        """The last *max_bytes* of a (possibly huge) raw response; None on 404.
        Asks for just the end (Range); a server that sends the whole body is read
        streamed, at most *max_total* bytes or *max_seconds*, past which the end
        is out of reach and the answer is None."""
        import time as _time
        r = self._stream(path, {"Range": "bytes=-%d" % max_bytes})
        with r:
            if r.status_code in (404, 416):
                return None
            if r.status_code // 100 != 2:
                raise ForgeError(r.status_code, "GET %s -> %s" % (path, r.status_code))
            buf, total, began = bytearray(), 0, _time.monotonic()
            try:
                for chunk in r.iter_content(64 * 1024):
                    buf += chunk
                    total += len(chunk)
                    if len(buf) > 2 * max_bytes:
                        del buf[:len(buf) - max_bytes]
                    if total > max_total or _time.monotonic() - began > max_seconds:
                        return None
            except requests.RequestException as e:
                raise ForgeError(0, str(e)[:200])
            return bytes(buf[-max_bytes:])

    def job_log_tail(self, job_id, max_bytes=1 << 20):
        """The end of a job's log (GitLab keeps at most its log limit, 4 MB by
        default, and cuts the rest)."""
        return self._tail("/jobs/%s/trace" % job_id, max_bytes)

    def job_artifact_tail(self, job_id, name, max_bytes=1 << 20):
        """The end of one artifact file of a job, or None when it has none (or
        it expired)."""
        return self._tail("/jobs/%s/artifacts/%s" % (job_id, quote(name, safe="/")), max_bytes)

    def _raw(self, path, max_bytes):
        """A whole raw response of at most *max_bytes*; None on 404. Larger is a
        ForgeError (413), never a silently cut body."""
        r = self._stream(path)
        with r:
            if r.status_code == 404:
                return None
            if r.status_code // 100 != 2:
                raise ForgeError(r.status_code, "GET %s -> %s" % (path, r.status_code))
            buf = bytearray()
            try:
                for chunk in r.iter_content(256 * 1024):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise ForgeError(413, "GET %s: over %d bytes" % (path, max_bytes))
            except requests.RequestException as e:
                raise ForgeError(0, str(e)[:200])
            return bytes(buf)

    def file_raw(self, path, ref, max_bytes=1 << 20):
        """A repository file at *ref* (a commit), or None when it is not there."""
        return self._raw("/repository/files/%s/raw?ref=%s" % (self._seg(path), quote(ref, safe="")),
                         max_bytes)

    def repository_archive(self, sha, path=None, max_bytes=256 << 20):
        """The repository at commit *sha* as a .tar.gz (only *path* when given);
        None when the commit is unknown."""
        q = "sha=%s" % quote(sha, safe="")
        if path:
            q += "&path=%s" % quote(path, safe="")
        return self._raw("/repository/archive.tar.gz?%s" % q, max_bytes)

    def job_artifacts(self, job_id, max_bytes=64 << 20):
        """A job's artifacts archive (zip), or None when it has none (or they
        expired)."""
        return self._raw("/jobs/%s/artifacts" % job_id, max_bytes)

    def get_pipeline(self, pid):
        return self._get("/pipelines/%s" % pid)

    def pipeline_jobs(self, pid):
        """Every job of the pipeline, retried attempts included."""
        return self._get_all("/pipelines/%s/jobs?include_retried=true" % pid)

    def pipeline_variables(self, pid):
        """The pipeline's variables as GitLab lists them ([{key, value, ...}])."""
        return self._get("/pipelines/%s/variables" % pid) or []

    def pipeline_community(self, pid):
        """The pipeline's COMMUNITY variable (lowercased), or '' if none — used to
        authorize a lifecycle action against the pipeline's own community."""
        for v in self.pipeline_variables(pid):
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
