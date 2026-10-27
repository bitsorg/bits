#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Replace reference prefixes in a staged package, following CMS's
# find | xargs grep | xargs perl relocation. Mappings are tab-separated pairs,
# one pair per line; paths may contain spaces but not tabs or newlines.
set -eu

root=$1
if [[ -L "$root" || ! -d "$root" ]]; then
  echo "bits: reference relocation requires a real staging directory: $root" >&2
  exit 1
fi

relocate_mappings() {
while IFS=$'\t' read -r source destination; do
  [[ -n "$source" && -n "$destination" ]] || continue
  if [[ "$source" != /* || "$destination" != /* ]]; then
    echo "bits: reference relocation paths must be absolute" >&2
    exit 1
  fi
  [[ "$source" == "$destination" ]] && continue

  # --null, not -Z (decompress in BSD grep); no xargs -r (GNU only). LC_ALL=C: only
  # a NUL byte makes a file binary, whatever the locale and grep.
  find "$root" -type f -exec env LC_ALL=C grep -Il --null -- "$source" {} + \
    | while IFS= read -r -d '' file; do
        BITS_REFERENCE_SOURCE="$source" BITS_REFERENCE_DESTINATION="$destination" \
          perl -p -i -e 's/\Q$ENV{BITS_REFERENCE_SOURCE}\E/$ENV{BITS_REFERENCE_DESTINATION}/g' \
          "$file"
      done
done
}

if [[ "${2:-}" == "--file" ]]; then
  mappings_file=${3:-}
  if [[ ! -f "$mappings_file" || ! -r "$mappings_file" ]]; then
    echo "bits: reference relocation mapping file is missing or unreadable: $mappings_file" >&2
    exit 1
  fi
  relocate_mappings < "$mappings_file"
else
  relocate_mappings <<< "${2:-}"
fi
