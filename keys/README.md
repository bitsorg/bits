# bits trust anchor — signed-reuse public keys

This directory ships **trusted Ed25519 public keys** (PEM) with bits. They are the
root of trust for reusing pre-built artifacts from a remote archive: a client
reuses a remote tarball only when its hash is listed in a manifest whose
signature verifies against one of these keys **and** the tarball's `sha256`
matches (see `docs/REFERENCE.md`, "Artifact resolution order").

- Only **public** keys live here. The private signing key never ships — it lives
  only in the `BITS_SIGN_KEY` CI variable (Protected File) of the manifests repo.
- Keys are versioned by filename, e.g. `public-key-26.0.pem`. `key_id` (sha256 of
  the raw key, first 16 hex) identifies which key produced a given signature; the
  signature envelope carries it.
- Additional trust dirs, most-specific last: `$BITS_TRUST_KEYS` (path-list) and
  `~/.config/bits/keys`.

Generate a keypair (CI keeps the private PEM secret; commit only the public one):

```bash
openssl genpkey -algorithm ed25519 -out bits-sign.pem            # private -> BITS_SIGN_KEY
openssl pkey -in bits-sign.pem -pubout -out keys/public-key-26.0.pem   # public -> commit
# sanity: the committed public key must match the CI private key
openssl pkey -in bits-sign.pem -pubout | diff - keys/public-key-26.0.pem && echo "pair matches"
```

## Key rotation

The trust anchor loads **every** key in this directory, so multiple keys are
trusted at once — rotation is overlap, not a cutover, and never breaks offline
consumers:

1. **Add** the new public key alongside the old one (`public-key-26.1.pem`) and
   ship it (a bits release/pull). Both keys now verify.
2. **Switch** the manifests-repo `BITS_SIGN_KEY` to the new private key. New
   certifications are signed by the new key; already-signed manifests still
   verify under the old one.
3. **Retire** the old public key once every signed manifest in circulation has
   expired (see `--valid-days` / the `expires` field) or been re-signed. Delete
   `public-key-26.0.pem`; only the new key remains trusted.

Because the signed common manifest carries `expires`, an old key can be retired
safely as soon as the last manifest it signed has lapsed — consumers fail closed
on an expired manifest rather than trusting a stale signature.

Sign a manifest and verify it:

```bash
python3 -c 'from bits_helpers import trust; trust.sign_manifest("bits-manifest-latest.json","signing-key.pem")'
python3 -c 'from bits_helpers import trust; print(trust.verify_manifest("bits-manifest-latest.json"))'
```
