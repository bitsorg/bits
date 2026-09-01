# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Security review M4: the shipped key-policy.json is fail-closed — a signing key
not explicitly enrolled is denied every group (via the reserved "default": []
entry), while the enrolled bits-admin key ("*") stays authorised."""

import json
import os
import tempfile
import unittest
from unittest import mock

from bits_helpers import trust

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEYS_DIR = os.path.join(_ROOT, "keys")
_ADMIN_KID = "265bf1902ea0d4d9"


class KeyPolicyStrictTest(unittest.TestCase):
    def test_key_authorized_logic_with_strict_default(self):
        policy = {_ADMIN_KID: {"*"}, "default": set()}
        # enrolled "*" key: authorised for any group
        self.assertTrue(trust.key_authorized(_ADMIN_KID, "lcg", policy))
        self.assertTrue(trust.key_authorized(_ADMIN_KID, "common", policy))
        # unlisted key: denied every group (fail-closed via default: [])
        self.assertFalse(trust.key_authorized("deadbeefdeadbeef", "lcg", policy))
        self.assertFalse(trust.key_authorized("deadbeefdeadbeef", None, policy))

    def test_no_default_is_permissive(self):
        # Without a "default" entry an unlisted key is unrestricted (the old,
        # backward-compatible behaviour) — this is exactly what "default": []
        # closes, so the two must differ.
        policy = {_ADMIN_KID: {"*"}}
        self.assertTrue(trust.key_authorized("deadbeefdeadbeef", "lcg", policy))

    def test_shipped_policy_is_strict(self):
        policy = trust.load_key_policy(dirs=[_KEYS_DIR])
        self.assertIsNotNone(policy, "keys/key-policy.json must be present")
        self.assertIn("default", policy)
        self.assertEqual(policy["default"], set(), "shipped default must be [] (strict)")
        self.assertIn(_ADMIN_KID, policy)
        # concretely: the admin key is authorised, an unlisted key is denied.
        self.assertTrue(trust.key_authorized(_ADMIN_KID, "lcg", policy))
        self.assertFalse(trust.key_authorized("deadbeefdeadbeef", "lcg", policy))


class KeyPolicyDropDiagnosticTest(unittest.TestCase):
    """A verified signing key that key-policy.json does not authorise for an
    entry's group has its entries dropped — but with a diagnostic warning, not a
    silent no-reuse (mitigates the fail-closed silent-drop failure mode)."""

    def _manifest(self, group):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "common.json")
        with open(path, "w") as fh:
            json.dump({"packages": [
                {"group": group, "hash": "h1", "tarball_sha256": "sha256:aa"}]}, fh)
        return path

    def test_policy_denied_drops_and_warns(self):
        path = self._manifest("lcg")
        denying = {"deadbeefdeadbeef": {"lcg"}, "default": set()}  # signer kid not listed
        with mock.patch.object(trust, "verify_manifest", return_value="ffffffffffffffff"), \
             mock.patch.object(trust, "load_key_policy", return_value=denying), \
             mock.patch("bits_helpers.log.warning") as w:
            kid, entries = trust._verified_entries(path, None, None, None, None)
        self.assertEqual(entries, [])
        self.assertTrue(w.called, "a policy-denied verified key must be diagnosed")

    def test_authorized_key_no_warning(self):
        path = self._manifest("lcg")
        allowing = {"ffffffffffffffff": {"*"}, "default": set()}
        with mock.patch.object(trust, "verify_manifest", return_value="ffffffffffffffff"), \
             mock.patch.object(trust, "load_key_policy", return_value=allowing), \
             mock.patch("bits_helpers.log.warning") as w:
            kid, entries = trust._verified_entries(path, None, None, None, None)
        self.assertEqual(len(entries), 1)
        self.assertFalse(w.called)


if __name__ == "__main__":
    unittest.main()
