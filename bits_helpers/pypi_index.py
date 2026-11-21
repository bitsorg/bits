# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's pypi binding as a build's pip index (bits-store README: pypi).

The site key store.pypi (or $BITS_PYPI_INDEX) names it, e.g.
https://store.example.org/pypi/simple: public, so no token reaches recipe code.
Unset, unreachable, or with pip's index named already, pip reads its own.
"""

import os
import subprocess

import requests

from bits_helpers import site
from bits_helpers.log import info

ACCEPT = "application/vnd.pypi.simple.v1+json"
_reachable = {}   # index URL -> reachable, asked once per run
_trusting = {}    # image -> sets PIP_CERT, asked once per run


def configured():
  """The site's pypi index URL, or ""."""
  return (os.environ.get("BITS_PYPI_INDEX") or str(site.get("store.pypi") or "")).strip().rstrip("/")


def _answers(url):
  """Does *url* answer as a pypi index (pip's own page): asked once per run."""
  if url not in _reachable:
    try:
      r = requests.get(url + "/pip/", headers={"Accept": ACCEPT}, timeout=(5, 15), allow_redirects=False)
      _reachable[url] = r.status_code == 200
      why = "HTTP %d" % r.status_code
    except requests.RequestException as exc:
      _reachable[url], why = False, type(exc).__name__
    if not _reachable[url]:
      info("pip index %s does not answer (%s): pip reads its own index", url, why)
  return _reachable[url]


def _image_trusts(image):
  """Does the local *image* set PIP_CERT (bits-containers: the CERN CA bundle)?"""
  if image not in _trusting:
    try:
      if not image:
        raise OSError("no image")
      env = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image],
                           capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
      env = ""
    _trusting[image] = '"PIP_CERT=' in env
    if not _trusting[image]:
      info("image %s sets no PIP_CERT (an older one? pull it): pip reads its own index", image)
  return _trusting[image]


def build_environment(docker, image=None):
  """PIP_INDEX_URL, and outside a container PIP_CERT (the CA bundle bits verifies
  the store with), for a build's environment: none when not configured or reachable,
  outside a container when the environment sets them already, in one (*image*) when
  the image does not set PIP_CERT."""
  url = configured()
  if not url or not docker and "PIP_INDEX_URL" in os.environ or not _answers(url) \
      or docker and not _image_trusts(image):
    return []
  env = [("PIP_INDEX_URL", url)]
  ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
  if not docker and ca and "PIP_CERT" not in os.environ:
    env.append(("PIP_CERT", ca))   # a container trusts the CA itself (bits-containers)
  return env
