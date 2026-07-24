#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# All packaging metadata lives in pyproject.toml (PEP 621 [project] table +
# [tool.setuptools*] + [tool.setuptools_scm]). This shim only exists so legacy
# `python setup.py …` / older pip invocations keep working; it adds no metadata
# of its own. Do NOT reintroduce name/version/dependencies/python_requires here —
# duplicating them fights the [project] table (see issue #105).
from setuptools import setup

setup()
