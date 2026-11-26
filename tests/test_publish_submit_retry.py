# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""submit_ingest and submit_staged repeat a submission that met a network error or a
5xx, with a fresh signature, as prepub.submit_job does; only with an identity_path,
which prepub re-checks, so a lost reply does not publish twice."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import requests

import bits_helpers.cvmfs_publish as cp
import bits_helpers.prepub as pp


class Resp:
  def __init__(self, code):
    self.status_code, self.text = code, "busy" if code >= 500 else ""

  def json(self):
    return {"job_id": "J"}


class SubmitRetryTest(unittest.TestCase):
  def submit(self, staged, answers, identity):
    t = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, t)
    tar = os.path.join(t, "p.tar")
    with open(tar, "wb") as f:
      f.write(b"z")
    posts, signed = [], []

    class Session:
      def post(self, url, **kw):
        posts.append(url)
        a = answers.pop(0)
        if isinstance(a, Exception):
          raise a
        return Resp(a)
    with mock.patch.object(pp, "_make_session", lambda *a, **k: Session()), \
         mock.patch.object(pp, "_signed_uri", lambda u: u), \
         mock.patch.object(pp, "_auth_headers", lambda *a, **k: signed.append(1) or {}), \
         mock.patch.object(cp, "_prepub_max_tar", lambda *a: 0), \
         mock.patch.object(cp.time, "sleep"):
      kw = {"identity_path": "el9/x", "identity_hash": "h"} if identity else {}
      if staged:
        jid = cp.submit_staged("http://p", "t", "r", "el9/x", "pre", "cat", **kw)
      else:
        jid = cp.submit_ingest("http://p", "t", "r", "el9/x", tar, **kw)
    return jid, len(posts), len(signed)

  def test_repeated_with_an_identity(self):
    self.assertEqual(self.submit(True, [502, requests.ConnectionError("reset"), 202], True),
                     ("J", 3, 3))
    self.assertEqual(self.submit(False, [502, requests.ConnectTimeout("slow"), 202], True),
                     ("J", 3, 3))

  def test_an_upload_cut_off_is_not_repeated(self):
    with self.assertRaisesRegex(SystemExit, "cut off by prepub"):
      self.submit(False, [requests.ConnectionError("reset"), 202], True)

  def test_once_without(self):
    for staged in (False, True):
      with self.assertRaises(SystemExit):
        self.submit(staged, [502, 202], False)

  def test_a_4xx_is_not_repeated(self):
    with self.assertRaises(SystemExit):
      self.submit(False, [403, 202], True)


if __name__ == "__main__":
  unittest.main()
