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
