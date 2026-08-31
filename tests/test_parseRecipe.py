# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
import platform
from bits_helpers.utilities import parseDefaults
from bits_helpers.recipe import parseRecipe, getRecipeReader
from bits_helpers.recipe import FileReader, GitReader
from bits_helpers.utilities import validateDefaults, incompatibleFlavorDefaults
from bits_helpers.recipe import SpecError
from collections import OrderedDict

TEST1="""package: foo
version: bar
---
"""

TEST_BROKEN_1 = "broken"
TEST_BROKEN_2 = "---"
TEST_BROKEN_3 = """gfooo:
   - :
---
"""
TEST_BROKEN_4 = """broken
---
"""

TEST_BROKEN_5 = """tag: foo
---
"""

TEST_BROKEN_6 = """tag: "foo
---
"""

ERROR_MSG_3 = """Unable to parse test_broken_3.sh
while parsing a block mapping
expected <block end>, but found ':'
  in "<unicode string>", line 2, column 6:
       - :
         ^"""

ERROR_MSG_4 = """Malformed header for test_broken_4.sh
Not a YAML key / value."""

ERROR_MSG_5 = """Malformed header for test_broken_5.sh
Missing package field in header."""

ERROR_MSG_6 = """Unable to parse test_broken_6.sh
while scanning a quoted scalar
  in "<unicode string>", line 1, column 6:
    tag: "foo
         ^
found unexpected end of stream
  in "<unicode string>", line 2, column 1:
    
    ^"""

class Recoder:
  def __init__(self) -> None:
    self.buffer = ""
  def __call__(self, s, *a) -> None:
    self.buffer += s % a

class BufferReader:
  def __init__(self, filename, recipe) -> None:
    self.url = filename
    self.buffer = recipe
  def __call__(self):
    if type(self.buffer) == bytes:
      return self.buffer.decode()
    else:
      return self.buffer

class TestRecipes(unittest.TestCase):
  def test_recipes(self) -> None:
    err, meta, body = parseRecipe(BufferReader("test1.sh", TEST1))
    self.assertEqual(err, None)
    self.assertEqual(meta["package"], "foo")
    self.assertEqual(meta["version"],  "bar")
    err, meta, body = parseRecipe(BufferReader("test_broken_1.sh", TEST_BROKEN_1))
    self.assertEqual(err,  "Unable to parse test_broken_1.sh. Header missing.")
    err, meta, body = parseRecipe(BufferReader("test_broken_2.sh", TEST_BROKEN_2))
    self.assertEqual(err, "Malformed header for test_broken_2.sh\nEmpty recipe.")
    self.assertTrue(not meta and not body)
    err, meta, body = parseRecipe(BufferReader("test_broken_3.sh", TEST_BROKEN_3))
    self.assertEqual(err.encode("ascii"), ERROR_MSG_3.encode("ascii"))
    self.assertEqual(meta, None)
    self.assertEqual(body.strip(), "")
    err, meta, body = parseRecipe(BufferReader("test_broken_4.sh", TEST_BROKEN_4))
    self.assertEqual(err, ERROR_MSG_4)
    err, meta, body = parseRecipe(BufferReader("test_broken_5.sh", TEST_BROKEN_5))
    self.assertEqual(err, ERROR_MSG_5)
    err, meta, body = parseRecipe(BufferReader("test_broken_6.sh", TEST_BROKEN_6))
    self.assertEqual(err.strip(), ERROR_MSG_6.strip())

  def test_freetext_autoquote(self) -> None:
    # A ": " inside an unquoted description used to trip YAML
    # ("mapping values are not allowed here"); it is now auto-quoted.
    err, meta, body = parseRecipe(BufferReader(
      "fq1.sh", "package: foo\nversion: 1\ndescription: Foo: the bar (baz)\n---\n"))
    self.assertEqual(err, None)
    self.assertEqual(meta["description"], "Foo: the bar (baz)")
    # acknowledgement with parentheses + colon-space.
    err, meta, body = parseRecipe(BufferReader(
      "fq2.sh", "package: foo\nversion: 1\nacknowledgement: Portions (C) 2020: ACME\n---\n"))
    self.assertEqual(err, None)
    self.assertEqual(meta["acknowledgement"], "Portions (C) 2020: ACME")
    # An already-quoted value is preserved (transform is idempotent).
    err, meta, body = parseRecipe(BufferReader(
      "fq3.sh", 'package: foo\nversion: 1\ndescription: "already: fine"\n---\n'))
    self.assertEqual(err, None)
    self.assertEqual(meta["description"], "already: fine")
    # A block scalar (|) is left untouched by the sanitizer.
    err, meta, body = parseRecipe(BufferReader(
      "fq4.sh", "package: foo\nversion: 1\ndescription: |\n  line one\n  line two\n---\n"))
    self.assertEqual(err, None)
    self.assertIn("line one", meta["description"])
    self.assertIn("line two", meta["description"])

  def test_getRecipeReader(self) -> None:
    f = getRecipeReader("foo")
    self.assertEqual(type(f), FileReader)
    f = getRecipeReader("dist:foo@master")
    self.assertEqual(type(f), FileReader)
    f = getRecipeReader("dist:foo@master", "alidist")
    self.assertEqual(type(f), GitReader)

  def test_parseDefaults(self) -> None:
    disable = ["bar"]
    err, overrides, taps, _defaults_meta = parseDefaults(disable,
                                        lambda: ({ "disable": "foo",
                                                   "overrides": OrderedDict({"ROOT@master": {"requires": "GCC"}})},
                                                 ""),
                                        Recoder())
    self.assertEqual(disable, ["bar", "foo"])
    self.assertEqual(overrides, {'defaults-release': {}, 'root': {'requires': 'GCC'}})
    self.assertEqual(taps, {'root': 'dist:ROOT@master'})

  def test_validateDefault(self) -> None:
    ok, out, validDefaults = validateDefaults({"something": True}, "release")
    self.assertEqual(ok, True)
    ok, out, validDefaults = validateDefaults({"package": "foo","valid_defaults": ["o2", "o2-dataflow"]}, "release")
    self.assertEqual(ok, False)
    self.assertEqual(out, "Cannot compile foo with `release' default. Valid defaults are\n - o2\n - o2-dataflow")
    ok, out, validDefaults = validateDefaults({"package": "foo","valid_defaults": ["o2", "o2-dataflow"]}, "o2")
    self.assertEqual(ok, True)
    ok, out, validDefaults = validateDefaults({"package": "foo","valid_defaults": "o2-dataflow"}, "o2")
    self.assertEqual(ok, False)
    self.assertEqual(validDefaults, ["o2-dataflow"])
    ok, out, validDefaults = validateDefaults({"package": "foo","valid_defaults": "o2"}, "o2")
    self.assertEqual(ok, True)
    ok, out, validDefaults = validateDefaults({"package": "foo","valid_defaults": 1}, "o2")
    self.assertEqual(ok, False)
    self.assertEqual(out, 'valid_defaults needs to be a string or a list of strings. Found [1].')
    ok, out, validDefaults = validateDefaults({"package": "foo", "valid_defaults": {}}, "o2")
    self.assertEqual(ok, False)
    self.assertEqual(out, 'valid_defaults needs to be a string or a list of strings. Found [{}].')

  def test_incompatibleFlavorDefaults(self) -> None:
    # No package restricts defaults -> always compatible.
    self.assertEqual(incompatibleFlavorDefaults([], ["release", "o2"]), ([], False))
    self.assertEqual(incompatibleFlavorDefaults(None, ["release", "o2"]), ([], False))

    valid = ["ali", "alo", "o2", "o2-acts"]
    meta = {"_valid_defaults_exempt": ["alidist", "release"]}

    # Structural layers (release base + alidist variant) are exempt; the flavor
    # leaf 'o2' is accepted -> compatible. This is the release::alidist::o2 case.
    self.assertEqual(
        incompatibleFlavorDefaults(valid, ["release", "alidist", "o2"], meta),
        ([], False))

    # A real flavor mismatch is still rejected.
    bad, missing = incompatibleFlavorDefaults(valid, ["release", "alidist", "nope"], meta)
    self.assertEqual((bad, missing), (["nope"], False))

    # 'release' is always exempt even without meta; a flavor package built with
    # no flavor selected is flagged as missing (preserves the original gate).
    self.assertEqual(incompatibleFlavorDefaults(valid, ["release"]), ([], True))
    self.assertEqual(incompatibleFlavorDefaults(valid, ["release"], {}), ([], True))

    # A valid single flavor passes.
    self.assertEqual(incompatibleFlavorDefaults(valid, ["release", "o2"]), ([], False))

if __name__ == '__main__':
    unittest.main()

