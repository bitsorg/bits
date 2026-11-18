# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import subprocess
from unittest.mock import patch

import pytest

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


def test_layout_mixes_new_and_legacy_repositories(tmp_path, monkeypatch):
    repo = tmp_path / 'group[dev].bits'
    packages = repo / 'packages'
    packages.mkdir(parents=True)
    provider = repo / 'base.bits'
    provider.mkdir()
    monkeypatch.setenv('BITS_PATH', 'base')
    (repo / 'defaults-release.sh').write_text(recipe('defaults-release'))
    (repo / 'foo.sh').write_text(recipe('NotARecipeInThisLayout'))
    (packages / 'foo.sh').write_text(recipe('Foo'))
    (provider / 'bar.sh').write_text(recipe('Bar'))
    (repo / 'extra.tar').touch()
    (packages / 'versioned').mkdir()
    (packages / 'versioned' / 'latest').write_text(recipe('Versioned'))
    root = str(repo)
    assert resolveFilename({}, 'foo', root, {}) == (str(packages / 'foo.sh'), root)
    assert resolveFilename({}, 'versioned', root, {}) == (str(packages / 'versioned' / 'latest'), root)
    assert resolveFilename({}, 'bar', root, {}) == (str(provider / 'bar.sh'), str(provider))
    assert resolveFilename({}, 'defaults-release', root, {}) == (str(repo / 'defaults-release.sh'), root)
    assert resolveDefaultsFilename('release', root) == str(repo / 'defaults-release.sh')
    assert resolveLocalPath(root, 'file://extra.tar') == 'file:/' + str(repo / 'extra.tar')
    assert recipes_in(root) == {'foo': str(packages / 'foo.sh')}
    assert set(scan_recipes(root)['by_package']) == {'foo', 'defaults-release'}


def test_provider_discovery_uses_packages_but_keeps_checkout_root(tmp_path, monkeypatch):
    packages = tmp_path / 'packages'
    packages.mkdir()
    provider = tmp_path / 'base.bits'
    (provider / 'packages').mkdir(parents=True)
    (provider / 'packages' / 'foo.sh').write_text(recipe('Foo'))
    text = 'package: base.bits\nversion: "1"\nsource: example\nprovides_repository: true\nalways_load: true\n---\n'
    (packages / 'base.bits.sh').write_text(text)
    monkeypatch.delenv('BITS_PATH', raising=False)
    assert _local_provider_dir(tmp_path, 'base.bits') == str(provider)
    assert load_always_on_providers(str(tmp_path), str(tmp_path), '', True) == {
        str(provider): ('base.bits', 'local')}
    assert resolveFilename({}, 'foo', str(tmp_path), {}) == (
        str(provider / 'packages' / 'foo.sh'), str(provider))
    args = type('Args', (), {'bits_providers': 'https://example.invalid/repo'})()
    with patch('bits_helpers.repo_provider.clone_or_update_provider', return_value=(str(tmp_path), 'hash')):
        assert resolve_registry_repo(args, 'base.bits', str(tmp_path))['source'] == 'example'


def test_git_reader_uses_layout_of_requested_reference(tmp_path):
    root = str(tmp_path)

    def git(*args):
        return subprocess.check_output(['git', '-C', root, *args], text=True).strip()

    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Recipe Layout Test')
    (tmp_path / 'foo.sh').write_text(recipe('Foo'))
    git('add', 'foo.sh')
    git('commit', '-qm', 'Flat layout')
    old = git('rev-parse', 'HEAD')
    (tmp_path / 'packages').mkdir()
    (tmp_path / 'packages' / 'latest').write_text(recipe('Packages'))
    git('add', 'packages/latest')
    git('commit', '-qm', 'Add a versioned legacy Packages recipe')
    legacy = git('rev-parse', 'HEAD')
    assert resolveFilename({}, 'foo', root, {}) == (str(tmp_path / 'foo.sh'), root)
    assert resolveFilename({}, 'packages', root, {}) == (str(tmp_path / 'packages' / 'latest'), root)
    git('rm', 'packages/latest')
    (tmp_path / 'packages').mkdir(exist_ok=True)
    git('mv', 'foo.sh', 'packages/foo.sh')
    git('commit', '-qm', 'New layout')
    assert GitReader(f'dist:Foo@{old}', root)().strip() == recipe('Foo').strip()
    assert GitReader(f'dist:Foo@{legacy}', root)().strip() == recipe('Foo').strip()
    assert GitReader('dist:Foo@HEAD', root)().strip() == recipe('Foo').strip()


def test_generated_packages_and_includes_use_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv('BITS_PATH', raising=False)
    primary = tmp_path / 'primary'
    primary.mkdir()
    (primary / 'root.inc').write_text('echo wrong repository')
    monkeypatch.setenv('BITS_REPO_DIR', str(primary))
    generators = tmp_path / 'packages' / 'generated'
    generators.mkdir(parents=True)
    (generators / 'packages.py').write_text(
        'def getPackages(virtual_packages, config_dir):\n'
        '    virtual_packages["generated"] = {"pkgdir": config_dir, "version": "1"}\n')
    root = str(tmp_path)
    assert resolveFilename({}, 'generated', root, getGeneratedPackages(root)) == (
        'generate:generated@1', root)
    path = tmp_path / 'packages' / 'foo.sh'
    path.write_text(recipe('Foo') + '#!include <root.inc>\n#!include "local.inc"\n')
    (tmp_path / 'root.inc').write_text('echo root')
    (path.parent / 'local.inc').write_text('echo local')
    err, _, body = parseRecipe(getRecipeReader(str(path), repo_dir=root))
    assert err is None
    assert body.strip().splitlines() == ['echo root', 'echo local']


def test_checksums_resolves_patches_at_root_and_rejects_missing_checkout(tmp_path):
    (tmp_path / 'packages').mkdir()
    (tmp_path / 'packages' / 'foo.sh').write_text(
        'package: Foo\nversion: "1"\npatches: [foo.patch]\n---\n')
    root = str(tmp_path)
    checks = plan(root, [], None, [], 'x86_64-el9')
    assert checks['Foo']['patches']['foo.patch'] == (None, str(tmp_path / 'patches'))
    with pytest.raises(FileNotFoundError):
        plan(root, [str(tmp_path / 'missing.bits')], 'all', [], 'x86_64-el9')


def test_upgrade_copies_recipe_under_packages_and_patches_at_root(tmp_path):
    provider = tmp_path / 'provider'
    (provider / 'packages').mkdir(parents=True)
    (provider / 'patches').mkdir()
    (provider / '.bits_provider_ok').touch()
    (provider / 'patches' / 'foo.patch').write_text('patch')
    path = provider / 'packages' / 'foo.sh'
    path.write_text('package: Foo\nversion: "1"\npatches: [foo.patch]\n---\n')
    config = tmp_path / 'group.bits'
    (config / 'packages').mkdir(parents=True)
    assert _cached_repo(str(path)) == str(provider)
    dest, copied = _bring_in(str(path), str(config), 'Foo', dry=False)
    assert dest == str(config / 'packages' / 'foo.sh')
    assert copied == ['packages/foo.sh', 'patches/foo.patch']
    assert (config / 'patches' / 'foo.patch').read_text() == 'patch'
