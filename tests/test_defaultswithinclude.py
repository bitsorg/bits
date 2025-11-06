import os
import tempfile

import unittest
import yaml
import bits_helpers.utilities
from bits_helpers.utilities import merge_dicts
from bits_helpers.utilities import yamlLoad

class TestYamlLoadIncludes(unittest.TestCase):

    def setUp(self):
        # Sets up temporary directories
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = self.tmpdir.name

    def tearDown(self):
        # Removes temporary directories
        self.tmpdir.cleanup()

    def _write(self, name, content):
        # Helper to write test files in the temp directory
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_basic_include(self):
        # Include one YAML file inside a defaults file
        included = self._write("included.yaml", """
            b: 2
            """)
        main = r"""
            a: 1
            overrides: !include included.yaml
            """

        with open(os.path.join(self.dir, "main.yaml"), "w") as f:
            f.write(main)

        with open(os.path.join(self.dir, "main.yaml")) as f:
            data = yamlLoad(f)

        self.assertEqual(data["a"], 1)
        self.assertEqual(data["overrides"]["b"], 2)

    def test_nested_includes(self):
        # Include a yaml file that itself includes another yaml file in a defaults file
        deep = self._write("deep.yaml", """
            x: 42
            """)
        inner = self._write("inner.yaml", r"""
            nested: !include deep.yaml
            """)
        main = self._write("main.yaml", r"""
            root: !include inner.yaml
            """)

        with open(main) as f:
            data = yamlLoad(f)

        self.assertEqual(data["root"]["nested"]["x"], 42)

    def test_missing_include_raises(self):
        # Include a missing yaml file raises FileNotFoundError
        main = self._write("main.yaml", """
            missing: !include nofile.yaml
            """)

        with self.assertRaises(FileNotFoundError):
            with open(main) as f:
                yamlLoad(f)

    def test_relative_path_resolution(self):
        # !include paths should be resolved relative to the parent file.
        subdir = os.path.join(self.dir, "sub")
        os.makedirs(subdir)
        included = os.path.join(subdir, "child.yaml")
        with open(included, "w") as f:
            f.write("v: 99")
        main = os.path.join(self.dir, "main.yaml")
        with open(main, "w") as f:
            f.write("child: !include sub/child.yaml")

        with open(main) as f:
            data = yamlLoad(f)

        self.assertEqual(data["child"]["v"], 99)

    def test_order_preservation(self):
        included = self._write("included.yaml", r"""
            x: 1
            y: 2
            """)
        main = self._write("main.yaml", r"""
            !include included.yaml
            """)

        with open(main) as f:
            data = yamlLoad(f)

        keys = list(data.keys())
        self.assertEqual(keys, ["x", "y"])

        def test_merge_defaults_with_package_overrides(self):
        # Simulate a "base defaults" YAML structure
            base_defaults = {
                "packages": {
                    "ROOT": {
                        "tag": "v6-30-04",
                        "source": "https://github.com/root-project/root.git",
                        "requires": ["Python", "zlib"],
                    },
                    "AliRoot": {
                        "tag": "v5-09-60",
                        "source": "https://github.com/alisw/AliRoot.git",
                        "requires": ["ROOT"],
                    },
                },
                "disable": ["MySQL"],
                "architecture": "slc9_x86-64",
            }

        # Simulate an "override defaults" YAML
            override_defaults = {
                "packages": {
                   "ROOT": {
                        # overrides the tag and adds an extra dependency
                        "tag": "v6-32-02",
                        "requires": ["Python", "zlib", "OpenSSL"],
                   },
                   "AliRoot": {
                        # same tag, but overrides the source
                        "source": "https://mirror.example.com/AliRoot.git",
                   },
                   "AliPhysics": {
                        # completely new package
                        "tag": "v5-09-60a",
                        "source": "https://github.com/alisw/AliPhysics.git",
                        "requires": ["AliRoot"],
                   },
                },
                "disable": ["Boost"],
            }

            merged = merge_dicts(base_defaults, override_defaults)

            # Expected result: overrides applied, lists extended, new packages added
            expected = {
                "packages": {
                    "ROOT": {
                        "tag": "v6-32-02",
                        "source": "https://github.com/root-project/root.git",
                        "requires": ["Python", "zlib", "OpenSSL"],
                    },
                    "AliRoot": {
                        "tag": "v5-09-60",
                        "source": "https://mirror.example.com/AliRoot.git",
                        "requires": ["ROOT"],
                    },
                    "AliPhysics": {
                        "tag": "v5-09-60a",
                        "source": "https://github.com/alisw/AliPhysics.git",
                        "requires": ["AliRoot"],
                    },
                },
                "disable": ["MySQL", "Boost"],
                "architecture": "slc9_x86-64",
            }

            self.assertEqual(merged, expected)
            self.assertIn("AliPhysics", merged["packages"])
            self.assertEqual(merged["packages"]["ROOT"]["tag"], "v6-32-02")
            self.assertIn("Boost", merged["disable"])
