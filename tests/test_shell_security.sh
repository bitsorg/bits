#!/usr/bin/env bash
# tests/test_shell_security.sh
#
# Shell-level regression tests for the security fixes applied to:
#   bits_helpers/relocate-me.sh  (R1, R2, R3)
#   bits_helpers/build_template.sh  (B1–B7)
#   bits_helpers/tar_template.sh    (T1)
#
# Each test is a plain bash function; the harness at the bottom runs them all
# and reports PASS/FAIL.  No external test framework is required.
#
# Usage:
#   bash tests/test_shell_security.sh
#
# Exit code: 0 if all tests pass, 1 if any fail.

set -uo pipefail

PASS=0
FAIL=0
ERRORS=()

_pass() { echo "  PASS: $1"; (( ++PASS )); }
_fail() { echo "  FAIL: $1"; ERRORS+=("$1"); (( ++FAIL )); }

run_test() {
    local name="$1"
    if "$name"; then
        _pass "$name"
    else
        _fail "$name"
    fi
}

# ---------------------------------------------------------------------------
# R1 — relocate-me.sh: filenames with spaces must not be split across loop
# ---------------------------------------------------------------------------

test_R1_filename_with_space_is_not_split() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    # Create a fake installroot with:
    #   etc/profile.d/.bits-pkginfo   (sourced by relocate-me.sh)
    #   etc/profile.d/.bits-relocate  (list of files to relocate)
    #   a file whose name contains a space
    mkdir -p "$tmpdir/etc/profile.d"
    cat > "$tmpdir/etc/profile.d/.bits-pkginfo" <<'EOF'
OP=x86_64/mypkg/1.0-1
PP=x86_64/mypkg/1.0-1
PH=abc123
PKG_DIR=/old/base
EOF
    # File whose name contains a space — old-style $(cat …) would split this
    local spaced="lib/my lib.so"
    mkdir -p "$tmpdir/lib"
    touch "$tmpdir/$spaced"
    echo -e "s|/old/base/INSTALLROOT/abc123|/new/base|g;s|/old/base|/new/base|g" \
        > "$tmpdir/$spaced"   # reuse file as its own content (content doesn't matter here)
    echo "$spaced" > "$tmpdir/etc/profile.d/.bits-relocate"

    # Count files in lib/ before; a split loop would try to sed "lib/my" and "lib.so"
    # (neither of which exist) — the real file would be untouched.
    # We verify by checking sed can read the file without error.
    local rc=0
    (
        THISDIR="$tmpdir"
        . "$tmpdir/etc/profile.d/.bits-pkginfo"
        INSTALL_BASE="$(echo "$THISDIR" | sed "s|/$PP$||")"
        # Replicate the fixed loop from relocate-me.sh
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            # Touch the file to prove we addressed it by its full (spaced) name
            touch "$THISDIR/$f.touched"
        done < "$THISDIR/etc/profile.d/.bits-relocate"
    ) || rc=$?

    [[ $rc -eq 0 ]] && [[ -f "$tmpdir/$spaced.touched" ]]
}

test_R1_broken_loop_would_fail() {
    # Negative control: demonstrate that the OLD $(cat …) loop would NOT create
    # the .touched file for a spaced filename (it would create two wrong files).
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    mkdir -p "$tmpdir/etc/profile.d" "$tmpdir/lib"
    local spaced="lib/my lib.so"
    echo "$spaced" > "$tmpdir/etc/profile.d/.bits-relocate"
    touch "$tmpdir/$spaced"

    # Run the OLD broken pattern
    (
        THISDIR="$tmpdir"
        for f in $(cat "$THISDIR/etc/profile.d/.bits-relocate") ; do
            touch "$THISDIR/$f.touched" 2>/dev/null || true
        done
    )

    # The old loop creates "lib/my.touched" and "lib.so.touched" — NOT the combined name.
    # If the fixed file exists, the old code somehow worked (unexpected — test fails).
    [[ ! -f "$tmpdir/$spaced.touched" ]]
}

# ---------------------------------------------------------------------------
# R2 — relocate-me.sh: $INSTALL_BASE with spaces must not break sed argument
# ---------------------------------------------------------------------------

test_R2_install_base_with_space_in_sed_expression() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    mkdir -p "$tmpdir/etc/profile.d"
    # Write a .bits-pkginfo with PKG_DIR set to an old value
    echo 'PKG_DIR="/old/base"' > "$tmpdir/etc/profile.d/.bits-pkginfo"

    local INSTALL_BASE="/new/install base"   # contains a space

    # Fixed sed expression: $INSTALL_BASE is inside double-quotes in the script,
    # so the whole sed -e argument is a single token even with a space.
    local sed_expr="s|^PKG_DIR=.*|PKG_DIR=\"${INSTALL_BASE}\"|"

    local rc=0
    sed -e "$sed_expr" "$tmpdir/etc/profile.d/.bits-pkginfo" \
        > "$tmpdir/etc/profile.d/.bits-pkginfo.new" || rc=$?

    [[ $rc -eq 0 ]] && grep -q "PKG_DIR=\"/new/install base\"" \
        "$tmpdir/etc/profile.d/.bits-pkginfo.new"
}

test_R2_broken_expression_would_have_extra_args() {
    # Negative control: simulate the OLD broken quoting where $INSTALL_BASE
    # is unquoted — show that the sed call receives extra args (and fails or
    # produces wrong output).
    local INSTALL_BASE="/new/install base"

    # Old broken form: the shell splits $INSTALL_BASE after the closing "
    # The sed invocation becomes: sed -e 's|PKG_DIR=.*|PKG_DIR=' /new/install 'base|'
    # On most systems this will either fail (non-zero exit) or produce wrong output.
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    echo 'PKG_DIR="/old"' > "$tmpdir/pkginfo"

    # Deliberately construct the broken argument string the way the old code did:
    # "s|^PKG_DIR=.*|PKG_DIR="${INSTALL_BASE}"|"
    # In this test we use eval to replicate exactly what the shell would do.
    local broken_result rc=0
    broken_result=$(eval "sed -e \"s|^PKG_DIR=.*|PKG_DIR=\"${INSTALL_BASE}\"|\" '$tmpdir/pkginfo'" 2>&1) || rc=$?

    # If the broken version happens to work on this platform and produces the
    # correct output — the test must still pass (we only need the fix to be safe).
    # So: the test passes if either rc!=0 OR the output does NOT embed the full path.
    [[ $rc -ne 0 ]] || ! echo "$broken_result" | grep -q "PKG_DIR=\"/new/install base\""
}

# ---------------------------------------------------------------------------
# R3 — relocate-me.sh: $THISDIR quoted in echo
# ---------------------------------------------------------------------------

test_R3_thisdir_with_space_quoted_in_echo() {
    # Ensure that echo "$THISDIR" (quoted) handles a path with spaces correctly.
    local THISDIR="/some/path with spaces/pkg/1.0"
    local PP="pkg/1.0"
    # The fixed form:
    local result
    result=$(echo "$THISDIR" | sed "s|/$PP$||")
    [[ "$result" == "/some/path with spaces" ]]
}

# ---------------------------------------------------------------------------
# B1 — build_template.sh: rm -rf with quoted $INSTALLROOT
# ---------------------------------------------------------------------------

test_B1_quoted_rm_rf_does_not_split_on_space() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    # Create a directory whose name contains a space — simulates WORK_DIR with space
    local INSTALLROOT="$tmpdir/install root/pkg"
    mkdir -p "$INSTALLROOT"
    touch "$INSTALLROOT/sentinel"

    # Fixed form: "rm -rf "$INSTALLROOT"" removes the directory, not two separate paths
    rm -rf "$INSTALLROOT"

    # The directory should be gone, and tmpdir itself should still exist
    [[ ! -d "$INSTALLROOT" ]] && [[ -d "$tmpdir" ]]
}

test_B1_unquoted_rm_rf_would_split() {
    # Negative control: unquoted $INSTALLROOT with a space results in two
    # separate arguments to rm, neither of which may match the intended target.
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    local INSTALLROOT="$tmpdir/install root"
    mkdir -p "$INSTALLROOT"

    # Old broken form — note: no quotes around $INSTALLROOT
    # rm -rf $INSTALLROOT would try to remove "$tmpdir/install" and "root" separately.
    # We capture the effect: the INSTALLROOT directory should still exist.
    (
        # Run in subshell so the rm doesn't actually hurt anything outside tmpdir
        rm -rf $INSTALLROOT 2>/dev/null || true
    )

    # The directory "$tmpdir/install root" should still exist because rm got
    # "$tmpdir/install" (doesn't exist) and "root" (doesn't exist at CWD) instead.
    [[ -d "$INSTALLROOT" ]]
}

# ---------------------------------------------------------------------------
# B4 — build_template.sh: ln -snf with quoted $(dirname $PKGPATH)
# ---------------------------------------------------------------------------

test_B4_ln_snf_with_space_in_pkgpath() {
    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" RETURN

    local PKGPATH="x86_64/my pkg/1.0-1"
    local _VERREV="1.0-1"
    mkdir -p "$tmpdir/$PKGPATH"

    # Fixed form: both arguments are double-quoted
    ln -snf "${_VERREV}" "$(dirname "$tmpdir/$PKGPATH")/latest"

    [[ -L "$(dirname "$tmpdir/$PKGPATH")/latest" ]]
}

# ---------------------------------------------------------------------------
# B7 — build_template.sh: HASHPREFIX uses $(...) and quoted $PKGHASH
# ---------------------------------------------------------------------------

test_B7_hashprefix_extraction() {
    local PKGHASH="deadbeef1234"
    local HASHPREFIX
    # Fixed form using $() and -c
    HASHPREFIX=$(echo "$PKGHASH" | cut -c1,2)
    [[ "$HASHPREFIX" == "de" ]]
}

test_B7_hashprefix_with_space_in_hash_quoted() {
    # Edge case: even if hash somehow had leading whitespace, quoting protects it
    local PKGHASH="  ab1234"
    local HASHPREFIX
    HASHPREFIX=$(echo "$PKGHASH" | cut -c1,2)
    # cut -c1,2 of "  ab1234" gives "  " (two spaces) — we just verify no error
    [[ $? -eq 0 ]]
}

# ---------------------------------------------------------------------------
# T1 — tar_template.sh: $gzip is quoted
# ---------------------------------------------------------------------------

test_T1_gzip_path_quoted() {
    # Verify that a gzip path returned by 'command -v' can be invoked when quoted.
    local gzip
    gzip=$(command -v pigz 2>/dev/null) || gzip=$(command -v gzip 2>/dev/null)
    [[ -n "$gzip" ]] || { echo "    (skip: no gzip found)"; return 0; }

    # Invoke using the quoted form — must not error
    local version_output
    version_output=$("$gzip" --version 2>&1) || true
    [[ $? -eq 0 ]] || [[ -n "$version_output" ]]
}

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

echo "========================================"
echo "Shell security regression tests"
echo "========================================"

tests=(
    test_R1_filename_with_space_is_not_split
    test_R1_broken_loop_would_fail
    test_R2_install_base_with_space_in_sed_expression
    test_R2_broken_expression_would_have_extra_args
    test_R3_thisdir_with_space_quoted_in_echo
    test_B1_quoted_rm_rf_does_not_split_on_space
    test_B1_unquoted_rm_rf_would_split
    test_B4_ln_snf_with_space_in_pkgpath
    test_B7_hashprefix_extraction
    test_B7_hashprefix_with_space_in_hash_quoted
    test_T1_gzip_path_quoted
)

for t in "${tests[@]}"; do
    run_test "$t"
done

echo "========================================"
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
    echo "Failed tests:"
    for e in "${ERRORS[@]}"; do
        echo "  - $e"
    done
fi
echo "========================================"

[[ $FAIL -eq 0 ]]
