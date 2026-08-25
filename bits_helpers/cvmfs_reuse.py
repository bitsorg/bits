# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# The legacy ADR-0001 cvmfs:// build_id-graft reuse (available_build_ids,
# select_build_id, graftable_match) was removed in the Step 5 cleanup. Deployed
# components are now reused via --reuse-from (module overlay); see
# bits_helpers/cvmfs_import.py and cvmfs_layout.py. This module is empty and
# scheduled for `git rm`.
