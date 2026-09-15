"""Configuration, workflow rendering and CLI run_sync tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from sc import cli, config, deploy, engine  # noqa: E402


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


class ConfigTests(unittest.TestCase):
    def test_valid_config_passes(self):
        settings = {'vault': {'repo': 'a/b'},
                   'collaborators': {'ok': {'repo': 'c/d', 'include': ['f.md']}},
                   'shares': [{'paths': ['g.md'], 'to': ['ok']}],
                   'sync': {'interval': 300}}
        self.assertEqual(config.validate(settings), [])

    def test_missing_sections_are_errors(self):
        self.assertTrue(config.validate({}))
        self.assertTrue(config.validate({'vault': {'repo': 'a/b'}}))

    def test_unsafe_paths_are_errors(self):
        for path in ('../secret', '.git/config', '**', '/etc/passwd', 'a/../b'):
            settings = {'vault': {'repo': 'a/b'},
                       'collaborators': {'x': {'repo': 'c/d', 'include': [path]}}}
            errors = config.validate(settings)
            self.assertTrue(any(path in error for error in errors), path)
            self.assertTrue(all('FAILED' not in error for error in errors))

    def test_share_targeting_unknown_collaborator_is_an_error(self):
        settings = {'vault': {'repo': 'a/b'},
                    'collaborators': {'x': {'repo': 'c/d', 'include': ['f.md']}},
                    'shares': [{'paths': ['g.md'], 'to': ['nobody']}]}
        self.assertTrue(any('unknown collaborator' in error for error in config.validate(settings)))

    def test_bad_interval_is_an_error(self):
        for interval in (10, 'x', True, None):
            settings = {'vault': {'repo': 'a/b'},
                       'collaborators': {'x': {'repo': 'c/d', 'include': ['f.md']}},
                       'sync': {'interval': interval}}
            self.assertTrue(any('interval' in error for error in config.validate(settings)), interval)

    def test_prefix_validation(self):
        def errors_with(prefix):
            settings = {'vault': {'repo': 'a/b'},
                        'collaborators': {'x': {'repo': 'c/d', 'include': ['f.md'],
                                                'prefix': prefix}}}
            return config.validate(settings)
        self.assertEqual(errors_with('steffen-notes'), [])
        self.assertEqual(errors_with('a/b'), [])
        for prefix in ('', '../x', '.git', 'a/', '/abs', 'x*', './a', 5, True):
            self.assertTrue(errors_with(prefix), prefix)


class WorkflowRenderTests(unittest.TestCase):
    def test_placeholders_are_replaced(self):
        content = deploy.render_workflow('https://github.com/a/b.git', 'trunk', 'sc.toml')
        for placeholder in ('__SC_CONFIG_REPO__', '__SC_CONFIG_BRANCH__', '__SC_CONFIG_FILE__'):
            self.assertNotIn(placeholder, content)
        self.assertIn('https://github.com/a/b.git', content)
        self.assertIn('--branch trunk', content)
        self.assertIn('sc sync --config "$work/sc.toml"', content)

    def test_template_ships_the_expected_workflow(self):
        content = deploy.render_workflow('u', 'main', 'sharing.toml')
        self.assertIn('runs-on: [self-hosted, linux, sc-sync]', content)
        self.assertIn('permissions: {}', content)


class RunSyncTests(unittest.TestCase):
    """End-to-end through cli.run_sync with real local repos and stubbed PR calls."""

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
        self.config_path = self.root / 'sc.toml'
        self.config_path.write_text(
            f'[vault]\nrepo = "{self.repos["vault"]}"\n'
            f'[collaborators.demo]\nrepo = "{self.repos["collab"]}"\n'
            'include = ["note.md"]\n')
        (self.repos['vault'] / 'private.md').write_text('secret\n')
        (self.repos['vault'] / 'note.md').write_text('shared\n')
        git(self.repos['vault'], 'add', '-A')
        git(self.repos['vault'], 'commit', '-qm', 'Initial files')
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state'),
                                      'GITHUB_STEP_SUMMARY': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_run_sync_propagates_and_records(self):
        prs = patch.object(cli, 'list_prs', return_value=[])
        prs.start()
        self.addCleanup(prs.stop)
        ensure = patch.object(engine, 'ensure_pr')
        ensure.start()
        self.addCleanup(ensure.stop)
        self.assertTrue(cli.run_sync(self.config_path, initialize=True) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'shared\n')
        self.assertFalse((self.repos['collab'] / 'private.md').exists())
        (self.repos['vault'] / 'note.md').write_text('updated\n')
        git(self.repos['vault'], 'add', '-A')
        git(self.repos['vault'], 'commit', '-qm', 'Edit')
        self.assertTrue(cli.run_sync(self.config_path) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'updated\n')
        record = json.loads((self.root / 'state/sc/last-run.json').read_text())
        self.assertTrue(record['ok'])
        self.assertEqual(record['conflicts'], 0)
        self.assertTrue(any('demo:' in line for line in record['results']))

    def test_run_sync_rejects_invalid_config_without_touching_repos(self):
        self.config_path.write_text('[vault]\nrepo = "a/b"\n'
                                    '[collaborators.demo]\nrepo = "c/d"\n'
                                    'include = ["../escape.md"]\n')
        before = [git(r, 'rev-parse', 'main') for r in self.repos.values()]
        self.assertTrue(cli.run_sync(self.config_path, initialize=True))
        self.assertEqual(before, [git(r, 'rev-parse', 'main') for r in self.repos.values()])
        self.assertFalse((self.root / 'state/sc/last-run.json').exists())


class CliParserTests(unittest.TestCase):
    def test_subcommands_parse(self):
        parser = cli.build_parser()
        args = parser.parse_args(['sync', '-c', 'x.toml', '--initialize'])
        self.assertTrue(args.initialize)
        self.assertEqual(args.config, Path('x.toml'))
        args = parser.parse_args(['watch', '-i', '120'])
        self.assertEqual(args.interval, 120)
        args = parser.parse_args(['runner', 'install', '--labels', 'foo'])
        self.assertEqual(args.action, 'install')
        self.assertEqual(args.labels, 'foo')
        args = parser.parse_args(['workflow', 'install', '--config-repo', 'https://x.git'])
        self.assertEqual(args.action, 'install')
        args = parser.parse_args(['help'])
        self.assertEqual(args.topic, None)


if __name__ == '__main__':
    unittest.main()
