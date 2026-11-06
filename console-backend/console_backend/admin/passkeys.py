# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-admin's keys (ADR bits-services 0001, section J): security keys and synced
passkeys (Touch ID, Face ID; refused when the site wants security keys only), and the
signer's passphrase encrypted by the browser with a key from each one's PRF output.
Kept in passkeys.json under BITS_ADMIN_DATA (0600, the container's user); never a
passphrase nor what decrypts one."""

import json
import os
import secrets
import tempfile
import threading
import time
from urllib.parse import urlsplit

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AttestationConveyancePreference, AuthenticatorSelectionCriteria,
                                       CredentialDeviceType, PublicKeyCredentialDescriptor,
                                       ResidentKeyRequirement, UserVerificationRequirement)

DATA_DIR = os.environ.get("BITS_ADMIN_DATA", "/var/lib/bits-admin")
LABEL_MAX = 64


class Refused(Exception):
    """A key bits-admin does not accept; the message is for the admin."""


def rp_id(origins):
    """The passkeys' rp_id: the first origin's host name (bits-admin.localhost)."""
    return urlsplit(origins[0]).hostname


def clean_name(text):
    """An admin's name as given at sign-in: printable, at most 40 characters, or ""."""
    return "".join(c for c in str(text) if c.isprintable()).strip()[:40]


def clean_label(text):
    text = "".join(c for c in str(text) if c.isprintable()).strip()[:LABEL_MAX]
    return text or "key"


class Store:
    """passkeys.json: a list of {id, owner, public_key, sign_count, label, added, aaguid, synced},
    once stored, box: {iv, ct, stored}, the passphrase as the browser encrypted it."""

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
                raise Refused("this key is already enrolled")
            self._save(keys + [key])

    def claim_unowned(self, owner):
        """Keys enrolled before keys had owners become *owner*'s; how many."""
        with self.lock:
            keys = self.load()
            unowned = [k for k in keys if not k.get("owner")]
            for k in unowned:
                k["owner"] = owner
            if unowned:
                self._save(keys)
            return len(unowned)

    def get(self, key_id):
        return next((k for k in self.load() if k["id"] == key_id), None)

    def update(self, key_id, **fields):
        with self.lock:
            keys = self.load()
            for k in keys:
                if k["id"] == key_id:
                    k.update(fields)
                    self._save(keys)
                    return True
            return False

    def remove_owner(self, owner):
        """All of *owner*'s keys; how many."""
        with self.lock:
            keys = self.load()
            rest = [k for k in keys if k.get("owner") != owner]
            if len(rest) != len(keys):
                self._save(rest)
            return len(keys) - len(rest)

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


def _synced(v):
    """Backup eligible: the passkey can be copied to the owner's other devices."""
    return v.credential_backed_up or v.credential_device_type == CredentialDeviceType.MULTI_DEVICE


def verify_registration(rp, origins, credential, challenge, label, now, *, security_keys_only):
    """The key to store, or Refused: one without PRF cannot guard the passphrase, nor a
    synced one when the site wants security keys only. py_webauthn raises on the rest."""
    v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                      expected_rp_id=rp, expected_origin=origins,
                                      require_user_verification=True)
    if security_keys_only and _synced(v):
        raise Refused("this is a synced passkey and this site accepts security keys only")
    prf = ((credential.get("clientExtensionResults") or {}).get("prf") or {})
    if prf.get("enabled") is not True:
        raise Refused("this key or browser offers no PRF, so it could not unlock the signer")
    return {"id": bytes_to_base64url(v.credential_id),
            "public_key": bytes_to_base64url(v.credential_public_key),
            "sign_count": v.sign_count, "label": label, "aaguid": v.aaguid, "synced": _synced(v),
            "added": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))}


def authentication_options(rp, keys):
    """Options for navigator.credentials.get with these keys (the page adds PRF when needed)."""
    opts = generate_authentication_options(
        rp_id=rp, allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in keys],
        user_verification=UserVerificationRequirement.REQUIRED)
    return options_to_json(opts), opts.challenge


def verify_assertion(rp, origins, credential, challenge, key, *, security_keys_only):
    """The key's new signature counter; py_webauthn raises on any mismatch."""
    if credential.get("id") != key["id"]:
        raise Refused("the answer came from another key")
    v = verify_authentication_response(
        credential=credential, expected_challenge=challenge, expected_rp_id=rp,
        expected_origin=origins, credential_public_key=base64url_to_bytes(key["public_key"]),
        credential_current_sign_count=key["sign_count"], require_user_verification=True)
    if security_keys_only and _synced(v):
        raise Refused("this is a synced passkey and this site accepts security keys only")
    return v.new_sign_count


def check_box(passphrase, iv, ct):
    """The box to store, {iv, ct} in canonical base64url: a 12-byte AES-GCM nonce and
    the passphrase's ciphertext (its length plus a 16-byte tag), or Refused."""
    if not isinstance(passphrase, str) or not 0 < len(passphrase.encode()) <= 1024:
        raise Refused("no passphrase, or longer than 1024 bytes")
    try:
        iv, ct = base64url_to_bytes(iv), base64url_to_bytes(ct)
    except (TypeError, ValueError):
        raise Refused("the encrypted passphrase is malformed")
    if len(iv) != 12 or len(ct) != len(passphrase.encode()) + 16:
        raise Refused("the encrypted passphrase is malformed")
    return {"iv": bytes_to_base64url(iv), "ct": bytes_to_base64url(ct)}
