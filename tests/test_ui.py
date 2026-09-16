"""Surgical TOML editing and the `sc ui` web server."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from sc import config, tomlio, ui  # noqa: E402


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


SAMPLE = '''# Shared context: maps pair repositories, permissions grant paths.
# Synced by the sc engine.

[[maps]]
name = "jenny"
from = "SteffenPL/steffen-notes"
to   = "SteffenPL/sc-jenny/steffen-notes"

[[maps]]
name = "joi"
# Henkaku Duties lives at the vault root there and under steffen-notes/ here.
to   = "SteffenPL/sc-joi/steffen-notes"
from = "SteffenPL/steffen-notes"

[[permissions]]
repo = "SteffenPL/steffen-notes"
paths = ["**"]
maps = ["jenny"]
'''


class TomlioTests(unittest.TestCase):
    def setUp(self):
        self.text = SAMPLE

    def applied(self, *ops):
        return tomlio.apply_ops(self.text, list(ops))

    def test_edit_map_touches_only_that_block(self):
        new = self.applied({'op': 'map_upsert', 'entry': {
            'name': 'jenny', 'from': 'SteffenPL/steffen-notes',
            'to': 'SteffenPL/sc-jenny2/steffen-notes'}})
        self.assertIn('to   = "SteffenPL/sc-jenny2/steffen-notes"', new)
        self.assertIn('# Henkaku Duties lives at the vault root', new)
        self.assertNotIn('sc-jenny"', new)
        errors = config.validate(__import__('tomllib').loads(new))
        self.assertEqual(errors, [])

    def test_map_roundtrip_is_byte_identical(self):
        added = self.applied({'op': 'map_upsert', 'entry': {
            'name': 'zoe', 'from': 'A/B', 'to': 'C/D'}})
        self.assertIn('name = "zoe"', added)
        self.assertEqual(tomlio.op_map_remove(added, 'zoe'), self.text)

    def test_map_extras_render_only_when_non_default(self):
        new = self.applied({'op': 'map_upsert', 'entry': {
            'name': 'kai', 'from': 'A/B', 'to': 'C/D', 'merge': 'union',
            'bi_directional': False, 'from_branch': 'trunk'}})
        block = new[new.index('[[maps]]\nname = "kai"'):]
        self.assertIn('merge = "union"', block)
        self.assertIn('bi_directional = false', block)
        self.assertIn('from_branch = "trunk"', block)
        self.assertNotIn('to_branch', block)
        self.assertNotIn('conflict', block)

    def test_add_map_without_permissions_present(self):
        text = '[[maps]]\nname = "solo"\nfrom = "A/B"\nto = "C/D"\n'
        new = tomlio.op_map_upsert(text, {'name': 'duo', 'from': 'A/B', 'to': 'E/F'})
        self.assertIn('[[maps]]\nname = "duo"', new)
        self.assertLess(text.index('name = "solo"'), new.index('name = "duo"'))

    def test_grant_merges_and_creates_entries(self):
        grant = {'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                 'path': 'People/**', 'maps': ['joi']}
        new = self.applied(grant)
        self.assertIn('paths = ["People/**"]\nmaps = ["joi"]', new)
        union = tomlio.apply_ops(new, [dict(grant, maps=['jenny'])])
        self.assertIn('paths = ["People/**"]\nmaps = ["jenny", "joi"]', union)
        self.assertEqual(tomlio.op_perm_grant(union, 'SteffenPL/steffen-notes',
                                             'People/**', ['jenny', 'joi']), union)
        folded = tomlio.apply_ops(new, [dict(grant, maps=['jenny']),
                                        dict(grant, maps=['joi'])])
        self.assertEqual(folded, union)

    def test_grant_appends_to_entry_with_same_maps(self):
        new = self.applied({'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                            'path': 'People/**', 'maps': ['jenny']})
        new = tomlio.apply_ops(new, [{'op': 'perm_grant',
                                      'repo': 'SteffenPL/steffen-notes',
                                      'path': 'Projects/*', 'maps': ['jenny']}])
        self.assertEqual(new.count('[[permissions]]'), 1)
        self.assertIn('    "**",\n    "People/**",\n    "Projects/*",\n', new)

    def test_revoke_splits_entry_and_removes_empty(self):
        new = self.applied({'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                            'path': 'People/**', 'maps': ['jenny', 'joi']})
        split = tomlio.apply_ops(new, [{'op': 'perm_revoke',
                                        'repo': 'SteffenPL/steffen-notes',
                                        'path': 'People/**', 'maps': ['joi']}])
        self.assertEqual(split.count('[[permissions]]'), 2)
        self.assertIn('paths = ["People/**"]\nmaps = ["jenny"]', split)
        self.assertNotIn('maps = ["jenny", "joi"]', split)
        gone = tomlio.apply_ops(split, [{'op': 'perm_revoke',
                                         'repo': 'SteffenPL/steffen-notes',
                                         'path': 'People/**', 'maps': ['jenny']}])
        self.assertNotIn('People/**', gone)
        self.assertEqual(gone.count('[[permissions]]'), 1)

    def test_revoke_splits_multi_path_entry(self):
        text = ('[[maps]]\nname = "m"\nfrom = "A/B"\nto = "C/D"\n\n'
                '[[maps]]\nname = "n"\nfrom = "A/B"\nto = "E/F"\n\n'
                '[[permissions]]\nrepo = "A/B"\n'
                'paths = ["a.md", "b.md"]\nmaps = ["m", "n"]\n')
        out = tomlio.op_perm_revoke(text, 'A/B', 'a.md', ['n'])
        self.assertIn('paths = ["a.md"]\nmaps = ["m"]', out)
        self.assertIn('paths = ["b.md"]\nmaps = ["m", "n"]', out)
        errors = config.validate(__import__('tomllib').loads(out))
        self.assertEqual(errors, [])

    def test_revoke_is_noop_when_only_globs_cover(self):
        self.assertEqual(self.applied({'op': 'perm_revoke',
                                        'repo': 'SteffenPL/steffen-notes',
                                        'path': 'x.md', 'maps': ['jenny']}), self.text)

    def test_absolute_path_entries_grant_and_revoke(self):
        text = '[[maps]]\nname = "m"\nfrom = "A/B"\nto = "C/D"\n\n' \
               '[[permissions]]\npaths = ["A/B/notes.md"]\nmaps = ["m"]\n'
        new = tomlio.op_perm_grant(text, 'A/B', 'logs.md', ['m'])
        self.assertIn('"A/B/logs.md"', new)
        back = tomlio.op_perm_revoke(new, 'A/B', 'logs.md', ['m'])
        self.assertNotIn('logs.md', back)
        self.assertIn('"A/B/notes.md"', back)

    def test_perm_upsert_and_remove_by_index(self):
        new = self.applied({'op': 'perm_upsert', 'index': 0,
                            'entry': {'repo': 'SteffenPL/steffen-notes',
                                      'paths': ['Projects/*'], 'maps': ['jenny']}})
        self.assertNotIn('"**"', new)
        new = tomlio.apply_ops(new, [{'op': 'perm_upsert', 'index': None,
                                       'entry': {'paths': ['A/B/x.md'],
                                                 'maps': ['joi']}}])
        self.assertIn('paths = ["A/B/x.md"]', new)
        new = tomlio.apply_ops(new, [{'op': 'perm_remove', 'index': 1}])
        self.assertNotIn('A/B/x.md', new)

    def test_unknown_map_and_bad_index_raise(self):
        self.assertEqual(tomlio.apply_op(self.text,
                                         {'op': 'map_remove', 'name': 'nobody'}), self.text)
        for op in ({'op': 'perm_remove', 'index': 9},
                   {'op': 'map_upsert', 'entry': {'name': 'x y', 'from': 'A/B', 'to': 'C/D'}},
                   {'op': 'perm_grant', 'repo': 'A/B', 'path': '../escape', 'maps': ['jenny']},
                   {'op': 'nonsense'}):
            with self.assertRaises(tomlio.BlockError):
                tomlio.apply_op(self.text, op)

    def test_multiline_strings_are_rejected(self):
        with self.assertRaises(tomlio.BlockError):
            tomlio.apply_op('[[maps]]\nx = """\n', {'op': 'map_remove', 'name': 'a'})

    def test_build_model_reports_effective_defaults(self):
        model = tomlio.build_model(self.text)
        self.assertEqual([m['name'] for m in model['maps']], ['jenny', 'joi'])
        self.assertTrue(model['maps'][0]['bi_directional'])
        self.assertEqual(model['maps'][0]['merge'], 'text')
        self.assertEqual(model['permissions'][0]['repo'], 'SteffenPL/steffen-notes')
        self.assertEqual(model['permissions'][0]['maps'], ['jenny'])


class UiServerTests(unittest.TestCase):
    """End-to-end over HTTP against a real git repository."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / 'cfgrepo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.org')
        self.config = self.repo / 'sharing.toml'
        self.config.write_text(SAMPLE)
        git(self.repo, 'add', 'sharing.toml')
        git(self.repo, 'commit', '-qm', 'Initial sharing config')
        (self.repo / 'unrelated.txt').write_text('unrelated\n')
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')})
        env.start()
        self.addCleanup(env.stop)

        instance = ui.UI(self.config)
        server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        ui.Handler.ui = instance
        port = server.server_address[1]
        instance.allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        instance.suffix_check = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.base = f'http://127.0.0.1:{port}'
        self.ui = instance
        self.token = self.call('GET', '/api/bootstrap')[1]['token']

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        if getattr(self, 'token', None):
            request.add_header('X-SC-Token', self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {'raw': payload.decode(errors='replace')}
            return error.code, payload

    def bootstrap(self):
        status, payload = self.call('GET', '/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        return payload

    def test_page_and_bootstrap(self):
        with urllib.request.urlopen(self.base + '/', timeout=20) as response:
            page = response.read().decode()
        self.assertIn('<title>sc ui</title>', page)
        payload = self.bootstrap()
        self.assertTrue(payload['git']['repo'])
        self.assertEqual(payload['git']['branch'], 'main')
        self.assertEqual([m['name'] for m in payload['model']['maps']],
                         ['jenny', 'joi'])
        self.assertEqual(len(payload['model']['permissions']), 1)
        self.assertEqual(payload['file_version'],
                         ui._sha(self.config.read_text()))

    def test_save_commits_only_the_config_file(self):
        before = git(self.repo, 'rev-parse', 'HEAD')
        status, payload = self.call('POST', '/api/save', {
            'base_version': self.bootstrap()['file_version'],
            'ops': [{'op': 'map_upsert', 'entry': {'name': 'zoe',
                    'from': 'SteffenPL/steffen-notes', 'to': 'SteffenPL/sc-zoe'}}],
            'commit': True})
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'] and payload['changed'])
        self.assertEqual(payload['commit']['message'], 'Add map zoe')
        after = git(self.repo, 'rev-parse', 'HEAD')
        self.assertNotEqual(before, after)
        self.assertEqual(git(self.repo, 'show', '--name-only', '--format=', 'HEAD'),
                         'sharing.toml')
        text = self.config.read_text()
        self.assertIn('# Henkaku Duties lives at the vault root', text)
        self.assertIn('name = "zoe"', text)
        self.assertIn('unrelated.txt', git(self.repo, 'status', '--porcelain'))

    def test_save_without_git_repo(self):
        plain = self.root / 'plain'
        plain.mkdir()
        self.ui.path = plain / 'sharing.toml'
        self.ui.path.write_text(SAMPLE)
        self.ui.token = 'tok'
        self.token = 'tok'
        status, payload = self.call('POST', '/api/save', {
            'base_version': ui._sha(SAMPLE),
            'ops': [{'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                     'path': 'People/**', 'maps': ['joi']}],
            'commit': True})
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertIn('People/**', self.ui.path.read_text())
        self.assertFalse(payload['git']['repo'])

    def test_save_rejects_stale_version(self):
        status, payload = self.call('POST', '/api/save', {
            'base_version': 'stale', 'ops': [
                {'op': 'map_remove', 'name': 'jenny'}]})
        self.assertEqual(status, 409)
        self.assertFalse(payload['ok'])

    def test_save_rejects_invalid_result_without_writing(self):
        before = self.config.read_text()
        status, payload = self.call('POST', '/api/save', {
            'base_version': self.bootstrap()['file_version'],
            'ops': [{'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                     'path': 'x.md', 'maps': ['nobody']}], 'commit': True})
        self.assertEqual(status, 400)
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['errors'])
        self.assertEqual(self.config.read_text(), before)

    def test_save_noop_makes_no_commit(self):
        before = git(self.repo, 'rev-parse', 'HEAD')
        status, payload = self.call('POST', '/api/save', {
            'base_version': self.bootstrap()['file_version'],
            'ops': [{'op': 'map_upsert', 'entry': {
                'name': 'jenny', 'from': 'SteffenPL/steffen-notes',
                'to': 'SteffenPL/sc-jenny/steffen-notes'}}], 'commit': True})
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['changed'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)

    def test_token_and_host_guards(self):
        data = json.dumps({'ops': [{'op': 'map_remove', 'name': 'jenny'}]}).encode()
        request = urllib.request.Request(self.base + '/api/save', data=data,
                                         method='POST')
        request.add_header('Content-Type', 'application/json')
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(raised.exception.code, 403)
        status, _ = self.call('POST', '/api/save',
                              {'base_version': 'x', 'ops': []},
                              headers={'X-SC-Token': 'wrong'})
        self.assertEqual(status, 403)
        status, _ = self.call('GET', '/api/bootstrap', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)

    def test_preview_returns_diff_and_model(self):
        status, payload = self.call('POST', '/api/preview', {
            'ops': [{'op': 'map_upsert', 'entry': {'name': 'zoe',
                    'from': 'A/B', 'to': 'C/D'}}]})
        self.assertEqual(status, 200)
        self.assertIn('name = "zoe"', payload['diff'])
        self.assertEqual([m['name'] for m in payload['model']['maps']],
                         ['jenny', 'joi', 'zoe'])
        self.assertEqual(self.config.read_text(), SAMPLE)

    def test_active_applies_pending_ops_to_coverage(self):
        files = ['Projects/alpha.md', 'Projects/sub/beta.md', 'secret.md']
        with patch.object(ui, 'repo_tree', return_value=(files, False, 'main')):
            status, payload = self.call('POST', '/api/active', {
                'repo': 'SteffenPL/steffen-notes', 'ops': []})
            self.assertEqual(status, 200)
            by_path = {f['path']: f['maps'] for f in payload['files']}
            self.assertEqual(by_path['Projects/alpha.md'], {'jenny': '**'})
            self.assertEqual(by_path['secret.md'], {'jenny': '**'})
            self.assertEqual(payload['maps'], ['jenny', 'joi'])
            status, payload = self.call('POST', '/api/active', {
                'repo': 'SteffenPL/steffen-notes',
                'ops': [{'op': 'perm_revoke', 'repo': 'SteffenPL/steffen-notes',
                         'path': '**', 'maps': ['jenny']},
                        {'op': 'perm_grant', 'repo': 'SteffenPL/steffen-notes',
                         'path': 'Projects/**', 'maps': ['jenny']}]})
            by_path = {f['path']: f['maps'] for f in payload['files']}
            self.assertEqual(by_path['Projects/alpha.md'],
                             {'jenny': 'Projects/**'})
            self.assertEqual(by_path['Projects/sub/beta.md'],
                             {'jenny': 'Projects/**'})
            self.assertEqual(by_path['secret.md'], {})

    def test_status_reports_local_repositories(self):
        repos = {}
        for name in ('vault', 'collab'):
            repo = self.root / name
            repo.mkdir()
            git(repo, 'init', '-q', '-b', 'main')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@example.org')
            git(repo, 'commit', '-qm', 'base', '--allow-empty')
            repos[name] = repo
        self.config.write_text(
            '[[maps]]\nname = "demo"\n'
            f'from = "{repos["vault"]}"\nto = "{repos["collab"]}"\n\n'
            '[[permissions]]\n'
            f'repo = "{repos["vault"]}"\npaths = ["**"]\nmaps = ["demo"]\n')
        with patch.object(ui.cli, 'list_prs', return_value=[]):
            status, payload = self.call('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['maps'][0]['name'], 'demo')
        self.assertTrue(payload['maps'][0]['from']['tip'])
        self.assertTrue(payload['maps'][0]['to']['tip'])
        self.assertIsNone(payload['maps'][0]['baseline'])
        self.assertEqual(payload['lock'], 'free')
        self.assertIsNone(payload['last_run'])

    def test_commit_message_summaries(self):
        before = SAMPLE
        self.assertEqual(ui.commit_message(
            [{'op': 'perm_grant', 'repo': 'A/B', 'path': 'x/*', 'maps': ['m']}], before),
            'Grant A/B:x/* to m')
        self.assertEqual(ui.commit_message(
            [{'op': 'map_remove', 'name': 'jenny'}], before), 'Remove map jenny')
        self.assertEqual(ui.commit_message(
            [{'op': 'map_upsert', 'entry': {'name': 'jenny'}}], before), 'Edit map jenny')
        self.assertEqual(ui.commit_message(
            [{'op': 'perm_grant', 'repo': 'A/B', 'path': 'x', 'maps': ['m']},
             {'op': 'map_remove', 'name': 'jenny'}], before),
            'Update sharing configuration')


class UiCliTests(unittest.TestCase):
    def test_ui_parses(self):
        from sc import cli
        parser = cli.build_parser()
        args = parser.parse_args(['ui', '-c', 'x.toml', '-p', '9000'])
        self.assertEqual(args.config, Path('x.toml'))
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.host, '127.0.0.1')
        args = parser.parse_args(['ui'])
        self.assertEqual(args.port, 8080)


if __name__ == '__main__':
    unittest.main()
