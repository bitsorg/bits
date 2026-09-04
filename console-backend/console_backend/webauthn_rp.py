# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""WebAuthn relying-party helpers (py_webauthn).

Registration enrols a passkey for a GitLab user. Authentication is the
per-operation approval: the challenge IS the manifest digest, so a valid
assertion proves a human on an enrolled device approved *these exact bytes*.
"""

from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                       PublicKeyCredentialDescriptor,
                                       UserVerificationRequirement)


def _descriptors(creds):
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in creds]


def registration_options(settings, user, existing):
    """Return ``(options_json, challenge_bytes)`` for navigator.credentials.create."""
    uv = (UserVerificationRequirement.REQUIRED if settings.webauthn_require_uv
          else UserVerificationRequirement.PREFERRED)
    opts = generate_registration_options(
        rp_id=settings.rp_id, rp_name=settings.rp_name,
        user_name=user, user_id=user.encode("utf-8"),
        authenticator_selection=AuthenticatorSelectionCriteria(user_verification=uv),
        exclude_credentials=_descriptors(existing))
    return options_to_json(opts), opts.challenge


def verify_registration(settings, credential_json, expected_challenge):
    """Verify an attestation and return a storable credential dict."""
    v = verify_registration_response(
        credential=credential_json, expected_challenge=expected_challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.rp_origins or settings.rp_origin,
        require_user_verification=settings.webauthn_require_uv)
    return {"id": bytes_to_base64url(v.credential_id),
            "public_key": bytes_to_base64url(v.credential_public_key),
            "sign_count": v.sign_count}


def authentication_options(settings, challenge, user_creds):
    """Return options JSON for navigator.credentials.get; *challenge* is the
    manifest digest bytes (content binding)."""
    opts = generate_authentication_options(
        rp_id=settings.rp_id, challenge=challenge,
        allow_credentials=_descriptors(user_creds),
        user_verification=UserVerificationRequirement.PREFERRED)
    return options_to_json(opts)


def verify_authentication(settings, credential_json, expected_challenge, cred):
    """Verify an assertion over *expected_challenge* (the digest); return the new
    sign counter. Raises on any mismatch."""
    v = verify_authentication_response(
        credential=credential_json, expected_challenge=expected_challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.rp_origins or settings.rp_origin,
        credential_public_key=base64url_to_bytes(cred["public_key"]),
        credential_current_sign_count=cred["sign_count"],
        require_user_verification=settings.webauthn_require_uv)
    return v.new_sign_count
