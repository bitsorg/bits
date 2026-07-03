# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits login` -- exchange the personal bits-token for scoped upload creds."""

from bits_helpers import auth
from bits_helpers.log import banner, error, info


def doLogin(args) -> int:
  if getattr(args, "loginStatus", False):
    info("%s", auth.session_status())
    return 0

  if getattr(args, "loginLogout", False):
    info("Logged out." if auth.logout() else "No active session.")
    return 0

  cfg = auth.read_config()
  if not cfg.get("bits_token"):
    error("No bits-token found. Log in to bits-console with CERN SSO, then copy "
          "the issued token into %s:\n\n  [bits]\n  console_url = "
          "https://bits-console.web.cern.ch\n  bits_token  = <token>\n\n"
          "and `chmod 600` it.", auth.config_path())
    return 1
  if not cfg.get("console_url"):
    error("Set console_url in %s.", auth.config_path())
    return 1

  ttl = auth.parse_duration(args.duration)
  try:
    creds = auth.exchange_token(cfg["console_url"], cfg["bits_token"], ttl)
  except Exception as exc:
    error("Login failed: %s", exc)
    return 1
  auth.write_session(creds)
  banner("%s", auth.session_status())
  return 0
