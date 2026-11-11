# SPDX-License-Identifier: GPL-3.0-or-later
"""Defaults variables as CVMFS path slots ({root_release}): filled for the whole
build like {release}, collapsed when empty; reserved tokens are never taken from
the variables, and a value must be one path segment."""
import pytest
from bits_helpers.cvmfs_layout import bake_variables
from bits_helpers.cvmfs_publish import fixed_dir

T = "{prefix}/nightlies/ROOT/{arch}/LCG_110/Wed/{root_release}"


def test_variable_filled_and_collapsed():
    assert bake_variables(T, {"root_release": "master"}).endswith("/Wed/master")
    assert bake_variables(T, {"root_release": "v6-36-02"}).endswith("/Wed/v6-36-02")
    assert bake_variables(T, {"root_release": ""}).endswith("/LCG_110/Wed")
    with pytest.raises(ValueError):
        bake_variables(T, {})                  # neither a variable nor a token


def test_reserved_tokens_untouched():
    t = "{prefix}/{arch}/Packages/{pkg}/{version}"
    assert bake_variables(t, {"pkg": "x", "version": "y", "arch": "z", "prefix": "/p"}) == t


@pytest.mark.parametrize("value", ["a/b", "..", ".", "a b", "%2F", "-rf", ".git"])
def test_unsafe_values_refused(value):
    with pytest.raises(ValueError):
        bake_variables(T, {"root_release": value})


def test_view_is_the_whole_baked_path():
    t = bake_variables(T, {"root_release": "master"})
    ctx = {"templates": {"views": t, "prefix": "/cvmfs/r/lcg"}, "tmpl_prefix": "/cvmfs/r/lcg",
           "arch": "x86_64-el9-gcc14-dbg", "repo": "r"}
    assert fixed_dir(ctx, "views") == "lcg/nightlies/ROOT/x86_64-el9-gcc14-dbg/LCG_110/Wed/master"


def test_empty_value_inside_a_segment_refused():
    with pytest.raises(ValueError):
        bake_variables("{prefix}/n/ROOT-{root_release}/{arch}", {"root_release": ""})
    assert bake_variables("{prefix}/n/ROOT-{root_release}/{arch}", {"root_release": "master"}) \
        == "{prefix}/n/ROOT-master/{arch}"
