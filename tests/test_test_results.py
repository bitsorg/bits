# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The top package's test result (bits build --run-tests): passed, failed,
no_tests or not_run, with the JUnit counts and failed names, from what
TestRecipe wrote in this run (matched by run id)."""
import json
import os

from bits_helpers.test_results import summarize, report_tests

SPEC = {"package": "ROOT", "version": "master", "revision": "3", "hash": "abc"}


def write(d, rc, run="r1", junit=None):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "result.json"), "w") as fh:
        json.dump({"rc": rc, "run": run}, fh)
    if junit is not None:
        with open(os.path.join(d, "junit.xml"), "w") as fh:
            fh.write(junit)


def test_failed_with_counts_and_names(tmp_path):
    d = str(tmp_path / "TESTS" / "ROOT")
    write(d, 8, junit='<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1">'
                      '<testcase name="a"><failure/></testcase><testcase name="b"/>'
                      '<testcase name="c"><error/></testcase></testsuite></testsuites>')
    s = report_tests(str(tmp_path), SPEC, "r1")
    assert (s["state"], s["rc"], s["tests"], s["failed"], s["skipped"]) == ("failed", 8, 5, 2, 1)
    assert s["failed_tests"] == ["a", "c"] and s["hash"] == "abc"
    assert json.load(open(os.path.join(d, "summary.json")))["state"] == "failed"


def test_passed_and_no_tests(tmp_path):
    write(str(tmp_path / "p"), 0, junit='<testsuite tests="3" failures="0"/>')
    assert summarize(str(tmp_path / "p"), "r1")["state"] == "passed"
    write(str(tmp_path / "n"), 0)
    assert summarize(str(tmp_path / "n"), "r1")["state"] == "no_tests"


def test_other_run_or_missing_is_not_run_and_still_written(tmp_path):
    d = str(tmp_path / "TESTS" / "ROOT")
    write(d, 0, run="yesterday")
    s = report_tests(str(tmp_path), SPEC, "r1")
    assert s["state"] == "not_run"
    assert json.load(open(os.path.join(d, "summary.json")))["state"] == "not_run"
    assert summarize(str(tmp_path / "none"), "r1")["state"] == "not_run"


def test_broken_files_never_raise(tmp_path):
    d = str(tmp_path / "TESTS" / "ROOT")
    os.makedirs(d)
    with open(os.path.join(d, "result.json"), "w") as fh:
        fh.write("{trunc")
    assert report_tests(str(tmp_path), SPEC, "r1")["state"] == "not_run"
    write(d, 3, junit="<not xml")
    assert report_tests(str(tmp_path), SPEC, "r1")["state"] == "failed"
