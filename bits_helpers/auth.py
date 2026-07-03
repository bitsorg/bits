# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Personal login and short-lived scoped credentials for uploads.

Two files live under ``~/.bits`` (override the directory with ``$BITS_HOME``):

``config``
    A small INI holding the developer's long-lived personal *bits-token*
    (issued by bits-console after a CERN SSO login) and the console URL::

        [bits]
        console_url = https://bits-console.web.cern.ch
        bits_token  = <opaque token copied from the console>

``session``
    JSON with the *short-lived* scoped S3 credentials that ``bits login``
    obtains by exchanging the bits-token. It is rewritten on every login and
    deleted by ``bits logout``.

Both files must be private (mode 600): they carry secrets. We refuse to read a
world/group-readable token file, and always write with 0600.

The private manifest-signing key never lives here — bits-console countersigns
manifests server-side (single trust anchor). ``bits login`` only yields upload
credentials scoped to the caller's area (their user area, or a shared common
area for group admins).
"""

import json
import os
import stat
import time


def bits_home() -> str:
  return os.environ.get("BITS_HOME") or os.path.expanduser("~/.bits")


def config_path() -> str:
  return os.path.join(bits_home(), "config")


def session_path() -> str:
  return os.path.join(bits_home(), "session")


def _require_private(path: str) -> None:
  """Refuse to read *path* if it is group/other-accessible."""
  mode = os.stat(path).st_mode
  if mode & (stat.S_IRWXG | stat.S_IRWXO):
    raise PermissionError(
        "%s is group/other-accessible; run `chmod 600 %s` (it holds a secret)"
        % (path, path))


def _write_private(path: str, data: str) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  try:
    os.write(fd, data.encode())
  finally:
    os.close(fd)
  os.chmod(path, 0o600)  # tighten even if the file pre-existed


def read_config() -> dict:
  """Return {console_url, bits_token} from ~/.bits/config (empty if absent)."""
  path = config_path()
  if not os.path.isfile(path):
    return {}
  _require_private(path)
  import configparser
  cp = configparser.ConfigParser()
  cp.read(path)
  sec = cp["bits"] if cp.has_section("bits") else {}
  return {"console_url": sec.get("console_url", "").rstrip("/"),
          "bits_token": sec.get("bits_token", "")}


# ── Duration parsing ──────────────────────────────────────────────────────────

def parse_duration(text: str) -> int:
  """Parse ``30m`` / ``8h`` / ``3600`` into seconds."""
  text = str(text).strip().lower()
  units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
  if text and text[-1] in units:
    return int(float(text[:-1]) * units[text[-1]])
  return int(text)


# ── Token exchange ────────────────────────────────────────────────────────────

def exchange_token(console_url: str, bits_token: str, ttl_seconds: int) -> dict:
  """Exchange the bits-token for scoped S3 credentials at the console.

  POST {console_url}/api/login  Bearer <bits_token>, body {"ttl": <seconds>}.
  The console authorises the caller (from the token) and returns AWS-STS-shaped
  temporary credentials plus the write area they are scoped to:

      {"AccessKeyId", "SecretAccessKey", "SessionToken",
       "Expiration": <ISO8601>, "Area": "user/<name>"|"common",
       "Endpoint": <optional S3 endpoint>}
  """
  import requests
  resp = requests.post(
      console_url + "/api/login",
      headers={"Authorization": "Bearer " + bits_token},
      json={"ttl": int(ttl_seconds)},
      timeout=30)
  resp.raise_for_status()
  return resp.json()


# ── Session file ──────────────────────────────────────────────────────────────

def _iso_to_epoch(value) -> float:
  if isinstance(value, (int, float)):
    return float(value)
  from datetime import datetime
  s = str(value).replace("Z", "+00:00")
  return datetime.fromisoformat(s).timestamp()


def write_session(creds: dict) -> str:
  """Persist exchanged *creds* to ~/.bits/session (0600). Returns the path."""
  data = {
      "access_key_id": creds["AccessKeyId"],
      "secret_access_key": creds["SecretAccessKey"],
      "session_token": creds.get("SessionToken", ""),
      "expiration": _iso_to_epoch(creds["Expiration"]),
      "area": creds.get("Area", ""),
      "endpoint": creds.get("Endpoint", ""),
  }
  path = session_path()
  _write_private(path, json.dumps(data))
  return path


def load_session():
  """Return the current session dict, or None if missing/expired."""
  path = session_path()
  if not os.path.isfile(path):
    return None
  _require_private(path)
  try:
    with open(path) as fh:
      data = json.load(fh)
  except Exception:
    return None
  if float(data.get("expiration", 0)) <= time.time():
    return None
  return data


def logout() -> bool:
  """Remove the session file. Returns True if one was present."""
  path = session_path()
  if os.path.isfile(path):
    os.remove(path)
    return True
  return False


def apply_session_env() -> bool:
  """Export a valid session's scoped credentials into the environment.

  A no-op when already-set env vars are present (so CI-injected credentials
  always win) or when there is no valid session. Returns True if it exported
  anything.
  """
  if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("BITS_AWS_ACCESS_KEY_ID"):
    return False
  sess = load_session()
  if not sess:
    return False
  os.environ["AWS_ACCESS_KEY_ID"] = sess["access_key_id"]
  os.environ["AWS_SECRET_ACCESS_KEY"] = sess["secret_access_key"]
  if sess.get("session_token"):
    os.environ["AWS_SESSION_TOKEN"] = sess["session_token"]
  if sess.get("endpoint"):
    os.environ.setdefault("S3_ENDPOINT_URL", sess["endpoint"])
  return True


def session_status() -> str:
  """One-line human summary of the current login state."""
  sess = load_session()
  if not sess:
    return "not logged in (no valid session; run `bits login`)"
  remaining = int(float(sess["expiration"]) - time.time())
  h, rem = divmod(max(remaining, 0), 3600)
  m, _ = divmod(rem, 60)
  return "logged in; area=%s; expires in %dh%02dm" % (
      sess.get("area") or "?", h, m)
