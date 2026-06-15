#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Standard library
import os
import subprocess
import sys
from os.path import exists, expanduser

# Internal
from bits_helpers.cmd import getstatusoutput
from bits_helpers.log import banner, debug


def generate_analytics_id():
  """Generate and persist a unique analytics UUID via ``uuidgen``.

  Returns ``True`` on success, ``False`` if ``uuidgen`` is unavailable (in
  which case analytics are automatically disabled).
  """
  os.makedirs(os.path.expanduser("~/.config/bits"), exist_ok=True)
  err, output = getstatusoutput("uuidgen >  ~/.config/bits/analytics-uuid")
  # If an error is found while generating the unique user ID, we disable
  # the analytics on the machine.
  if err:
    debug("Could not generate unique ID for user. Disabling analytics")
    getstatusoutput("touch ~/.config/bits/disable-analytics")
    return False
  return True

def askForAnalytics():
  """Prompt the user interactively to opt in or out of analytics.

  Returns ``True`` if the user accepts (and a UUID was generated successfully),
  ``False`` otherwise.
  """
  banner("In order to improve user experience, Bits would like to gather "
         "analytics about your builds.\nYou can find all the details at:\n\n"
         "  https://github.com/bitsorg/bits/blob/master/ANALYTICS.md\n")
  a = input("Is that ok for you [YES/no]? ")
  if a.strip() and a.strip().lower().startswith("n"):
    debug("User requested disabling analytics.")
    return disable_analytics()
  return generate_analytics_id()

# Helper function to decide whether or not we should run analytics.
# It's done this way so that we can easily test all the alternatives.
# This is the rationale to enable the analytics:
# - In case user disabled analytics via environment variable or by
#   answering no when prompted the first time. Just run as usual.
# - In case there is already an analytics user id, it means the user
#   already replied yes to the question wether he wants analytics or
#   not, so we proceed with analytics.
# - If we are not running in a tty, run without analytics.
# - In case there is no analytics id, ask wether is ok to have
#   analytics. If no, remember the answer and disable it. If yes,
#   generate a uuid with uuidgen and remember it.
def decideAnalytics(hasDisableFile, hasUuid, isTty, questionCallback):
  """Return ``True`` when analytics should be sent for this invocation.

  Parameters are injected so that each decision branch can be unit-tested
  without touching the file system or opening a tty.
  """
  if hasDisableFile:
    debug("Analytics previously disabled.")
    return False
  if hasUuid:
    debug("User has analytics id. Pushing analytics to Google Analytics.")
    return True
  if not isTty:
    debug("This is not an interactive process and "
          "no indication has been given about analytics. Disabling")
    return False
  return questionCallback()

def report(eventType, **metadata):
  """Fire-and-forget a Google Analytics hit via ``curl``.

  Does nothing when ``BITS_NO_ANALYTICS`` is set in the environment.
  """
  if "BITS_NO_ANALYTICS" in os.environ:
    return
  opts = {
    "v": "1",
    "tid": os.environ["BITS_ANALYTICS_ID"],
    "cid": os.environ["BITS_ANALYTICS_USER_UUID"],
    "aip": "1",
    "an": "aliBuild",
    "av": os.environ["BITS_VERSION"],
    "t": eventType
  }
  opts.update(metadata)
  architecture = os.environ["BITS_ARCHITECTURE"]
  ostype = "Macintosh" if architecture.startswith("osx") else "Linux"
  osversion, osprocessor = architecture.split("_", 1)
  args = ["curl", "--max-time", "5",
          "--user-agent", "bitsBuild/{} ({}; {} {}) Python/{}".format(
                                                    os.environ["BITS_VERSION"],
                                                    ostype,
                                                    osprocessor,
                                                    osversion,
                                                    ".".join([str(x) for x in sys.version_info[:3]])
                                                   )
         ]
  for k,v in opts.items():
    if not v:
      continue
    args += ["-d", "%s=%s" %(k,v)]

  args += ["--silent", "--output", "/dev/null",
           "https://www.google-analytics.com/collect"]
  try:
    subprocess.Popen(args)
  except Exception:
    pass

def report_event(category, action, label = "", value = None):
  report("event", ec=category, ea=action, el = label, ev = value)

def report_screenview(screen_name):
  report("screenview", cd=screen_name)

def report_timing(category, var, value, label):
  report("timing", utc=category, utv=var, utt=value, utl=label)

def report_exception(e):
  report("exception",
    exd = e.__class__.__name__,
    exf = "1")

def enable_analytics() -> None:
  """Re-enable analytics: remove the disable flag and regenerate a UUID if needed."""
  disable_flag = expanduser("~/.config/bits/disable-analytics")
  if exists(disable_flag):
    os.unlink(disable_flag)
  if not exists(expanduser("~/.config/bits/analytics-uuid")):
    generate_analytics_id()

def disable_analytics():
  """Persist the analytics opt-out and return ``False``.

  Uses the shell rather than Python's ``os.makedirs`` because intermediate
  directories may not be writeable in all environments.
  """
  getstatusoutput("mkdir -p ~/.config/bits && touch ~/.config/bits/disable-analytics")
  return False

