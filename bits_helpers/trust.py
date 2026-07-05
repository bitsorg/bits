# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manifest signing and verification for trusted binary reuse.

A build manifest (bits-manifest-latest.json) lists every package's content hash
and tarball_sha256. Signing it turns the store into a *trusted* source: a client
reuses a remote tarball only when its hash is in a signature-verified manifest
and its sha256 matches (see docs/REFERENCE.md, "Artifact resolution order").

Scheme (shared with the cvmfs-bits publish side): Ed25519, keys in PEM. The
signature is a small JSON envelope carrying the algorithm and a key id so several
public keys can be trusted at once (rotation) without changing the client.

Trust anchor: Ed25519 *public* keys shipped with bits (the ``keys/`` directory
next to the package) plus any dirs in $BITS_TRUST_KEYS. The private signing key
never ships; it stays with the release/CI or a bits-console-authorised user.
"""

import base64
import glob
import hashlib
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

_ALG = "ed25519"


def _pub_bytes(pub: Ed25519PublicKey) -> bytes:
  return pub.public_bytes(serialization.Encoding.Raw,
                          serialization.PublicFormat.Raw)


def key_id(pub: Ed25519PublicKey) -> str:
  """Short, stable id for a public key: sha256 of its raw bytes, 16 hex chars."""
  return hashlib.sha256(_pub_bytes(pub)).hexdigest()[:16]


def load_private_key(pem_path: str) -> Ed25519PrivateKey:
  with open(pem_path, "rb") as fh:
    key = serialization.load_pem_private_key(fh.read(), password=None)
  if not isinstance(key, Ed25519PrivateKey):
    raise ValueError("%s is not an Ed25519 private key" % pem_path)
  return key


def load_public_key(data) -> Ed25519PublicKey:
  """Load a public key from PEM bytes/str or a file path."""
  if isinstance(data, str) and os.path.isfile(data):
    with open(data, "rb") as fh:
      data = fh.read()
  if isinstance(data, str):
    data = data.encode()
  key = serialization.load_pem_public_key(data)
  if not isinstance(key, Ed25519PublicKey):
    raise ValueError("not an Ed25519 public key")
  return key


def default_trust_dirs() -> list:
  """Directories searched for trusted public keys, most-specific last."""
  dirs = [os.path.join(os.path.dirname(__file__), os.pardir, "keys")]
  env = os.environ.get("BITS_TRUST_KEYS", "")
  dirs += [d for d in env.split(os.pathsep) if d]
  dirs.append(os.path.expanduser("~/.config/bits/keys"))
  return [os.path.abspath(d) for d in dirs]


def load_trusted_keys(dirs=None) -> dict:
  """Return {key_id: Ed25519PublicKey} for every *.pem/*.pub under *dirs*."""
  trusted = {}
  for d in (dirs if dirs is not None else default_trust_dirs()):
    if not os.path.isdir(d):
      continue
    for path in sorted(glob.glob(os.path.join(d, "*.pem"))
                       + glob.glob(os.path.join(d, "*.pub"))):
      try:
        pub = load_public_key(path)
      except Exception:
        continue
      trusted[key_id(pub)] = pub
  return trusted


def sign_bytes(data: bytes, priv: Ed25519PrivateKey) -> dict:
  """Detached signature envelope over *data*."""
  sig = priv.sign(data)
  return {"alg": _ALG,
          "key_id": key_id(priv.public_key()),
          "sig": base64.b64encode(sig).decode("ascii")}


def verify_bytes(data: bytes, envelope: dict, trusted: dict):
  """Verify *envelope* over *data* against *trusted* {key_id: pub}.

  Returns the signing key_id on success, or None. Tries the enveloped key_id
  first, then every trusted key (so an unlabelled/rotated signature still
  verifies as long as some trusted key made it).
  """
  if not isinstance(envelope, dict) or envelope.get("alg") != _ALG:
    return None
  try:
    sig = base64.b64decode(envelope["sig"])
  except Exception:
    return None
  kid = envelope.get("key_id")
  candidates = []
  if kid in trusted:
    candidates.append((kid, trusted[kid]))
  candidates += [(k, v) for k, v in trusted.items() if k != kid]
  for k, pub in candidates:
    try:
      pub.verify(sig, data)
      return k
    except InvalidSignature:
      continue
  return None


def sign_manifest(manifest_path: str, key_pem_path: str, sig_path=None) -> str:
  """Sign a manifest file; write the envelope to *sig_path* (default: +'.sig')."""
  priv = load_private_key(key_pem_path)
  with open(manifest_path, "rb") as fh:
    data = fh.read()
  envelope = sign_bytes(data, priv)
  sig_path = sig_path or (manifest_path + ".sig")
  with open(sig_path, "w") as fh:
    json.dump(envelope, fh)
  return sig_path


def verify_manifest(manifest_path: str, sig_path=None, dirs=None):
  """Verify a manifest against its .sig and the trust anchor.

  Returns the signing key_id, or None if the signature is missing/untrusted.
  """
  sig_path = sig_path or (manifest_path + ".sig")
  if not os.path.isfile(sig_path):
    return None
  try:
    with open(sig_path) as fh:
      envelope = json.load(fh)
    with open(manifest_path, "rb") as fh:
      data = fh.read()
  except Exception:
    return None
  return verify_bytes(data, envelope, load_trusted_keys(dirs))


def load_key_policy(dirs=None):
  """Return the key->groups signing policy, or None if no policy is configured.

  Looks for ``key-policy.json`` in the trust dirs (most-specific last wins per
  key). The file maps ``key_id -> [groups]``; the group ``"*"`` grants a key
  authority over every group (the overall bits-admin key). When *no* policy file
  exists anywhere, returns None and callers impose no per-key restriction
  (backward compatible). Example::

      {"265bf1902ea0d4d9": ["*"], "ab12…": ["lcg", "common"]}
  """
  policy = None
  for d in (dirs if dirs is not None else default_trust_dirs()):
    path = os.path.join(d, "key-policy.json")
    if not os.path.isfile(path):
      continue
    try:
      with open(path) as fh:
        data = json.load(fh)
    except Exception:
      continue
    if not isinstance(data, dict):
      continue
    policy = policy or {}
    for kid, groups in data.items():
      if isinstance(groups, (list, tuple)):
        policy[str(kid)] = {str(g) for g in groups}
  return policy


def key_authorized(key_id, group, policy) -> bool:
  """Whether *key_id* may certify an entry of *group* under *policy*.

  *policy* None (no policy file) -> unrestricted. A key listed in the policy is
  restricted to its groups (``"*"`` = every group; an empty list = none; an
  untagged entry counts as ``common``). A key NOT listed falls back to the
  reserved ``"default"`` entry if present, else is unrestricted — so adding a
  policy only restricts the keys you explicitly enrol. Set ``"default": []`` to
  make an enrolled policy strict (deny any unlisted key).
  """
  if policy is None:
    return True
  allowed = policy.get(str(key_id))
  if allowed is None:
    allowed = policy.get("default")
  if allowed is None:
    return True
  if "*" in allowed:
    return True
  return (str(group) if group else "common") in allowed


def accepts_group(entry_group, accept_groups) -> bool:
  """Group-policy predicate for one common-manifest entry.

  *accept_groups* is the caller's trust policy (the groups it opts into, e.g. its
  own group). The base/``common`` layer is always trusted, and an untagged entry
  is treated as base — so legacy single-group manifests keep working. Passing
  ``accept_groups=None`` disables filtering entirely (trust every entry).
  """
  if accept_groups is None:
    return True
  accept = {str(g) for g in accept_groups} | {"common"}
  return (str(entry_group) if entry_group else "common") in accept


def is_expired(manifest_data, now=None) -> bool:
  """True if the manifest carries an ``expires`` timestamp that is in the past.

  Backward-compatible: a manifest without ``expires`` never expires. An
  unparseable ``expires`` is treated as expired (fail-closed).
  """
  raw = (manifest_data or {}).get("expires") if isinstance(manifest_data, dict) else None
  if not raw:
    return False
  import datetime
  now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
  try:
    exp = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
  except (ValueError, TypeError):
    return True
  return now > exp


def trusted_index(manifest_path: str, sig_path=None, dirs=None, accept_groups=None,
                  now=None):
  """Verify a signed manifest and return its trusted reuse index.

  Returns ``(key_id, {content_hash: tarball_sha256})`` on success, or
  ``(None, {})`` when the signature is missing or untrusted. This is the
  authoritative index for trusted reuse: a remote tarball is reused only when
  its content hash is a key here AND the downloaded tarball's sha256 matches
  the value (fail-closed).

  *accept_groups* applies the group trust policy (see :func:`accepts_group`):
  ``None`` (default) trusts every signed entry; a set/list keeps only entries in
  those groups plus the always-trusted ``common`` base. A manifest whose
  ``expires`` has passed is rejected wholesale (fail-closed, offline anti-replay).
  """
  kid = verify_manifest(manifest_path, sig_path, dirs)
  if not kid:
    return None, {}
  try:
    with open(manifest_path) as fh:
      data = json.load(fh)
  except Exception:
    return None, {}
  if is_expired(data, now):
    return None, {}
  # Per-key group binding: a signing key vouches only for the groups it is
  # authorised for (policy file opt-in; absent = no restriction).
  policy = load_key_policy(dirs)
  index = {}
  for e in data.get("packages", []):
    h, sha, grp = e.get("hash"), e.get("tarball_sha256"), e.get("group")
    if (h and sha and accepts_group(grp, accept_groups)
        and key_authorized(kid, grp, policy)):
      index[h] = sha
  return kid, index
