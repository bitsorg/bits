# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's security keys (ADR bits-services 0001, section J): enrolment here;
the encrypted passphrase each one unlocks comes later. Kept in passkeys.json under
BITS_ADMIN_DATA (0600, the container's user); never a passphrase nor what decrypts one.
"""

import json
import os
import secrets
import tempfile
import threading
import time
from urllib.parse import urlsplit

from webauthn import generate_registration_options, options_to_json, verify_registration_response
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AttestationConveyancePreference, AuthenticatorSelectionCriteria,
                                       CredentialDeviceType, PublicKeyCredentialDescriptor,
                                       ResidentKeyRequirement, UserVerificationRequirement)

DATA_DIR = os.environ.get("BITS_ADMIN_DATA", "/var/lib/bits-admin")
LABEL_MAX = 64


class Refused(Exception):
    """A security key bits-admin does not accept; the message is for the admin."""


def rp_id(origins):
    """The passkeys' rp_id: the first origin's host name (bits-admin.localhost)."""
    return urlsplit(origins[0]).hostname


def clean_label(text):
    text = "".join(c for c in str(text) if c.isprintable()).strip()[:LABEL_MAX]
    return text or "security key"


class Store:
    """passkeys.json: a list of {id, public_key, sign_count, label, added, aaguid}."""

    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "passkeys.json")
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def _save(self, keys):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".passkeys.")
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(keys, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, key):
        with self.lock:
            keys = self.load()
            if any(k["id"] == key["id"] for k in keys):
                raise Refused("this security key is already enrolled")
            self._save(keys + [key])

    def remove(self, key_id):
        with self.lock:
            keys = self.load()
            rest = [k for k in keys if k["id"] != key_id]
            if len(rest) != len(keys):
                self._save(rest)
            return len(rest) != len(keys)


def registration_options(rp, label, keys):
    """Options for navigator.credentials.create (the page adds the PRF extension).
    Not discoverable: unlock names its keys, and a YubiKey keeps no slot for it."""
    opts = generate_registration_options(
        rp_id=rp, rp_name="bits-admin", user_name=label, user_id=secrets.token_bytes(16),
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in keys])
    return options_to_json(opts), opts.challenge


def verify_registration(rp, origins, credential, challenge, label, now):
    """The key to store, or Refused: a synced passkey (backup eligible) or one without
    PRF cannot guard the passphrase. py_webauthn raises on any other mismatch."""
    v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                      expected_rp_id=rp, expected_origin=origins,
                                      require_user_verification=True)
    if v.credential_backed_up or v.credential_device_type == CredentialDeviceType.MULTI_DEVICE:
        raise Refused("this is a synced passkey (it can be copied to other devices); use a security key")
    prf = ((credential.get("clientExtensionResults") or {}).get("prf") or {})
    if prf.get("enabled") is not True:
        raise Refused("this key or browser offers no PRF, so it could not unlock the signer")
    return {"id": bytes_to_base64url(v.credential_id),
            "public_key": bytes_to_base64url(v.credential_public_key),
            "sign_count": v.sign_count, "label": label, "aaguid": v.aaguid,
            "added": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))}
