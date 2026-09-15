"""Integration suite using real Git repositories; only the GitHub PR API is stubbed."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'src'))

from sc import engine  # noqa: E402


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repos = {}
        for name in ('vault', 'collab'):
            repo = self.root / name
            repo.mkdir()
            git(repo, 'init', '-q', '-b', 'main')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@example.org')
            git(repo, 'config', 'receive.denyCurrentBranch', 'updateInstead')
            git(repo, 'commit', '-qm', 'Initial', '--allow-empty')
            self.repos[name] = repo
        self.settings = {'vault': {'repo': str(self.repos['vault'])},
                         'collaborators': {'test': {'repo': str(self.repos['collab']),
                                                   'include': ['note.md', 'other.md']}}}
        self.prs = []
        mock = patch.object(engine, 'ensure_pr', side_effect=lambda *args: self.prs.append(args))
        mock.start()
        self.addCleanup(mock.stop)
        self.edit('vault', 'private.md', 'secret')
        self.edit('vault', 'note.md', 'one\ntwo\nthree\nfour\nfive\n')
        self.run_sync(initialize=True)

    def edit(self, repo, path, content):
        root = self.repos[repo]
        target = root / path
        if content is None:
            target.unlink()
        else:
            target.write_text(content)
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Edit')

    def run_sync(self, initialize=False):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            return engine.sync_one(self.settings, 'test', Path(tmp), initialize=initialize)

    def test_both_directions_clean_merge_deletion_and_noop(self):
        self.edit('vault', 'note.md', 'ONE\ntwo\nthree\nfour\nfive\n')
        self.edit('collab', 'note.md', 'one\ntwo\nthree\nfour\nFIVE\n')
        self.edit('collab', 'other.md', 'collaborator addition')
        self.run_sync()
        for repo in self.repos.values():
            self.assertEqual((repo / 'note.md').read_text(), 'ONE\ntwo\nthree\nfour\nFIVE\n')
            self.assertEqual((repo / 'other.md').read_text(), 'collaborator addition')
        self.assertFalse((self.repos['collab'] / 'private.md').exists())
        self.assertNotIn('private.md', git(self.repos['collab'], 'log', '--all', '--name-only', '--format='))
        self.edit('collab', 'other.md', None)
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'other.md').exists())
        before = [git(r, 'rev-parse', 'main') for r in self.repos.values()]
        self.run_sync()
        self.assertEqual(before, [git(r, 'rev-parse', 'main') for r in self.repos.values()])
        self.assertFalse(self.prs)

    def test_conflict_accepts_external_and_preserves_vault_in_pr(self):
        self.edit('vault', 'note.md', 'vault alternative\n')
        self.edit('collab', 'note.md', 'external accepted\n')
        self.run_sync()
        for repo in self.repos.values():
            self.assertEqual((repo / 'note.md').read_text(), 'external accepted\n')
        self.assertTrue(self.prs)
        branch = self.prs[-1][1]
        self.assertEqual(git(self.repos['vault'], 'show', f'{branch}:note.md'), 'vault alternative')
        self.assertEqual(git(self.repos['vault'], 'diff', '--name-only', 'main', branch), 'note.md')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'external accepted\n')

    def test_modify_delete_conflict_and_revocation(self):
        self.edit('vault', 'note.md', 'keep this alternative')
        self.edit('collab', 'note.md', None)
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'note.md').exists())
        self.assertTrue(self.prs)
        self.edit('vault', 'other.md', 'previously shared')
        self.run_sync()
        self.settings['collaborators']['test']['include'] = ['note.md']
        self.run_sync()
        self.assertFalse((self.repos['collab'] / 'other.md').exists())
        self.assertTrue((self.repos['vault'] / 'other.md').exists())

    def test_rejected_external_push_retries_without_losing_changes(self):
        self.edit('vault', 'note.md', 'new vault value')
        hook = self.repos['collab'] / '.git/hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        state = git(self.repos['vault'], 'rev-parse', 'sync-state/test')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        self.assertEqual(state, git(self.repos['vault'], 'rev-parse', 'sync-state/test'))
        hook.unlink()
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'new vault value')

    def test_pr_api_failure_is_retried_from_durable_review_branch(self):
        self.edit('vault', 'note.md', 'vault alternative')
        self.edit('collab', 'note.md', 'external wins')
        with patch.object(engine, 'ensure_pr', side_effect=subprocess.CalledProcessError(1, 'gh')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_sync()
        self.assertTrue(git(self.repos['vault'], 'branch', '--list', 'sync-review/*'))
        self.run_sync()
        self.assertTrue(self.prs)
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'external wins')

    def test_missing_state_does_not_restore_a_collaborator_deletion(self):
        git(self.repos['vault'], 'update-ref', '-d', 'refs/heads/sync-state/test')
        self.edit('collab', 'note.md', None)
        with self.assertRaisesRegex(ValueError, 'initialize'):
            self.run_sync()
        self.assertFalse((self.repos['collab'] / 'note.md').exists())

    def test_non_utf8_data_is_not_text_merged(self):
        for repo in self.repos.values():
            (repo / 'note.md').write_bytes(b'one\ntwo\n\xff\nfour\nfive\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'Binary baseline')
        self.run_sync()
        for name, value in [('vault', b'ONE\ntwo\n\xff\nfour\nfive\n'),
                            ('collab', b'one\ntwo\n\xff\nfour\nFIVE\n')]:
            (self.repos[name] / 'note.md').write_bytes(value)
            git(self.repos[name], 'add', '.')
            git(self.repos[name], 'commit', '-qm', 'Binary edit')
        self.run_sync()
        self.assertTrue(self.prs, 'non-UTF8 conflict must preserve the vault alternative')
        self.assertEqual((self.repos['vault'] / 'note.md').read_bytes(),
                         (self.repos['collab'] / 'note.md').read_bytes())

    def test_unsafe_config_is_rejected(self):
        for path in ('../secret', '.git/config', '**', '/etc/passwd'):
            self.settings['collaborators']['test']['include'] = [path]
            with self.assertRaises(ValueError):
                self.run_sync()


class PrefixTests(unittest.TestCase):
    """A collaborator with prefix maps vault paths into a subfolder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repos = {}
        for name in ('vault', 'collab'):
            repo = self.root / name
            repo.mkdir()
            git(repo, 'init', '-q', '-b', 'main')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@example.org')
            git(repo, 'config', 'receive.denyCurrentBranch', 'updateInstead')
            git(repo, 'commit', '-qm', 'Initial', '--allow-empty')
            self.repos[name] = repo
        self.settings = {'vault': {'repo': str(self.repos['vault'])},
                         'collaborators': {'test': {
                             'repo': str(self.repos['collab']),
                             'prefix': 'mirror',
                             'include': ['Projects/note.md']}}}
        self.prs = []
        mock = patch.object(engine, 'ensure_pr', side_effect=lambda *args: self.prs.append(args))
        mock.start()
        self.addCleanup(mock.stop)
        self.edit('vault', 'private.md', 'secret')
        self.edit('vault', 'Projects/note.md', 'one\ntwo\n')
        self.run_sync(initialize=True)

    def edit(self, repo, path, content):
        root = self.repos[repo]
        target = root / path
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Edit')

    def collab_path(self, path):
        return self.repos['collab'] / 'mirror' / path

    def run_sync(self, initialize=False):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            return engine.sync_one(self.settings, 'test', Path(tmp), initialize=initialize)

    def test_prefix_mapping_roundtrip(self):
        self.assertEqual(self.collab_path('Projects/note.md').read_text(), 'one\ntwo\n')
        self.assertFalse((self.repos['collab'] / 'Projects/note.md').exists())
        self.assertFalse(self.collab_path('private.md').exists())
        # Collaborator edit at the mapped path propagates back to the vault.
        self.edit('collab', 'mirror/Projects/note.md', 'ONE\ntwo\n')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'Projects/note.md').read_text(), 'ONE\ntwo\n')
        # Unmanaged files under the prefix stay external and are ignored.
        self.edit('collab', 'mirror/Projects/unlisted.md', 'not granted')
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'Projects/unlisted.md').exists())
        self.assertTrue(self.collab_path('Projects/unlisted.md').exists())
        # Revocation removes only the mapped copy.
        self.settings['collaborators']['test']['include'] = []
        self.run_sync()
        self.assertFalse(self.collab_path('Projects/note.md').exists())
        self.assertTrue((self.repos['vault'] / 'Projects/note.md').exists())

    def test_prefix_conflict_preserves_vault_alternative(self):
        self.edit('vault', 'Projects/note.md', 'vault alternative\n')
        self.edit('collab', 'mirror/Projects/note.md', 'external wins\n')
        self.run_sync()
        self.assertEqual(self.collab_path('Projects/note.md').read_text(), 'external wins\n')
        self.assertEqual((self.repos['vault'] / 'Projects/note.md').read_text(), 'external wins\n')
        self.assertTrue(self.prs)
        branch = self.prs[-1][1]
        self.assertEqual(git(self.repos['vault'], 'show', f'{branch}:Projects/note.md'),
                         'vault alternative')

    def test_unsafe_prefix_is_rejected(self):
        for prefix in ('../x', '.git', 'a/', '/abs', 'x*', './a'):
            self.settings['collaborators']['test']['prefix'] = prefix
            with self.assertRaises(ValueError):
                self.run_sync()


if __name__ == '__main__':
    unittest.main()
