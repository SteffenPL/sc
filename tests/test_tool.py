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


class LocationTests(unittest.TestCase):
    def test_parse_location(self):
        self.assertEqual(config.parse_location('A/B'), ('A/B', ''))
        self.assertEqual(config.parse_location('A/B/C'), ('A/B', 'C'))
        self.assertEqual(config.parse_location('A/B/C/D'), ('A/B', 'C/D'))
        self.assertEqual(config.parse_location('https://github.com/A/B/C'), ('A/B', 'C'))
        self.assertEqual(config.parse_location('/tmp/some/repo'), ('/tmp/some/repo', ''))

    def test_parse_location_rejects_unsafe(self):
        for value in ('', 'A', 'A/B/../x', 'A/B/.git', 'A/B/C/', 'A/B/*.md', None, 5):
            with self.assertRaises(ValueError, msg=repr(value)):
                config.parse_location(value)


class PatternTests(unittest.TestCase):
    def test_expand_braces(self):
        self.assertEqual(config.expand_braces('a{b,c}d'), ['abd', 'acd'])
        self.assertEqual(config.expand_braces('a{b,{c,d}}e'), ['abe', 'ace', 'ade'])
        self.assertEqual(config.expand_braces('plain'), ['plain'])
        for bad in ('{a', 'a{,b}c'):
            with self.assertRaises(ValueError, msg=bad):
                config.expand_braces(bad)

    def test_pattern_regex(self):
        cases = [
            ('*.md', 'note.md', True), ('*.md', 'a/note.md', False),
            ('Projects/*', 'Projects/x.md', True), ('Projects/*', 'Projects/a/x.md', False),
            ('Projects/**', 'Projects/x.md', True), ('Projects/**', 'Projects/a/b/x.md', True),
            ('Projects/**', 'Projects', False),
            ('a/**/x.md', 'a/x.md', True), ('a/**/x.md', 'a/b/x.md', True),
            ('a?.txt', 'ab.txt', True), ('a?.txt', 'abc.txt', False),
            ('note.md', 'note.md', True), ('note.md', 'other.md', False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(bool(config.pattern_regex(pattern).fullmatch(path)),
                             expected, f'{pattern!r} vs {path!r}')


def sample_settings():
    return {
        'maps': [
            {'name': 'jenny', 'from': 'SteffenPL/steffen-notes',
             'to': 'SteffenPL/sc-jenny-chang'},
            {'name': 'joi', 'from': 'SteffenPL/steffen-notes',
             'to': 'SteffenPL/sc-joi/steffen-notes'},
        ],
        'permissions': [
            {'repo': 'SteffenPL/steffen-notes',
             'paths': ['Projects/Diet.md', '{Projects,Views}/*'],
             'maps': ['jenny']},
            {'paths': ['SteffenPL/steffen-notes/Projects/Henkaku Duties.md'],
             'maps': ['joi']},
            {'paths': ['SteffenPL/sc-joi/steffen-notes/Projects/Joi File.md'],
             'maps': ['joi']},
        ],
        'sync': {'interval': 300},
    }


class ResolveMapsTests(unittest.TestCase):
    def test_resolves_maps_and_patterns(self):
        specs = config.resolve_maps(sample_settings())
        self.assertEqual([spec['name'] for spec in specs], ['jenny', 'joi'])
        jenny, joi = specs
        self.assertEqual(jenny['to']['prefix'], '')
        self.assertEqual(jenny['patterns'],
                         ['Projects/*', 'Projects/Diet.md', 'Views/*'])
        self.assertEqual(joi['to']['prefix'], 'steffen-notes')
        self.assertEqual(joi['patterns'],
                         ['Projects/Henkaku Duties.md', 'Projects/Joi File.md'])
        self.assertEqual(joi['conflict'], 'swap')
        self.assertEqual(joi['merge'], 'text')

    def test_rejects_bad_configs(self):
        base = sample_settings()

        def errors_with(mutation):
            settings = sample_settings()
            mutation(settings)
            return config.validate(settings)

        self.assertEqual(config.validate(sample_settings()), [])
        self.assertTrue(errors_with(lambda s: s.update({'maps': []})))
        self.assertTrue(errors_with(lambda s: s['maps'][0].update({'name': 'joi'})))  # duplicate
        self.assertTrue(errors_with(lambda s: s['maps'][0].update({'merge': 'magic'})))
        self.assertTrue(errors_with(lambda s: s['maps'][0].update({'conflict': 'shout'})))
        self.assertTrue(errors_with(lambda s: s['maps'][0].update({'bi_directional': 'yes'})))
        self.assertTrue(errors_with(lambda s: s['maps'][0].update({'to': 'SteffenPL/steffen-notes/x'})))
        self.assertTrue(errors_with(lambda s: s['permissions'][0].update({'maps': ['ghost']})))
        self.assertTrue(errors_with(lambda s: s['permissions'][0].update(
            {'repo': None, 'paths': ['SteffenPL/other/notes/Projects/x.md']})))
        self.assertTrue(errors_with(lambda s: s['permissions'][0].update(
            {'repo': None, 'paths': ['Projects/x.md']})))  # relative without context
        self.assertTrue(errors_with(lambda s: s['permissions'][0].update(
            {'paths': ['SteffenPL/steffen-notes/[abc].md']})))
        self.assertTrue(errors_with(lambda s: s['permissions'][0].update(
            {'paths': ['SteffenPL/steffen-notes/Projects/../private.md']})))
        self.assertTrue(errors_with(lambda s: s.update({'sync': {'interval': 10}})))
        # A path outside a prefixed map folder is unroutable.
        self.assertTrue(errors_with(lambda s: s.update({
            'maps': s['maps'] + [{'name': 'arch', 'from': 'A/B/Projects', 'to': 'C/D'}],
            'permissions': s['permissions'] + [
                {'paths': ['A/B/README.md'], 'maps': ['arch']}]})))

    def test_validate_missing_maps(self):
        self.assertTrue(config.validate({}))


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
            '[[maps]]\nname = "demo"\n'
            f'from = "{self.repos["vault"]}"\n'
            f'to = "{self.repos["collab"]}"\n\n'
            '[[permissions]]\n'
            f'repo = "{self.repos["vault"]}"\n'
            'paths = ["note.md"]\n'
            'maps = ["demo"]\n')
        (self.repos['vault'] / 'private.txt').write_text('secret\n')
        (self.repos['vault'] / 'note.md').write_text('shared\n')
        git(self.repos['vault'], 'add', '-A')
        git(self.repos['vault'], 'commit', '-qm', 'Initial files')
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state'),
                                      'GITHUB_STEP_SUMMARY': ''})
        env.start()
        self.addCleanup(env.stop)
        ensure = patch.object(engine, 'ensure_pr')
        ensure.start()
        self.addCleanup(ensure.stop)
        close = patch.object(engine, 'close_stale_prs')
        close.start()
        self.addCleanup(close.stop)
        prs = patch.object(cli, 'list_prs', return_value=[])
        prs.start()
        self.addCleanup(prs.stop)

    def test_run_sync_propagates_and_records(self):
        self.assertTrue(cli.run_sync(self.config_path, initialize=True) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'shared\n')
        self.assertFalse((self.repos['collab'] / 'private.txt').exists())
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
        self.config_path.write_text('[[maps]]\nname = "demo"\n'
                                    f'from = "{self.repos["vault"]}"\n'
                                    f'to = "{self.repos["collab"]}"\n\n'
                                    '[[permissions]]\n'
                                    f'repo = "{self.repos["vault"]}"\n'
                                    'paths = ["../escape.md"]\n'
                                    'maps = ["demo"]\n')
        before = [git(r, 'rev-parse', 'main') for r in self.repos.values()]
        self.assertTrue(cli.run_sync(self.config_path, initialize=True))
        self.assertEqual(before, [git(r, 'rev-parse', 'main') for r in self.repos.values()])
        self.assertFalse((self.root / 'state/sc/last-run.json').exists())

    def test_dry_run_leaves_repos_untouched(self):
        self.assertTrue(cli.run_sync(self.config_path, initialize=True) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'shared\n')
        (self.repos['vault'] / 'note.md').write_text('pending edit\n')
        git(self.repos['vault'], 'add', '-A')
        git(self.repos['vault'], 'commit', '-qm', 'Edit')
        self.assertTrue(cli.run_sync(self.config_path, dry_run=True) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'shared\n')
        self.assertEqual((self.repos['vault'] / 'note.md').read_text(), 'pending edit\n')
        # A real run afterwards still propagates the pending edit.
        self.assertTrue(cli.run_sync(self.config_path) is False)
        self.assertEqual((self.repos['collab'] / 'note.md').read_text(), 'pending edit\n')


class CliParserTests(unittest.TestCase):
    def test_subcommands_parse(self):
        parser = cli.build_parser()
        args = parser.parse_args(['sync', '-c', 'x.toml', '--initialize', '--dry-run'])
        self.assertTrue(args.initialize)
        self.assertTrue(args.dry_run)
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
