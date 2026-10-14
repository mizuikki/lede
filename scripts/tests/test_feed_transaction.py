"""Regression tests for isolated feed snapshots, rollback and Go prerequisites."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('feed_transaction', ROOT / 'scripts/sync-feeds-transaction.py')
txn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(txn)
inputs = txn.inputs
api = txn.lock_api


def git(path, *args):
    env = os.environ | {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_AUTHOR_NAME': 'Feed Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                       'GIT_COMMITTER_NAME': 'Feed Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
    return subprocess.check_output(['git', '-C', str(path), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def repository(path, files):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '--quiet', '--initial-branch=main')
    for relative, content in files.items():
        write(path / relative, content)
    git(path, 'add', '.')
    git(path, 'commit', '--quiet', '-m', 'test fixture')
    git(path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return git(path, 'rev-parse', 'HEAD')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repository(self.root, {'feeds.conf.default': 'src-git packages https://example.invalid/packages.git;main\n',
                               'patches/dockerd/100-skip-host-nested-binaries.patch': 'managed docker\n'})
        self.package = self.root / 'feeds/packages'
        sha = repository(self.package, {'lang/perl/Makefile': 'original perl\n', 'untouched': 'base\n'})
        write(self.package / 'lang/perl/Makefile', 'managed perl\n')
        self.lock = {'schema_version': 1,
                     'feeds': [{'name': 'packages', 'url': 'https://example.invalid/packages.git',
                                'branch': 'main', 'configured_branch': 'main', 'revision': sha, 'order': 1}],
                     'overlays': {'official_perl': {'repository': api.PERL_URL, 'branch': api.PERL_BRANCH,
                                                   'revision': 'a' * 40, 'tree': 'b' * 40,
                                                   'path': api.PERL_DESTINATION,
                                                   'sha256': api.directory_content_digest(self.package / 'lang/perl')},
                                  'dockerd_patch': {'path': api.DOCKER_PATCH, 'staged_path': api.DOCKER_STAGED,
                                                    'sha256': hashlib.sha256(b'managed docker\n').hexdigest()}}}
        write(self.root / '.github/feed-lock.json', json.dumps(self.lock))
        write(self.root / api.DOCKER_STAGED, 'managed docker\n')
        write(self.root / 'package/feeds/previous', 'old links\n')

    def test_known_overlays_are_recognized_without_resetting_them(self):
        txn.check_existing(self.root)
        self.assertEqual((self.package / 'lang/perl/Makefile').read_text(), 'managed perl\n')

    def test_pinned_feed_without_configured_branch_can_be_prepared(self):
        lock = copy.deepcopy(self.lock)
        lock['feeds'][0]['branch'] = None
        stage = self.root / '.cache/prepared'
        with mock.patch.object(txn, 'copy_source'), mock.patch.object(txn, 'command') as command, \
                mock.patch.object(inputs, 'collect_requirements', return_value={}), \
                mock.patch.object(api, 'verify_installed', return_value=[]):
            txn.prepare(self.root, stage, lock)
        command.assert_any_call(stage, 'git', '-C', str(stage / 'feeds/packages'),
                                'update-ref', 'refs/remotes/origin/locked', lock['feeds'][0]['revision'])

    def test_user_edits_in_each_managed_overlay_are_preserved_and_rejected(self):
        for relative in ['lang/perl/Makefile', 'utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch']:
            with self.subTest(relative=relative):
                path = self.package / relative
                before = path.read_text()
                path.write_text(before + 'user edit\n')
                with self.assertRaisesRegex(api.LockError, 'unrecognized modification'):
                    txn.check_existing(self.root)
                self.assertIn('user edit', path.read_text())
                path.write_text(before)

    def test_staged_unstaged_untracked_and_local_commit_are_rejected(self):
        write(self.package / 'untouched', 'user\n')
        with self.assertRaisesRegex(api.LockError, 'unrecognized modification'):
            txn.check_existing(self.root)
        git(self.package, 'add', 'untouched')
        with self.assertRaisesRegex(api.LockError, 'staged user changes'):
            txn.check_existing(self.root)
        git(self.package, 'commit', '--quiet', '-m', 'local fixture change')
        with self.assertRaisesRegex(api.LockError, 'local commit'):
            txn.check_existing(self.root)

    def test_untracked_user_file_is_not_deleted(self):
        write(self.package / 'user-extra', 'preserve\n')
        with self.assertRaisesRegex(api.LockError, 'unrecognized modification'):
            txn.check_existing(self.root)
        self.assertEqual((self.package / 'user-extra').read_text(), 'preserve\n')

    def test_legacy_official_cache_recognizes_exact_committed_tree(self):
        cache = self.root / '.cache/openwrt-packages'
        repository(cache, {'lang/perl/Makefile': 'managed perl\n'})
        git(cache, 'remote', 'add', 'origin', api.PERL_URL)
        self.lock['overlays']['official_perl']['sha256'] = '0' * 64
        write(self.root / '.github/feed-lock.json', json.dumps(self.lock))
        txn.check_existing(self.root)
        write(self.package / 'lang/perl/Makefile', 'user edit\n')
        with self.assertRaises(api.LockError):
            txn.check_existing(self.root)

    def stage(self, root, stage, lock):
        write(stage / 'feeds/new-feed/data', 'complete snapshot\n')
        write(stage / 'package/feeds/new-link', 'new links\n')
        write(stage / '.github/feed-lock.json', json.dumps(lock))

    def test_download_index_or_requirement_failure_keeps_existing_inputs_and_blocks_build(self):
        for reason in ['download', 'index', 'Go requirement']:
            with self.subTest(reason=reason), mock.patch.object(api, 'create_lock', return_value=copy.deepcopy(self.lock)), \
                    mock.patch.object(txn, 'prepare', side_effect=api.LockError(reason)):
                with self.assertRaisesRegex(api.LockError, reason):
                    txn.synchronize(self.root, locked=False, lock_path=self.root / '.github/feed-lock.json')
                self.assertEqual(git(self.package, 'rev-parse', 'HEAD'), self.lock['feeds'][0]['revision'])
                self.assertEqual((self.root / 'package/feeds/previous').read_text(), 'old links\n')
                self.assertEqual(json.loads((self.root / '.github/feed-lock.json').read_text()), self.lock)
                with self.assertRaisesRegex(api.LockError, 'incomplete or failed'):
                    inputs.check_build(self.root, 'world')

    def test_success_promotes_complete_snapshot_and_retains_backup(self):
        updated = copy.deepcopy(self.lock)
        updated['feeds'][0]['revision'] = 'c' * 40
        with mock.patch.object(api, 'create_lock', return_value=updated), mock.patch.object(txn, 'prepare', side_effect=self.stage):
            txn.synchronize(self.root, locked=False, lock_path=self.root / '.github/feed-lock.json')
        self.assertTrue((self.root / 'feeds/new-feed/data').is_file())
        self.assertTrue((self.root / 'package/feeds/new-link').is_file())
        self.assertEqual(json.loads((self.root / '.github/feed-lock.json').read_text()), updated)
        self.assertFalse((self.root / '.cache/feed-sync/FAILED').exists())
        backups = list((self.root / '.cache/feed-sync/backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'feeds/packages/lang/perl/Makefile').read_text(), 'managed perl\n')
        self.assertEqual((backups[0] / 'package/feeds/previous').read_text(), 'old links\n')
        self.assertEqual(json.loads((backups[0] / '.github/feed-lock.json').read_text()), self.lock)

    def test_locked_success_does_not_rewrite_or_enrich_legacy_lock(self):
        original = (self.root / '.github/feed-lock.json').read_bytes()
        def stage(root, prepared, lock):
            self.stage(root, prepared, lock)
            lock['go_packages'] = {}
        with mock.patch.object(txn, 'prepare', side_effect=stage), mock.patch.object(api, 'create_lock', side_effect=AssertionError('no refresh')):
            txn.synchronize(self.root, locked=True, lock_path=self.root / '.github/feed-lock.json')
        self.assertEqual((self.root / '.github/feed-lock.json').read_bytes(), original)
        self.assertEqual(json.loads((self.root / '.cache/feed-sync/installed-lock.json').read_text()), self.lock)

    def test_promotion_failure_restores_all_previous_inputs(self):
        stage, backup = self.root / '.cache/stage', self.root / '.cache/backup'
        self.stage(self.root, stage, self.lock)
        original_replace = os.replace
        def replace(source, destination):
            if Path(source) == stage / 'package/feeds':
                raise OSError('promotion fixture failure')
            return original_replace(source, destination)
        with mock.patch.object(txn.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'promotion fixture failure'):
                txn.promote(self.root, stage, self.lock, refresh=True, backup=backup)
        self.assertEqual((self.package / 'lang/perl/Makefile').read_text(), 'managed perl\n')
        self.assertEqual((self.root / 'package/feeds/previous').read_text(), 'old links\n')
        self.assertEqual(json.loads((self.root / '.github/feed-lock.json').read_text()), self.lock)

    def test_profile_wrapper_stops_before_config_or_make_on_sync_failure(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'profiles').mkdir()
        import shutil
        shutil.copy2(ROOT / 'scripts/apply-profile.sh', self.root / 'scripts/apply-profile.sh')
        write(self.root / 'profiles/test.diffconfig', 'CONFIG_TARGET_x86=y\n')
        write(self.root / '.config', 'existing configuration\n')
        helper = self.root / 'scripts/sync-feeds.sh'
        write(helper, '#!/bin/sh\nexit 7\n')
        helper.chmod(0o755)
        result = subprocess.run(['bash', 'scripts/apply-profile.sh', 'test', '--sync-all-feeds'], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual((self.root / '.config').read_text(), 'existing configuration\n')


class GoRequirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        mod = b'module example.invalid/proxy\n\ngo 1.27\n'
        downloads = self.root / 'dl'
        downloads.mkdir()
        archive = downloads / 'Xray-core-26.10.10.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            member = tarfile.TarInfo('Xray-core-26.10.10/go.mod')
            member.size = len(mod)
            tar.addfile(member, io.BytesIO(mod))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.makefile = self.root / inputs.PACKAGES['xray-core']
        write(self.makefile, f'PKG_NAME:=Xray-core\nPKG_VERSION:=26.10.10\nPKG_SOURCE:=$(PKG_NAME)-$(PKG_VERSION).tar.gz\nPKG_HASH:={digest}\n')
        self.requirements = {'xray-core': inputs.archive_requirement(self.makefile, downloads, fetch=False)}
        self.go = self.root / 'feeds/packages/lang/golang/golang/Makefile'
        write(self.go, 'GO_VERSION_MAJOR_MINOR:=1.26\nGO_VERSION_PATCH:=5\n')

    def test_source_requirement_reproduces_old_go_error(self):
        with self.assertRaisesRegex(api.LockError, r'requires Go >= 1.27.*Go 1.26.5'):
            inputs.check_go(self.root, self.requirements, compiler=False)

    def test_archive_hash_mismatch_is_rejected(self):
        with (self.root / 'dl/Xray-core-26.10.10.tar.gz').open('ab') as stream:
            stream.write(b'tamper')
        with self.assertRaisesRegex(api.LockError, 'archive hash mismatch'):
            inputs.archive_requirement(self.makefile, self.root / 'dl', fetch=False)

    def test_stale_compiler_is_rejected_but_host_rebuild_is_allowed(self):
        write(self.go, 'GO_VERSION_MAJOR_MINOR:=1.27\nGO_VERSION_PATCH:=1\n')
        binary = self.root / 'staging_dir/hostpkg/lib/go-cross/bin/go'
        write(binary, '#!/bin/sh\necho "go version go1.26.5 linux/amd64"\n')
        binary.chmod(0o755)
        with self.assertRaisesRegex(api.LockError, 'compiler is 1.26.5'):
            inputs.check_go(self.root, self.requirements, compiler=True)
        inputs.check_build(self.root, 'package/feeds/packages/golang/host/compile')
        binary.write_text('#!/bin/sh\necho "go version go1.27.1 linux/amd64"\n')
        inputs.check_go(self.root, self.requirements, compiler=True)

    def test_package_version_changed_without_requirement_refresh_is_rejected(self):
        write(self.go, 'GO_VERSION_MAJOR_MINOR:=1.27\nGO_VERSION_PATCH:=1\n')
        self.makefile.write_text(self.makefile.read_text().replace('26.10.10', '26.10.11'))
        with self.assertRaisesRegex(api.LockError, 'source differs'):
            inputs.check_go(self.root, self.requirements, compiler=False)

    def test_unselected_go_package_does_not_require_compiler(self):
        config = self.root / '.config'
        write(config, '# CONFIG_PACKAGE_xray-core is not set\n')
        inputs.check_go(self.root, self.requirements, compiler=True, config=config)


if __name__ == '__main__':
    unittest.main()
