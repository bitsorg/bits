# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
# The tests see bits' own site configuration only, not this machine's.
# tox (unittest discover) does the same through BITS_SITE_ISOLATED in tox.ini.
import os

os.environ["BITS_SITE_ISOLATED"] = "1"
# ... and no git mirror or sources store: CERN's (store.git, store.sources) are never
# asked from the tests.
os.environ["BITS_GIT_MIRROR"] = ""
os.environ["BITS_SOURCE_STORE"] = ""
os.environ["BITS_SOURCE_STORE_RESTRICTED"] = ""
os.environ["BITS_CRATES_REGISTRY"] = ""
os.environ.pop("BITS_CONTAINER_ENGINE", None)   # docker, unless a test chooses
