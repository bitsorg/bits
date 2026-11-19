# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bits_helpers.checksums_cmd import plan, recipes_in
from bits_helpers.compliance import scan_recipes
from bits_helpers.paths import resolveDefaultsFilename, resolveFilename, resolveLocalPath
from bits_helpers.recipe import GitReader, getGeneratedPackages, getRecipeReader, parseRecipe
from bits_helpers.repo_provider import (
    _local_provider_dir, load_always_on_providers, resolve_registry_repo,
)
from bits_helpers.upgrade_cmd import _bring_in, _cached_repo


def recipe(name):
    return f'package: {name}\nversion: "1"\n---\n'


class TestRecipeLayout(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('BITS_PATH', None)
        os.environ.pop('BITS_REPO_DIR', None)

    def test_layout_mixes_new_and_legacy_repositories(self):
        repo = self.directory / 'group[dev].bits'
        recipes = repo / 'recipes'
        recipes.mkdir(parents=True)
        provider = repo / 'base.bits'
        provider.mkdir()
        os.environ['BITS_PATH'] = 'base'
        (repo / 'defaults-release.sh').write_text(recipe('defaults-release'))
        (repo / 'foo.sh').write_text(recipe('NotARecipeInThisLayout'))
        (recipes / 'foo.sh').write_text(recipe('Foo'))
        (provider / 'bar.sh').write_text(recipe('Bar'))
        (provider / 'packages').mkdir()
        (provider / 'packages' / 'latest').write_text(recipe('Packages'))
        (repo / 'extra.tar').touch()
        (recipes / 'versioned').mkdir()
        (recipes / 'versioned' / 'latest').write_text(recipe('Versioned'))
        root = str(repo)
        for name, path, checkout in (
            ('foo', recipes / 'foo.sh', repo),
            ('versioned', recipes / 'versioned' / 'latest', repo),
            ('bar', provider / 'bar.sh', provider),
            ('packages', provider / 'packages' / 'latest', provider),
            ('defaults-release', repo / 'defaults-release.sh', repo),
        ):
            with self.subTest(name=name):
                self.assertEqual(resolveFilename({}, name, root, {}), (str(path), str(checkout)))
        self.assertEqual(resolveDefaultsFilename('release', root), str(repo / 'defaults-release.sh'))
        self.assertEqual(resolveLocalPath(root, 'file://extra.tar'), 'file:/' + str(repo / 'extra.tar'))
        self.assertEqual(recipes_in(root), {'foo': str(recipes / 'foo.sh')})
        self.assertEqual(set(scan_recipes(root)['by_package']), {'foo', 'defaults-release'})

    def test_provider_discovery_uses_recipes_but_keeps_checkout_root(self):
        recipes = self.directory / 'recipes'
        recipes.mkdir()
        provider = self.directory / 'base.bits'
        (provider / 'recipes').mkdir(parents=True)
        (provider / 'recipes' / 'foo.sh').write_text(recipe('Foo'))
        text = 'package: base.bits\nversion: "1"\nsource: example\nprovides_repository: true\nalways_load: true\n---\n'
        (recipes / 'base.bits.sh').write_text(text)
        root = str(self.directory)
        self.assertEqual(_local_provider_dir(root, 'base.bits'), str(provider))
        self.assertEqual(load_always_on_providers(root, root, '', True), {str(provider): ('base.bits', 'local')})
        self.assertEqual(resolveFilename({}, 'foo', root, {}), (str(provider / 'recipes' / 'foo.sh'), str(provider)))
        args = type('Args', (), {'bits_providers': 'https://example.invalid/repo'})()
        with patch('bits_helpers.repo_provider.clone_or_update_provider', return_value=(root, 'hash')):
            self.assertEqual(resolve_registry_repo(args, 'base.bits', root)['source'], 'example')

    def test_git_reader_uses_layout_of_requested_reference(self):
        root = str(self.directory)

        def git(*args):
            return subprocess.check_output(['git', '-C', root, *args], text=True).strip()

        git('init', '-q')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'user.name', 'Recipe Layout Test')
        git('config', 'commit.gpgsign', 'false')
        (self.directory / 'foo.sh').write_text(recipe('Foo'))
        git('add', 'foo.sh')
        git('commit', '-qm', 'Flat layout')
        old = git('rev-parse', 'HEAD')
        recipes = self.directory / 'recipes'
        recipes.mkdir()
        (recipes / 'README.md').write_text('Notes for the flat checkout.')
        git('add', 'recipes/README.md')
        git('commit', '-qm', 'Add notes under recipes in the flat layout')
        assets = git('rev-parse', 'HEAD')
        self.assertEqual(recipes_in(root), {'foo': str(self.directory / 'foo.sh')})
        (recipes / 'latest').write_text(recipe('Recipes'))
        git('add', 'recipes/latest')
        git('commit', '-qm', 'Add a versioned legacy Recipes recipe')
        legacy = git('rev-parse', 'HEAD')
        self.assertEqual(resolveFilename({}, 'foo', root, {}), (str(self.directory / 'foo.sh'), root))
        self.assertEqual(resolveFilename({}, 'recipes', root, {}), (str(recipes / 'latest'), root))
        git('rm', 'recipes/latest')
        recipes.mkdir(exist_ok=True)
        git('mv', 'foo.sh', 'recipes/foo.sh')
        git('commit', '-qm', 'New layout')
        for ref in (old, assets, legacy, 'HEAD'):
            with self.subTest(ref=ref):
                self.assertEqual(GitReader(f'dist:Foo@{ref}', root)().strip(), recipe('Foo').strip())

    def test_generated_packages_and_includes_use_repo_root(self):
        primary = self.directory / 'primary'
        primary.mkdir()
        (primary / 'root.inc').write_text('echo wrong repository')
        os.environ['BITS_REPO_DIR'] = str(primary)
        recipes = self.directory / 'recipes'
        generators = recipes / 'generated'
        generators.mkdir(parents=True)
        (generators / 'packages.py').write_text(
            'def getPackages(virtual_packages, config_dir):\n'
            '    virtual_packages["generated"] = {"pkgdir": config_dir, "version": "1"}\n')
        root = str(self.directory)
        self.assertEqual(resolveFilename({}, 'generated', root, getGeneratedPackages(root)),
                         ('generate:generated@1', root))
        path = recipes / 'foo.sh'
        path.write_text(recipe('Foo') + '#!include <root.inc>\n#!include "local.inc"\n')
        (self.directory / 'root.inc').write_text('echo root')
        (recipes / 'local.inc').write_text('echo local')
        err, _, body = parseRecipe(getRecipeReader(str(path), repo_dir=root))
        self.assertIsNone(err)
        self.assertEqual(body.strip().splitlines(), ['echo root', 'echo local'])

    def test_checksums_resolves_patches_at_root_and_rejects_missing_checkout(self):
        (self.directory / 'recipes').mkdir()
        (self.directory / 'recipes' / 'foo.sh').write_text(
            'package: Foo\nversion: "1"\npatches: [foo.patch]\n---\n')
        root = str(self.directory)
        checks = plan(root, [], None, [], 'x86_64-el9')
        self.assertEqual(checks['Foo']['patches']['foo.patch'], (None, str(self.directory / 'patches')))
        with self.assertRaises(FileNotFoundError):
            plan(root, [str(self.directory / 'missing.bits')], 'all', [], 'x86_64-el9')

    def test_upgrade_copies_recipe_under_recipes_and_patches_at_root(self):
        provider = self.directory / 'provider'
        (provider / 'recipes').mkdir(parents=True)
        (provider / 'patches').mkdir()
        (provider / '.bits_provider_ok').touch()
        (provider / 'patches' / 'foo.patch').write_text('patch')
        path = provider / 'recipes' / 'foo.sh'
        path.write_text('package: Foo\nversion: "1"\npatches: [foo.patch]\n---\n')
        config = self.directory / 'group.bits'
        (config / 'recipes').mkdir(parents=True)
        (config / 'recipes' / 'existing.sh').write_text(recipe('Existing'))
        self.assertEqual(_cached_repo(str(path)), str(provider))
        dest, copied = _bring_in(str(path), str(config), 'Foo', dry=False)
        self.assertEqual(dest, str(config / 'recipes' / 'foo.sh'))
        self.assertEqual(copied, ['recipes/foo.sh', 'patches/foo.patch'])
        self.assertEqual((config / 'patches' / 'foo.patch').read_text(), 'patch')
