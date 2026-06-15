#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import sys
import os
from os.path import dirname, join, abspath
if __name__ == "__main__":
  bitsBuild = join(dirname(abspath(sys.argv[0])), "bitsBuild")
  os.execv(bitsBuild, [ bitsBuild ] + sys.argv[1:])
