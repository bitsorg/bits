# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
# The tests see bits' own site configuration only, not this machine's.
# tox (unittest discover) does the same through BITS_SITE_ISOLATED in tox.ini.
import os

os.environ["BITS_SITE_ISOLATED"] = "1"
