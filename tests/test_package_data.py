# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Every file bits_helpers needs at run time is installed with it."""

import ast
from fnmatch import fnmatch
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PackageDataTest(unittest.TestCase):
    def test_non_python_files_are_package_data(self):
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            m = re.search(r"^bits_helpers = (\[.*\])$", f.read(), re.M)
        patterns = ast.literal_eval(m.group(1))
        top = os.path.join(ROOT, "bits_helpers")
        missing = []
        for directory, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                rel = os.path.relpath(os.path.join(directory, name), top)
                if not rel.endswith((".py", ".pyc")) and not any(fnmatch(rel, p) for p in patterns):
                    missing.append(rel)
        self.assertEqual(missing, [], "not in pyproject.toml's package-data")


if __name__ == "__main__":
    unittest.main()
