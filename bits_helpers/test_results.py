# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The result of the top package's tests (bits build --run-tests): read what
bits-recipe-tools' TestRecipe left in <work dir>/TESTS/<pkg>, sum up its JUnit
file, print it and always write summary.json there for CI to report. Never
raises: a summary is not worth failing a build for."""

import json
import os
import xml.etree.ElementTree as ET

from bits_helpers.log import banner, warning

MAX_NAMES = 50


def summarize(test_dir, run_id):
    """{state, rc, tests, failed, skipped, failed_tests}; state is passed,
    failed, no_tests, or not_run (no result from this run: the package was
    not rebuilt, or its recipe does not use bits-recipe-tools)."""
    s = {"state": "not_run", "rc": None, "tests": 0, "failed": 0, "skipped": 0, "failed_tests": []}
    try:
        with open(os.path.join(test_dir, "result.json")) as fh:
            result = json.load(fh)
    except (OSError, ValueError):
        return s
    if result.get("run") != run_id:
        return s
    s["rc"] = int(result.get("rc", 1))
    junit = os.path.join(test_dir, "junit.xml")
    if os.path.isfile(junit):
        try:
            root = ET.parse(junit).getroot()
            for suite in [root] if root.tag == "testsuite" else root.iter("testsuite"):
                s["tests"] += int(suite.get("tests", 0))
                s["failed"] += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
                s["skipped"] += int(suite.get("skipped", suite.get("disabled", 0)) or 0)
            s["failed_tests"] = [c.get("name", "?") for c in root.iter("testcase")
                                 if c.find("failure") is not None or c.find("error") is not None][:MAX_NAMES]
        except (ET.ParseError, ValueError) as exc:
            warning("cannot read %s: %s", junit, exc)
    s["state"] = "failed" if s["rc"] else ("passed" if s["tests"] else "no_tests")
    return s


def report_tests(work_dir, spec, run_id):
    package = spec["package"]
    test_dir = os.path.join(work_dir, "TESTS", package)
    try:
        s = summarize(test_dir, run_id)
        s.update(package=package, version=spec.get("version", ""), revision=spec.get("revision", ""),
                 hash=spec.get("hash", ""), log=os.path.join(test_dir, "test.log"))
        if s["state"] == "not_run":
            banner("Tests of %s: not run (not rebuilt in this run, or its recipe does not "
                   "use bits-recipe-tools)", package)
        else:
            counts = ", %d tests, %d failed, %d skipped" % (s["tests"], s["failed"], s["skipped"]) \
                if s["tests"] else ""
            banner("Tests of %s: %s%s\n  log: %s", package,
                   "FAILED (exit %d)" % s["rc"] if s["state"] == "failed" else s["state"].replace("_", " "),
                   counts, s["log"])
        os.makedirs(test_dir, exist_ok=True)
        with open(os.path.join(test_dir, "summary.json"), "w") as fh:
            json.dump(s, fh, indent=1)
        return s
    except Exception as exc:   # never fail a finished build over its test summary
        warning("could not summarise the tests of %s: %s", package, exc)
        return None
