# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's git binding as the mirror of git sources (bits-store README: git).

The site key store.git (or $BITS_GIT_MIRROR) names it, e.g.
https://store.example.org/git: bits' clones, fetches and ls-remote of https sources
go there (url.<mirror>/.insteadOf=https://), through the store's partial mirrors.
Unset, or the store not answering, git reads upstream; a command that fails through
the mirror is run again upstream.
"""

import os
import urllib.parse

import requests

from bits_helpers import site
from bits_helpers.log import info

_answers = {}   # store root -> answers /health, asked once per run
_told = set()


def configured():
  """The site's git mirror URL, or ""."""
  return (os.environ.get("BITS_GIT_MIRROR") or str(site.get("store.git") or "")).strip().rstrip("/")


def _root(url):
  u = urllib.parse.urlsplit(url)
  return "%s://%s" % (u.scheme, u.netloc)


def _up(root):
  if root not in _answers:
    try:
      _answers[root] = requests.get(root + "/health", timeout=(5, 15)).status_code == 200
    except requests.RequestException:
      _answers[root] = False
    if not _answers[root]:
      info("git mirror %s does not answer: git sources from upstream", root)
  return _answers[root]


def config():
  """git configuration (key, value) sending https sources through the mirror, when
  it answers: the store's own URLs left as they are, and the CA bundle bits verifies
  the store with given to git for the store only."""
  url = configured()
  if not url.startswith("https://") or not _up(_root(url)):
    return []
  root = _root(url) + "/"
  out = [("url.%s/.insteadOf" % url, "https://"), ("url.%s.insteadOf" % root, root)]
  ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
  if ca:
    out.append(("http.%s.sslCAInfo" % root, ca))
  return out


def failed(args, output):
  """Said once per command kind: a git command failed through the mirror."""
  if args[0] not in _told:
    _told.add(args[0])
    info("git %s through the mirror failed, upstream instead: %s", args[0],
         " ".join(str(output).split())[:200])
