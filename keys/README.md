# bits trust anchor — signed-reuse public keys

This directory ships **trusted Ed25519 public keys** (PEM) with bits. They are the
root of trust for reusing pre-built artifacts from a remote archive: a client
reuses a remote tarball only when its hash is listed in a manifest whose
signature verifies against one of these keys **and** the tarball's `sha256`
matches (see `docs/REFERENCE.md`, "Artifact resolution order").

- Only **public** keys live here. The private signing key never ships — it stays
  with the release/CI, or with a bits-console-authorised user for their own area.
- **Rotation:** add the new public key alongside the old one and trust both for
  an overlap window, then remove the old one. `key_id` (sha256 of the raw key,
  16 hex) identifies which key signed.
- Additional trust dirs, most-specific last: `$BITS_TRUST_KEYS` (path-list) and
  `~/.config/bits/keys`.

Generate a keypair (release/CI keeps `signing-key.pem` secret; commit only the
`.pub`):

```bash
openssl genpkey -algorithm ed25519 -out signing-key.pem
openssl pkey -in signing-key.pem -pubout -out keys/release-2026.pub
```

Sign a manifest and verify it:

```bash
python3 -c 'from bits_helpers import trust; trust.sign_manifest("bits-manifest-latest.json","signing-key.pem")'
python3 -c 'from bits_helpers import trust; print(trust.verify_manifest("bits-manifest-latest.json"))'
```
