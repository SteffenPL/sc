"""Engine integration suite using real Git repositories; PR calls are stubbed."""
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


def make_spec(vault, collab, to_prefix='', patterns=None, **overrides):
    spec = {'name': 'test',
            'from': {'repo': str(vault), 'branch': 'main', 'prefix': ''},
            'to': {'repo': str(collab), 'branch': 'main', 'prefix': to_prefix},
            'merge': 'text', 'conflict': 'swap', 'bi_directional': True,
            'patterns': ['note.md', 'other.md'] if patterns is None else patterns}
    spec.update(overrides)
    return spec


class MapTests(unittest.TestCase):
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
        self.spec = make_spec(self.repos['vault'], self.repos['collab'])
        self.prs, self.closed = [], []
        ensure = patch.object(engine, 'ensure_pr',
                              side_effect=lambda *args: self.prs.append(args))
        ensure.start()
        self.addCleanup(ensure.stop)
        close = patch.object(engine, 'close_stale_prs',
                             side_effect=lambda *args: self.closed.append(args))
        close.start()
        self.addCleanup(close.stop)
        self.edit('vault', 'private.txt', 'secret')
        self.edit('vault', 'note.md', 'one\ntwo\nthree\nfour\nfive\n')
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

    def run_sync(self, initialize=False, dry_run=False):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            return engine.sync_one(self.spec, Path(tmp), initialize, dry_run)

    def test_both_directions_clean_merge_deletion_and_noop(self):
        self.edit('vault', 'note.md', 'ONE\ntwo\nthree\nfour\nfive\n')
        self.edit('collab', 'note.md', 'one\ntwo\nthree\nfour\nFIVE\n')
        self.edit('collab', 'other.md', 'collaborator addition')
        result = self.run_sync()
        self.assertEqual(result['auto_merged'], 1)
        for repo in self.repos.values():
            self.assertEqual((repo / 'note.md').read_text(), 'ONE\ntwo\nthree\nfour\nFIVE\n')
            self.assertEqual((repo / 'other.md').read_text(), 'collaborator addition')
        self.assertFalse((self.repos['collab'] / 'private.txt').exists())
        self.assertNotIn('private.txt', git(self.repos['collab'], 'log', '--all', '--name-only', '--format='))
        self.edit('collab', 'other.md', None)
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'other.md').exists())
        before = [git(r, 'rev-parse', 'main') for r in self.repos.values()]
        self.run_sync()
        self.assertEqual(before, [git(r, 'rev-parse', 'main') for r in self.repos.values()])
        self.assertFalse(self.prs)

    def test_prefix_remaps_paths_and_revocation(self):
        self.spec['to']['prefix'] = 'mirror'
        git(self.repos['vault'], 'update-ref', '-d', 'refs/heads/sync-state/test')
        self.run_sync(initialize=True)
        self.assertEqual((self.repos['collab'] / 'mirror/note.md').read_text(),
                         'one\ntwo\nthree\nfour\nfive\n')
        self.assertTrue((self.repos['collab'] / 'note.md').exists())  # old copy now unmanaged
        self.assertFalse((self.repos['collab'] / 'mirror/private.txt').exists())
        self.edit('collab', 'mirror/note.md', 'one\ntwo\nthree\nfour\nFIVE\n')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(),
                         'one\ntwo\nthree\nfour\nFIVE\n')
        self.edit('collab', 'mirror/unmanaged.md', 'nope')
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'unmanaged.md').exists())
        self.assertTrue((self.repos['collab'] / 'mirror/unmanaged.md').exists())
        self.edit('vault', 'other.md', 'newly shared')
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'mirror/other.md').read_text(), 'newly shared')
        self.spec['patterns'] = ['note.md']
        self.run_sync()
        self.assertFalse((self.repos['collab'] / 'mirror/other.md').exists())
        self.assertTrue((self.repos['vault'] / 'other.md').exists())

    def test_swap_conflict_freezes_prs_both_sides_and_autocloses(self):
        self.edit('vault', 'note.md', 'vault alternative\n')
        self.edit('collab', 'note.md', 'external accepted\n')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'vault alternative\n')
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'external accepted\n')
        self.assertEqual(len(self.prs), 2)
        self.assertEqual(self.prs[0][0], str(self.repos['vault']))
        self.assertEqual(self.prs[1][0], str(self.repos['collab']))
        self.assertEqual(self.prs[0][1], self.prs[1][1])  # same digest branch on both sides
        live = self.closed[-1][2]
        self.assertEqual(live, {self.prs[0][1]})
        # Adopting the other side's version on the vault resolves and auto-closes.
        self.edit('vault', 'note.md', 'external accepted\n')
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'external accepted\n')
        self.assertTrue(self.closed)
        self.assertEqual(self.closed[-1][2], set())
        self.assertEqual(self.closed[-2][2], set())

    def test_to_wins_preserves_source_alternative_in_pr(self):
        self.spec['conflict'] = 'to-wins'
        self.edit('vault', 'note.md', 'vault alternative\n')
        self.edit('collab', 'note.md', 'external accepted\n')
        self.run_sync()
        for repo in self.repos.values():
            self.assertEqual((repo / 'note.md').read_text(), 'external accepted\n')
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(self.prs[0][0], str(self.repos['vault']))
        self.assertEqual(git(self.repos['vault'], 'show', f'{self.prs[0][1]}:note.md'),
                         'vault alternative')

    def test_freeze_conflict_has_no_prs(self):
        self.spec['conflict'] = 'freeze'
        self.edit('vault', 'note.md', 'vault alternative\n')
        self.edit('collab', 'note.md', 'external accepted\n')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'vault alternative\n')
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'external accepted\n')
        self.assertFalse(self.prs)

    def test_union_merge_keeps_both_sides(self):
        self.spec['merge'] = 'union'
        self.edit('vault', 'note.md', 'one\nMILK\nthree\nfour\nfive\n')
        self.edit('collab', 'note.md', 'one\nCALL\nthree\nfour\nfive\n')
        result = self.run_sync()
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(result['auto_merged'], 1)
        for repo in self.repos.values():
            content = (repo / 'note.md').read_text()
            self.assertIn('MILK', content)
            self.assertIn('CALL', content)
        self.assertFalse(self.prs)

    def test_one_way_mirror_overwrites_target_drift(self):
        self.spec['bi_directional'] = False
        self.edit('vault', 'note.md', 'new vault value\n')
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'new vault value\n')
        self.edit('collab', 'note.md', 'target drift\n')
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'new vault value\n')
        self.assertFalse((self.repos['vault'] / 'unmanaged.md').exists())
        self.edit('collab', 'unmanaged.md', 'stays external')
        self.run_sync()
        self.assertFalse((self.repos['vault'] / 'unmanaged.md').exists())
        self.assertTrue((self.repos['collab'] / 'unmanaged.md').exists())
        self.edit('vault', 'note.md', None)
        self.run_sync()
        self.assertFalse((self.repos['collab'] / 'note.md').exists())

    def test_missing_state_does_not_restore_a_target_deletion(self):
        git(self.repos['vault'], 'update-ref', '-d', 'refs/heads/sync-state/test')
        self.edit('collab', 'note.md', None)
        with self.assertRaisesRegex(ValueError, 'initialize'):
            self.run_sync()
        self.assertFalse((self.repos['collab'] / 'note.md').exists())

    def test_rejected_target_push_retries_without_losing_changes(self):
        self.edit('vault', 'note.md', 'new vault value\n')
        hook = self.repos['collab'] / '.git/hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        state = git(self.repos['vault'], 'rev-parse', 'sync-state/test')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(),
                         'one\ntwo\nthree\nfour\nfive\n')
        self.assertEqual(state, git(self.repos['vault'], 'rev-parse', 'sync-state/test'))
        hook.unlink()
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'new vault value\n')

    def test_non_utf8_conflicts_freeze_with_swap_prs(self):
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
        self.assertEqual(len(self.prs), 2, 'non-UTF8 conflict must freeze with swap PRs')
        self.assertNotEqual((self.repos['vault'] / 'note.md').read_bytes(),
                            (self.repos['collab'] / 'note.md').read_bytes())

    def test_dry_run_reports_without_pushing(self):
        self.edit('vault', 'note.md', 'unpublished edit\n')
        self.closed.clear()
        self.prs.clear()
        result = self.run_sync(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(),
                         'one\ntwo\nthree\nfour\nfive\n')
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'unpublished edit\n')
        self.assertFalse(self.prs)
        self.assertFalse(self.closed)


class GlobTests(unittest.TestCase):
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
        self.spec = make_spec(self.repos['vault'], self.repos['collab'],
                              patterns=['*.md', 'Projects/**'])
        self.prs = []
        ensure = patch.object(engine, 'ensure_pr',
                              side_effect=lambda *args: self.prs.append(args))
        ensure.start()
        self.addCleanup(ensure.stop)
        close = patch.object(engine, 'close_stale_prs')
        close.start()
        self.addCleanup(close.stop)

    def edit(self, repo, path, content):
        root = self.repos[repo]
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Edit')

    def run_sync(self, initialize=False):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            return engine.sync_one(self.spec, Path(tmp), initialize)

    def test_globs_expand_on_both_sides(self):
        self.edit('vault', 'private.txt', 'secret')
        self.edit('vault', 'note.md', 'hello')
        self.run_sync(initialize=True)
        self.assertTrue((self.repos['collab'] / 'note.md').exists())
        self.assertFalse((self.repos['collab'] / 'private.txt').exists())
        # New files matching a glob sync from either side, in both directions.
        self.edit('collab', 'from-collab.md', 'imported into vault')
        self.run_sync()
        self.assertEqual((self.repos['vault'] / 'from-collab.md').read_text(), 'imported into vault')
        self.edit('vault', 'Projects/deep/nested.md', 'recursive')
        self.run_sync()
        self.assertEqual((self.repos['collab'] / 'Projects/deep/nested.md').read_text(), 'recursive')
        # Non-matching files stay put.
        self.edit('vault', 'image.png', 'binary-ish')
        self.edit('collab', 'collab.txt', 'not shared')
        self.run_sync()
        self.assertFalse((self.repos['collab'] / 'image.png').exists())
        self.assertFalse((self.repos['vault'] / 'collab.txt').exists())


if __name__ == '__main__':
    unittest.main()
