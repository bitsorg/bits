#!/usr/bin/env bash
# verify-deterministic-tarball.sh
#
# Verify the PROPOSED deterministic packaging for bits (finding R1): does it make
# two independent "builds" of byte-identical CONTENT produce a byte-identical
# .tar.gz on THIS platform?  Run it on each build platform (Linux/GNU tar and
# macOS/bsdtar) — certification is per-architecture, so per-platform
# reproducibility is what matters.
#
# It builds two trees with identical content but DIFFERENT metadata (mtimes,
# creation order) — the very things that vary between build nodes — then:
#   1. packs both with the CURRENT approach   -> expected: sha DIFFERS (the bug)
#   2. packs both with the PROPOSED approach   -> expected: sha IDENTICAL (fixed)
#   3. shows the compressor matters: gzip vs pigz differ, gzip -n is stable
#
# Exit status: 0 if the proposed approach is deterministic here, 1 otherwise.
set -u

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t bitsdet)
trap 'rm -rf "$TMP"' EXIT
fail=0

# ---- platform detection -----------------------------------------------------
if tar --version 2>/dev/null | grep -qi 'GNU tar'; then TARKIND=gnu; else TARKIND=bsd; fi
if command -v sha256sum >/dev/null 2>&1; then SHA() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum   >/dev/null 2>&1; then SHA() { shasum -a 256 "$1" | cut -d' ' -f1; }
else echo "no sha256 tool"; exit 2; fi
echo "platform : $(uname -s) $(uname -m)"
echo "tar      : $TARKIND — $(tar --version 2>/dev/null | head -1)"
echo "pigz     : $(command -v pigz || echo 'not installed')"
echo

# ---- two content-identical trees with different metadata --------------------
make_tree() {   # $1=dir  $2=order(1|2)
  mkdir -p "$1/bin" "$1/lib" "$1/share/doc"
  if [ "$2" = 1 ]; then
    printf 'AAA' >"$1/bin/a"; printf 'BBB' >"$1/lib/libb.so"; printf 'CCC' >"$1/share/doc/c.txt"
  else   # create in a different order (varies readdir/archive order)
    printf 'CCC' >"$1/share/doc/c.txt"; printf 'BBB' >"$1/lib/libb.so"; printf 'AAA' >"$1/bin/a"
  fi
  ln -sf a "$1/bin/link"
}
mkdir -p "$TMP/A" "$TMP/B"
make_tree "$TMP/A" 1
sleep 1                                   # guarantee different real mtimes
make_tree "$TMP/B" 2
# push B's mtimes somewhere else entirely, to be sure they differ from A
find "$TMP/B" -exec touch -t 202001020304 {} + 2>/dev/null

# ---- CURRENT packaging (what build_template.sh does today) ------------------
pack_current() {  # $1=tree  $2=out
  ( cd "$1" && tar -cf - . ) | gzip -c > "$2"
}

# ---- PROPOSED deterministic packaging ---------------------------------------
# Normalise mtimes portably (bsdtar has no --mtime), archive a SORTED member
# list with no recursion and zeroed numeric owner/group, then a PINNED gzip -n.
pack_det() {      # $1=tree  $2=out  $3=compressor-cmd (default: gzip -n)
  comp=${3:-gzip -n}
  find "$1" -exec touch -h -t 197001010000 {} + 2>/dev/null || \
    find "$1" -exec touch    -t 197001010000 {} + 2>/dev/null
  ( cd "$1" && find . -print | LC_ALL=C sort > "$TMP/list" )
  if [ "$TARKIND" = gnu ]; then
    ( cd "$1" && tar --no-recursion --owner=0 --group=0 --numeric-owner \
                     --mtime='@0' -T "$TMP/list" -cf - ) | $comp -c > "$2"
  else
    ( cd "$1" && tar --no-recursion --uid 0 --gid 0 --numeric-owner \
                     -T "$TMP/list" -cf - ) | $comp -c > "$2"
  fi
}

check() {  # $1=label  $2=fileA  $3=fileB  $4=want(same|diff)  — affects RESULT
  a=$(SHA "$2"); b=$(SHA "$3")
  if [ "$a" = "$b" ]; then got=same; else got=diff; fi
  if [ "$got" = "$4" ]; then res="PASS"; else res="FAIL"; fail=1; fi
  printf '  %-42s %s  (%s)\n' "$1" "$res" "$got"
  [ "$got" = diff ] && printf '      A=%s\n      B=%s\n' "$a" "$b"
}

report() {  # $1=label  $2=fileA  $3=fileB  — informational only, never fails
  a=$(SHA "$2"); b=$(SHA "$3")
  if [ "$a" = "$b" ]; then got=same; else got=diff; fi
  printf '  %-42s ....  (%s)\n' "$1" "$got"
}

det_tar() {  # $1=tree -> deterministic tar stream on stdout
  find "$1" -exec touch -h -t 197001010000 {} + 2>/dev/null || \
    find "$1" -exec touch    -t 197001010000 {} + 2>/dev/null
  ( cd "$1" && find . -print | LC_ALL=C sort > "$TMP/dl" )
  if [ "$TARKIND" = gnu ]; then
    ( cd "$1" && tar --no-recursion --owner=0 --group=0 --numeric-owner \
                     --mtime='@0' -T "$TMP/dl" -cf - )
  else
    ( cd "$1" && tar --no-recursion --uid 0 --gid 0 --numeric-owner \
                     -T "$TMP/dl" -cf - )
  fi
}

echo "== 1. current approach (expected: DIFFERS — demonstrates the bug) =="
pack_current "$TMP/A" "$TMP/cur.a.tgz"; pack_current "$TMP/B" "$TMP/cur.b.tgz"
check "current tar|gzip, two builds" "$TMP/cur.a.tgz" "$TMP/cur.b.tgz" diff

echo "== 2. proposed approach (expected: IDENTICAL — the fix) =="
pack_det "$TMP/A" "$TMP/det.a.tgz"; pack_det "$TMP/B" "$TMP/det.b.tgz"
check "deterministic tar + gzip -n" "$TMP/det.a.tgz" "$TMP/det.b.tgz" same

echo "== 3. compressor: gzip -n is deterministic; pigz can vary =="
# gzip -n twice -> identical (this IS a pass criterion).
pack_det "$TMP/A" "$TMP/g1.tgz" "gzip -n"; pack_det "$TMP/A" "$TMP/g2.tgz" "gzip -n"
check "gzip -n vs gzip -n (same input)" "$TMP/g1.tgz" "$TMP/g2.tgz" same
# The rest is INFORMATIONAL (never changes RESULT): for a SMALL input pigz emits
# a single block == gzip. The real hazard is LARGE inputs, where pigz's block
# layout depends on the THREAD COUNT (the node's core count) — so two nodes with
# pigz but different -p produce different bytes. Demonstrate on a multi-MB payload.
if command -v pigz >/dev/null 2>&1; then
  pack_det "$TMP/A" "$TMP/pz.tgz" "pigz -n"
  report "small: gzip -n vs pigz -n"    "$TMP/g1.tgz" "$TMP/pz.tgz"
  mkdir -p "$TMP/big"; head -c 8000000 /dev/urandom | base64 > "$TMP/big/data"
  det_tar "$TMP/big" | gzip -n     -c > "$TMP/b.gzip"
  det_tar "$TMP/big" | pigz -n -p1 -c > "$TMP/b.p1a"
  det_tar "$TMP/big" | pigz -n -p1 -c > "$TMP/b.p1b"
  det_tar "$TMP/big" | pigz -n -p4 -c > "$TMP/b.p4"
  report "large: pigz -p1 vs pigz -p1"  "$TMP/b.p1a" "$TMP/b.p1b"
  report "large: pigz -p1 vs pigz -p4"  "$TMP/b.p1a" "$TMP/b.p4"
  report "large: gzip -n vs pigz -p1"   "$TMP/b.gzip" "$TMP/b.p1a"
  echo "  -> a 'diff' on any large: line means the compressor + thread count must"
  echo "     be pinned (use gzip -n, or pigz -n with a fixed -p, on every node)."
else
  echo "  (pigz not installed — install it to see the large-input / thread divergence)"
fi

echo
if [ "$fail" = 0 ]; then
  echo "RESULT: deterministic packaging is REPRODUCIBLE on this platform."
else
  echo "RESULT: NOT fully reproducible here — see FAIL lines above (this platform"
  echo "        needs different flags; report which line failed)."
fi
exit $fail
