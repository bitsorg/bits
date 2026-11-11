"""consumer_hash_as: dependents see a pinned version's hash of a package."""
import unittest
from collections import OrderedDict

from bits_helpers.hashing import storeHashes

REFS = {"refs/tags/v0.0.35": "bca9f7e", "refs/tags/v0.0.36": "2b63992",
        "refs/heads/main": "2b63992"}


def _tools(version, body="install bits-include", pin=None, devel=False):
    spec = {"package": "tools", "recipe": body, "version": version,
            "tag": "v" + version, "commit_hash": "v" + version, "scm_refs": REFS,
            "pkg_family": "", "is_devel_pkg": devel, "source": "https://example/tools",
            "env": OrderedDict(), "requires": ["defaults-release"]}
    if devel:
        spec.update(commit_hash="abc123", devel_hash="dirty")
    if pin:
        spec["consumer_hash_as"] = pin
    return spec


def _app_hash(tools, defaults="defaults_A"):
    specs = {"defaults-release": {"hash": defaults}, "tools": tools,
             "app": {"package": "app", "recipe": "make", "version": "1.0", "tag": "1.0",
                     "commit_hash": "1.0", "scm_refs": {}, "pkg_family": "",
                     "is_devel_pkg": False, "env": OrderedDict(),
                     "requires": ["defaults-release", "tools"]}}
    storeHashes("tools", specs, considerRelocation=False)
    tools["hash"] = tools["remote_revision_hash"]
    storeHashes("app", specs, considerRelocation=False)
    return specs["app"]["remote_revision_hash"]


PIN = {"version": "0.0.35", "tag": "v0.0.35"}


class ConsumerHashTestCase(unittest.TestCase):

    def test_bump_with_pin_keeps_dependents(self):
        for d in ("defaults_A", "defaults_B"):   # holds for every defaults set
            self.assertEqual(_app_hash(_tools("0.0.35"), d),
                             _app_hash(_tools("0.0.36", pin=PIN), d))

    def test_bump_without_pin_changes_dependents(self):
        # Negative control: today's behaviour.
        self.assertNotEqual(_app_hash(_tools("0.0.35")), _app_hash(_tools("0.0.36")))

    def test_package_itself_still_rebuilds(self):
        old, new = _tools("0.0.35"), _tools("0.0.36", pin=PIN)
        _app_hash(old), _app_hash(new)
        self.assertNotEqual(old["remote_revision_hash"], new["remote_revision_hash"])
        self.assertEqual(new["_consumer_hash"], old["remote_revision_hash"])

    def test_pin_on_unchanged_version_changes_nothing(self):
        plain, pinned = _tools("0.0.35"), _tools("0.0.35", pin=PIN)
        self.assertEqual(_app_hash(plain), _app_hash(pinned))
        self.assertEqual(plain["remote_revision_hash"], pinned["remote_revision_hash"])

    def test_body_change_still_reaches_dependents(self):
        self.assertNotEqual(_app_hash(_tools("0.0.35")),
                            _app_hash(_tools("0.0.36", body="install other", pin=PIN)))

    def test_devel_checkout_is_not_pinned(self):
        # A checkout's edits must reach dependents: no pin while it is devel.
        devel = _tools("0.0.36", pin=PIN, devel=True)
        self.assertNotEqual(_app_hash(_tools("0.0.35")), _app_hash(devel))
        self.assertNotIn("_consumer_hash", devel)

    def test_without_scm_refs_the_pin_holds(self):
        # A spec without scm_refs (e.g. bits doctor's) must not fail.
        tools = _tools("0.0.36", pin=PIN)
        del tools["scm_refs"]
        self.assertEqual(_app_hash(_tools("0.0.35")), _app_hash(tools))

    def test_force_rebuild_does_not_reach_the_pin(self):
        tools = _tools("0.0.36", pin=PIN)
        tools["force_rebuild"] = True
        self.assertEqual(_app_hash(_tools("0.0.35")), _app_hash(tools))

    def test_branch_or_unknown_tag_is_an_error(self):
        for tag in ("main", "v0.0.34"):
            with self.assertRaises(SystemExit):
                _app_hash(_tools("0.0.36", pin={"version": "0.0.35", "tag": tag}))

    def test_tarball_sources_are_an_error(self):
        tools = _tools("0.0.36", pin=PIN)
        tools["sources"] = ["https://example/tools-0.0.36.tar.gz"]
        with self.assertRaises(SystemExit):
            _app_hash(tools)

    def test_incomplete_pin_is_an_error(self):
        for pin in ({"version": "0.0.35"}, {"version": 0.1, "tag": "v0.1"}, True):
            with self.assertRaises(SystemExit):
                _app_hash(_tools("0.0.36", pin=pin))


if __name__ == "__main__":
    unittest.main()
