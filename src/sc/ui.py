"""Local web UI for editing the sharing configuration (`sc ui`).

A stdlib-only HTTP server bound to localhost. The browser buffers edits as
operations; `Commit` applies them surgically to the config file (see
sc.tomlio), validates the result and — when the file lives in a Git
repository — commits exactly that file. Nothing is ever pushed.

Mutating requests must carry the per-run token printed at startup, and the
Host header is checked against the bound address, so a malicious web page
cannot drive the UI from another origin. `gh` provides the repository list
and the live file tree of a mapped repository.
"""
import difflib
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sc import __version__, cli, config, engine, tomlio

REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
BRANCH = re.compile(r'^[^\s~^:?*[\x00-\x1f]+$')
SYNC_SNIPPET = 'import sys\nfrom sc.cli import main\nsys.exit(main(sys.argv[1:]))'
WATCH_SNIPPET = SYNC_SNIPPET
BODY_LIMIT = 5 * 1024 * 1024


class UIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _git(directory, *args, check=True):
    result = subprocess.run(['git', '-C', str(directory), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if check and result.returncode != 0:
        raise UIError(500, 'git failed: ' + result.stderr.decode(errors='replace').strip())
    return result


def git_info(path):
    """Repository state of the config file: branch, tip, dirty flags."""
    directory = path.parent
    info = {'repo': False, 'branch': None, 'head': None, 'file_dirty': False,
            'other_dirty': False}
    try:
        if _git(directory, 'rev-parse', '--is-inside-work-tree').stdout.strip() != b'true':
            return info
    except UIError:
        return info
    info['repo'] = True
    branch = _git(directory, 'branch', '--show-current').stdout.decode().strip()
    info['branch'] = branch or None
    head = _git(directory, 'rev-parse', '--short', 'HEAD', check=False)
    if head.returncode == 0:
        info['head'] = head.stdout.decode().strip()
    try:
        root = Path(_git(directory, 'rev-parse', '--show-toplevel').stdout.decode().strip())
        relative = os.path.relpath(path, root)
    except (UIError, ValueError):
        relative, root = None, None
    status = _git(directory, 'status', '--porcelain', '--',
                  str(path.resolve()), check=False).stdout.decode(errors='replace')
    info['file_dirty'] = bool(status.strip())
    if relative is not None:
        everything = _git(directory, 'status', '--porcelain', check=False).stdout.decode(errors='replace')
        lines = [line for line in everything.splitlines() if line.strip()]
        info['other_dirty'] = any(relative not in line for line in lines)
    return info


def commit_file(path, message):
    """Commit exactly the config file; never stage or commit anything else.
    Returns the short commit sha, or None when there was nothing to commit."""
    directory = path.parent
    pending = _git(directory, 'status', '--porcelain', '--', str(path.resolve()),
                   check=False).stdout.strip()
    if not pending:
        return None
    target = str(path.resolve())
    if _git(directory, 'rev-parse', '--verify', '-q', 'HEAD', check=False).returncode == 0:
        _git(directory, 'commit', '-m', message, '--', target)
    else:
        _git(directory, 'add', '--', target)
        _git(directory, 'commit', '-m', message)
    return _git(directory, 'rev-parse', '--short', 'HEAD').stdout.decode().strip()


def commit_message(ops, before):
    """A short imperative message describing the batch, in repo style."""
    if len(ops) == 1:
        op = ops[0]
        kind = op.get('op')
        try:
            if kind == 'map_upsert':
                name = op['entry'].get('name')
                existed = tomlio._map_block(tomlio.parse_blocks(before), name) is not None
                return f'{"Edit" if existed else "Add"} map {name}'
            if kind == 'map_remove':
                return f"Remove map {op.get('name')}"
            if kind == 'perm_grant':
                return (f"Grant {op.get('repo')}:{op.get('path')} "
                        f"to {', '.join(op.get('maps', []))}")
            if kind == 'perm_revoke':
                return (f"Revoke {op.get('repo')}:{op.get('path')} "
                        f"from {', '.join(op.get('maps', []))}")
            if kind == 'perm_upsert':
                return ('Edit permission entry' if op.get('index') is not None
                        else 'Add permission entry')
            if kind == 'perm_remove':
                return 'Remove permission entry'
        except (KeyError, TypeError, ValueError, tomlio.BlockError):
            pass
    return 'Update sharing configuration'


def model_of(text):
    """Parsed view of a config text plus its validation errors."""
    errors = []
    try:
        settings = tomllib.loads(text)
        errors = config.validate(settings)
    except (tomllib.TOMLDecodeError, ValueError, TypeError) as error:
        errors = [f'cannot parse config: {error}']
    try:
        model = tomlio.build_model(text)
    except (tomllib.TOMLDecodeError, ValueError, TypeError) as error:
        model = {'maps': [], 'permissions': [], 'unparsable': True,
                 'parse_error': str(error)}
    model['errors'] = errors
    return model


def list_repos(query):
    out = engine.gh('repo', 'list', '--limit', '300',
                    '--json', 'nameWithOwner,visibility,isFork,isPrivate')
    repos = json.loads(out)
    needle = (query or '').lower()
    if needle:
        repos = [repo for repo in repos if needle in repo['nameWithOwner'].lower()]
    return sorted(repos, key=lambda repo: repo['nameWithOwner'].lower())


def repo_tree(repo, branch=None):
    """Live file list of a repository via the gh API."""
    if not REPO.fullmatch(repo or ''):
        raise UIError(400, f'invalid repository: {repo!r}')
    if not branch:
        branch = json.loads(engine.gh('api', f'repos/{repo}')).get('default_branch', 'main')
    if not BRANCH.fullmatch(branch):
        raise UIError(400, f'invalid branch: {branch!r}')
    data = json.loads(engine.gh(
        'api', f'repos/{repo}/git/trees/{branch}?recursive=1'))
    files = [entry['path'] for entry in data.get('tree', [])
             if entry.get('type') == 'blob']
    files.sort()
    truncated = bool(data.get('truncated')) or len(files) > 20000
    return files[:20000], truncated, branch


def coverage(files, settings, repo):
    """Per-file active maps: {path: {map: matching pattern}}."""
    specs = config.resolve_maps(settings)
    sides = {}
    for spec in specs:
        regexes = [(pattern, config.pattern_regex(pattern)) for pattern in spec['patterns']]
        for side in ('from', 'to'):
            if spec[side]['repo'] == repo:
                sides[spec['name']] = (spec[side]['prefix'], regexes)
    result = {}
    for path in files:
        active = {}
        for name, (prefix, regexes) in sides.items():
            logical = path
            if prefix:
                if not path.startswith(prefix + '/'):
                    continue
                logical = path[len(prefix) + 1:]
            for pattern, regex in regexes:
                if regex.fullmatch(logical):
                    active[name] = pattern
                    break
        result[path] = active
    return result, sorted(sides)


def _ls_remote_tips(repo, refs):
    """{branch: short sha} for the wanted branch names — one ls-remote.
    Missing branches (and unreachable repositories) map to None."""
    if not refs:
        return {}
    args = ['git', 'ls-remote', engine.repo_url(repo)]
    args += [f'refs/heads/{ref}' for ref in sorted(refs)]
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                                 check=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {ref: None for ref in refs}
    tips = {}
    for line in result.stdout.decode().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].startswith('refs/heads/'):
            tips[fields[1][len('refs/heads/'):]] = fields[0][:12]
    return {ref: tips.get(ref) for ref in refs}


def _repo_prs(repo):
    """Open PRs of a repository, or None when gh fails."""
    try:
        result = subprocess.run(
            ['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--limit', '1000',
             '--json', 'url,number,title,headRefName'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, **engine.BOT}, check=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _repo_state(repo, refs):
    return {'tips': _ls_remote_tips(repo, refs), 'prs': _repo_prs(repo)}


def status_payload(path):
    """A structured `sc status`: map tips, baselines, PRs, host state.

    One ls-remote per repository (tips and baselines together) and one gh
    call per repository, all in parallel, so the report takes a round trip
    instead of a round trip per map.
    """
    payload = {'ok': True, 'config': str(path)}
    try:
        settings = config.load(path)
    except (OSError, ValueError) as error:
        return {'ok': False, 'error': f'cannot read config: {error}'}
    payload['errors'] = config.validate(settings)
    specs = [] if payload['errors'] else config.resolve_maps(settings)
    wanted = {}
    for spec in specs:
        wanted.setdefault(spec['from']['repo'], set()).update(
            {spec['from']['branch'], f'sync-state/{spec["name"]}'})
        wanted.setdefault(spec['to']['repo'], set()).add(spec['to']['branch'])
    with ThreadPoolExecutor(max_workers=max(1, min(12, len(wanted) or 1))) as pool:
        futures = {repo: pool.submit(_repo_state, repo, refs)
                   for repo, refs in wanted.items()}
        state = {repo: future.result() for repo, future in futures.items()}
    prs_error = ('gh unavailable — run `gh auth login`'
                 if any(info['prs'] is None for info in state.values()) else None)
    maps = []
    for spec in specs:
        source, target = spec['from'], spec['to']
        source_info = state.get(source['repo'], {'tips': {}, 'prs': None})
        target_info = state.get(target['repo'], {'tips': {}, 'prs': None})
        reviews = {}
        for side, info in ((source, source_info), (target, target_info)):
            found = [pr['number'] for pr in (info['prs'] or [])
                     if str(pr.get('headRefName', '')).startswith(f'sync-review/{spec["name"]}/')]
            if found:
                reviews[side['repo']] = found
        maps.append({'name': spec['name'],
                     'from': {'repo': source['repo'], 'branch': source['branch'],
                              'prefix': source['prefix'],
                              'tip': source_info['tips'].get(source['branch'])},
                     'to': {'repo': target['repo'], 'branch': target['branch'],
                            'prefix': target['prefix'],
                            'tip': target_info['tips'].get(target['branch'])},
                     'baseline': source_info['tips'].get(f'sync-state/{spec["name"]}'),
                     'patterns': len(spec['patterns']),
                     'bi_directional': spec['bi_directional'], 'merge': spec['merge'],
                     'conflict': spec['conflict'], 'review_prs': reviews})
    payload['maps'], payload['prs_error'] = maps, prs_error
    payload['watch'] = cli.watch_state(path)
    payload['prs'] = {repo: [{'number': pr['number'], 'title': pr['title'], 'url': pr['url']}
                             for pr in (info['prs'] or [])]
                      for repo, info in state.items()}
    lock = None
    try:
        with (cli.state_dir() / 'sync.lock').open('w') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock = 'free'
            except BlockingIOError:
                lock = 'held'
    except OSError:
        lock = 'unknown'
    payload['lock'] = lock
    record_file = cli.state_dir() / 'last-run.json'
    try:
        payload['last_run'] = json.loads(record_file.read_text())
    except (OSError, ValueError):
        payload['last_run'] = None
    return payload


class UI:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.token = secrets.token_urlsafe(12)
        self.job = None
        self.job_lock = threading.Lock()
        self.watch_proc = None

    def watch_interval(self):
        try:
            settings = config.load(self.path)
            return settings.get('sync', {}).get('interval', 900)
        except (OSError, ValueError):
            return 900

    def watch_info(self):
        return {**cli.watch_state(self.path), 'interval': self.watch_interval()}

    def watch_start(self, wait=10.0):
        """Start a detached `sc watch` for this config; returns its state."""
        state = cli.watch_state(self.path)
        if state['running']:
            return {**state, 'already': True}
        log = cli.state_dir() / 'watch.log'
        with log.open('ab') as handle:
            self.watch_proc = subprocess.Popen(
                [sys.executable, '-c', WATCH_SNIPPET, 'watch', '--config', str(self.path)],
                stdout=handle, stderr=subprocess.STDOUT, cwd=str(self.path.parent),
                start_new_session=True, env={**os.environ, 'SC_WATCH_FROM_UI': '1'})
        deadline = time.time() + wait
        while time.time() < deadline:
            state = cli.watch_state(self.path)
            if state['running']:
                return state
            if self.watch_proc.poll() is not None:
                break
            time.sleep(0.25)
        state = cli.watch_state(self.path)
        if state['running']:
            return state
        detail = ''
        try:
            with log.open('rb') as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 1500))
                detail = handle.read().decode(errors='replace').strip()
        except OSError:
            pass
        raise UIError(500, 'the watch loop exited immediately' +
                      (f': {detail}' if detail else ''))

    def watch_stop(self, wait=15.0):
        """SIGINT the running watch loop; returns its state afterwards."""
        state = cli.watch_state(self.path)
        if not state['running']:
            return state
        try:
            os.kill(state['pid'], signal.SIGINT)
        except OSError as error:
            raise UIError(500, f'cannot signal the watch process: {error}') from None
        deadline = time.time() + wait
        while time.time() < deadline:
            state = cli.watch_state(self.path)
            if not state['running']:
                return state
            time.sleep(0.25)
        raise UIError(504, 'the watch loop keeps running — it may be mid-sync; try again')

    def sync_status(self):
        job = self.job
        if job is None:
            return {'running': False, 'log': '', 'started': None, 'returncode': None}
        running = job['proc'].poll() is None
        log = ''
        try:
            with job['log'].open('rb') as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8000))
                log = handle.read().decode(errors='replace')
        except OSError:
            pass
        return {'running': running, 'log': log, 'started': job['started'],
                'returncode': job['proc'].poll()}

    def start_sync(self):
        with self.job_lock:
            if self.job is not None and self.job['proc'].poll() is None:
                raise UIError(409, 'a sync is already running')
            log = cli.state_dir() / 'ui-sync.log'
            handle = log.open('w')
            proc = subprocess.Popen(
                [sys.executable, '-c', SYNC_SNIPPET, 'sync', '--config', str(self.path)],
                stdout=handle, stderr=subprocess.STDOUT, cwd=str(self.path.parent))
            self.job = {'proc': proc, 'log': log,
                        'started': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        return self.sync_status()


class Handler(BaseHTTPRequestHandler):
    ui = None
    server_version = f'sc-ui/{__version__}'
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    # responses ----------------------------------------------------------
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _page(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # request plumbing ---------------------------------------------------
    def _host_ok(self):
        host = (self.headers.get('Host') or '').strip()
        allowed = self.ui.allowed_hosts
        return host in allowed or (self.ui.suffix_check and host.endswith(self.ui.suffix_check))

    def _token_ok(self):
        given = self.headers.get('X-SC-Token') or ''
        return secrets.compare_digest(given, self.ui.token)

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise UIError(400, 'invalid Content-Length')
        if length > BODY_LIMIT:
            raise UIError(413, 'request too large')
        raw = self.rfile.read(length) if length else b''
        if not raw:
            raise UIError(400, 'empty request body')
        if 'application/json' not in (self.headers.get('Content-Type') or ''):
            raise UIError(415, 'Content-Type must be application/json')
        try:
            body = json.loads(raw)
        except ValueError:
            raise UIError(400, 'invalid JSON body')
        if not isinstance(body, dict):
            raise UIError(400, 'request body must be a JSON object')
        return body

    def _guard(self, post):
        if not self._host_ok():
            raise UIError(403, 'bad Host header')
        if post and not self._token_ok():
            raise UIError(403, 'missing or wrong token; reload the page')

    # routes -------------------------------------------------------------
    def do_GET(self):
        try:
            self._guard(post=False)
            route = urlparse(self.path)
            if route.path == '/':
                return self._page(
                    (files('sc') / 'templates' / 'ui.html').read_text())
            if route.path == '/favicon.ico':
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if route.path == '/api/bootstrap':
                return self.api_bootstrap()
            if route.path == '/api/repos':
                return self.api_repos(parse_qs(route.query).get('q', [''])[0])
            if route.path == '/api/status':
                return self._json(200, status_payload(self.ui.path))
            if route.path == '/api/sync':
                return self._json(200, self.ui.sync_status())
            if route.path == '/api/watch':
                return self._json(200, self.ui.watch_info())
            raise UIError(404, 'not found')
        except UIError as error:
            self._json(error.status, {'ok': False, 'error': error.message})
        except Exception:
            traceback.print_exc()
            self._json(500, {'ok': False, 'error': 'internal error '
                         f'(see {cli.state_dir() / "sync-error.log"} for details)'})
            with (cli.state_dir() / 'sync-error.log').open('a') as log:
                log.write(f'[{datetime.now(timezone.utc).isoformat()}] ui error\n')
                traceback.print_exc(file=log)

    def do_POST(self):
        try:
            self._guard(post=True)
            route = urlparse(self.path)
            body = self._body()
            if route.path == '/api/preview':
                return self.api_preview(body)
            if route.path == '/api/active':
                return self.api_active(body)
            if route.path == '/api/save':
                return self.api_save(body)
            if route.path == '/api/sync':
                return self._json(200, self.ui.start_sync())
            if route.path == '/api/watch':
                action = body.get('action')
                if action == 'start':
                    return self._json(200, {**self.ui.watch_start(), 'ok': True})
                if action == 'stop':
                    return self._json(200, {**self.ui.watch_stop(), 'ok': True})
                raise UIError(400, "action must be 'start' or 'stop'")
            raise UIError(404, 'not found')
        except UIError as error:
            self._json(error.status, {'ok': False, 'error': error.message})
        except tomlio.BlockError as error:
            self._json(400, {'ok': False, 'error': str(error)})
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b'').decode(errors='replace').strip()
            self._json(502, {'ok': False, 'error': f'gh failed: {detail or "see logs"}'})
        except Exception:
            traceback.print_exc()
            self._json(500, {'ok': False, 'error': 'internal error; see console'})
            with (cli.state_dir() / 'sync-error.log').open('a') as log:
                log.write(f'[{datetime.now(timezone.utc).isoformat()}] ui error\n')
                traceback.print_exc(file=log)

    # endpoints ----------------------------------------------------------
    def api_bootstrap(self):
        path = self.ui.path
        text = path.read_text()
        payload = {'ok': True, 'version': __version__, 'token': self.ui.token,
                   'config_path': str(path), 'file_version': _sha(text),
                   'git': git_info(path), 'model': model_of(text),
                   'watch': self.ui.watch_info()}
        return self._json(200, payload)

    def api_repos(self, query):
        return self._json(200, {'ok': True, 'repos': list_repos(query)})

    def _applied(self, body):
        """Config text with the pending operations applied, or a UIError."""
        text = self.ui.path.read_text()
        try:
            applied = tomlio.apply_ops(text, body.get('ops', []))
        except (tomlio.BlockError, tomllib.TOMLDecodeError) as error:
            raise UIError(400, f'cannot edit this config: {error}') from None
        return text, applied

    def api_preview(self, body):
        text, applied = self._applied(body)
        diff = None
        if applied != text:
            diff = ''.join(difflib.unified_diff(
                text.splitlines(True), applied.splitlines(True),
                fromfile=str(self.ui.path), tofile='(edited)'))
        return self._json(200, {'ok': True, 'file_version': _sha(text),
                                'model': model_of(applied), 'diff': diff})

    def api_active(self, body):
        _, applied = self._applied(body)
        settings = tomllib.loads(applied)
        errors = config.validate(settings)
        if errors:
            raise UIError(400, '; '.join(errors))
        repo = body.get('repo')
        files, truncated, branch = repo_tree(repo, body.get('branch'))
        covered, maps = coverage(files, settings, repo)
        return self._json(200, {'ok': True, 'repo': repo, 'branch': branch,
                                'truncated': truncated,
                                'files': [{'path': path, 'maps': covered[path]}
                                          for path in files], 'maps': maps})

    def api_save(self, body):
        path = self.ui.path
        text = path.read_text()
        if body.get('base_version') != _sha(text):
            raise UIError(409, 'the config file changed on disk since the page '
                           'was loaded; reload the page and redo your edits')
        ops = body.get('ops')
        if not isinstance(ops, list) or not ops:
            raise UIError(400, 'nothing to save')
        try:
            applied = tomlio.apply_ops(text, ops)
        except (tomlio.BlockError, tomllib.TOMLDecodeError) as error:
            raise UIError(400, f'cannot edit this config: {error}') from None
        errors = config.validate(tomllib.loads(applied))
        if errors:
            return self._json(400, {'ok': False, 'errors': errors})
        commit = {'sha': None, 'message': None, 'error': None}
        changed = applied != text
        if changed:
            path.write_text(applied)
            if body.get('commit', True) and git_info(path)['repo']:
                try:
                    message = body.get('message') or commit_message(ops, text)
                    sha = commit_file(path, message)
                    commit = {'sha': sha, 'message': message if sha else None,
                              'error': None}
                except UIError as error:
                    commit = {'sha': None, 'message': None, 'error': error.message}
        return self._json(200, {'ok': True, 'changed': changed, 'commit': commit,
                                'git': git_info(path), 'file_version': _sha(applied),
                                'model': model_of(applied)})


def serve(path, port, host):
    """Run the UI server until interrupted."""
    ui = UI(path)
    ui.allowed_hosts = {f'{h}:{port}' for h in {host, '127.0.0.1', 'localhost'}}
    ui.suffix_check = f':{port}' if host not in ('127.0.0.1', 'localhost', '::1') else None
    Handler.ui = ui
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    display_host = '127.0.0.1' if host == '0.0.0.0' else host
    print(f'sc {__version__} ui — editing {path}', flush=True)
    print(f'Git: {git_info(path)["branch"] or "not a repository"}', flush=True)
    print(f'Open http://{display_host}:{port}  (token: {ui.token})', flush=True)
    print('Ctrl+C stops. Edits are buffered in the browser and committed on demand.',
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        httpd.server_close()
    return 0
