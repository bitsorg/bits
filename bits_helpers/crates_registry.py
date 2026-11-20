# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's crates binding as cargo's registry (bits-store README: crates).

The site key store.crates (or $BITS_CRATES_REGISTRY; set empty: none) names it, e.g.
https://store.example.org/crates. A build in a container then reads
/.cargo/config.toml, which replaces crates.io with it: cargo reads that file whatever
CARGO_HOME a recipe sets, and takes no environment variable for it. Asked once per
run whether it answers; when it does not, and outside a container, cargo reads
crates.io as before.
"""

import os
import urllib.parse

import requests

from bits_helpers import site
from bits_helpers.log import info

_answers = {}        # store root -> answers /health, asked once per run


def configured():
  """The site's crates registry URL, or "": $BITS_CRATES_REGISTRY when set, even empty."""
  url = os.environ.get("BITS_CRATES_REGISTRY")
  if url is None:
    url = str(site.get("store.crates") or "")
  return url.strip().rstrip("/")


def _up(url):
  u = urllib.parse.urlsplit(url)
  root = "%s://%s" % (u.scheme, u.netloc)
  if root not in _answers:
    try:
      _answers[root] = requests.get(root + "/health", timeout=(5, 15)).status_code == 200
    except requests.RequestException:
      _answers[root] = False
    if not _answers[root]:
      info("crates registry %s does not answer: cargo reads crates.io", root)
  return _answers[root]


def cargo_config(url):
  """cargo's configuration replacing crates.io with *url*'s registry."""
  return ("# bits: crates.io through the site's bits-store (store.crates)\n"
          "[source.crates-io]\nreplace-with = \"bits-store\"\n\n"
          "[source.bits-store]\nregistry = \"sparse+%s/index/\"\n" % url)


def container_volume(work_dir):
  """The docker volume giving a build container /.cargo/config.toml (its file kept
  under *work_dir*), or None when there is no registry or it does not answer."""
  url = configured()
  if not url.startswith(("https://", "http://")) or not _up(url):
    return None
  path = os.path.join(os.path.abspath(work_dir), ".bits", "cargo", "config.toml")
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = "%s.%d" % (path, os.getpid())
  with open(tmp, "w") as f:
    f.write(cargo_config(url))
  os.replace(tmp, path)
  return "%s:/.cargo/config.toml:ro" % path
