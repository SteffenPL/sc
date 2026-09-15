"""Command line interface for shared context sync."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sc import __version__, config, engine


def state_dir():
    base = os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))
    directory = Path(base) / 'sc'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_config(argument):
    path = config.resolve(argument)
    if path is None or not path.is_file():
        print(f'No config found{f" ({path})" if argument is not None else ""}; '
              'run `sc init` or pass --config.', file=sys.stderr)
        return None
    return path


def list_prs(repo, branch):
    return json.loads(engine.gh('pr', 'list', '--repo', repo, '--state', 'open',
                                 '--base', branch, '--limit', '1000',
                                 '--json', 'url,number,title,headRefName'))


def run_sync(config_path, initialize=False):
    """Sync every configured collaborator under the host lock. True on failure."""
    try:
        settings = config.load(config_path)
    except (OSError, ValueError) as error:
        print(f'Cannot read config {config_path}: {error}', file=sys.stderr)
        return True
    errors = config.validate(settings)
    if errors:
        print('Invalid configuration:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return True
    os.umask(0o077)
    report, conflicts_total, failed = [], 0, False
    directory = state_dir()
    with (directory / 'sync.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another sc sync already runs on this host.', file=sys.stderr)
            return True
        for name in settings['collaborators']:
            try:
                with tempfile.TemporaryDirectory(prefix='sync-', dir=directory) as tmp:
                    line, conflicts = engine.sync_one(settings, name, Path(tmp), initialize)
                    report.append(line)
                    conflicts_total += conflicts
            except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                # Do not publish command output or private paths to Actions logs.
                report.append(f'{name}: FAILED ({type(error).__name__}); see private sync-error.log.')
                with (directory / 'sync-error.log').open('a') as log:
                    log.write(str(error) + '\n')
                    if isinstance(error, subprocess.CalledProcessError):
                        log.write((error.stderr or b'').decode(errors='replace') + '\n')
                failed = True
        try:
            vault = settings['vault']
            prs = list_prs(vault['repo'], vault.get('branch', 'main'))
            report.append('Vault PRs needing attention:')
            report.extend(f'- #{pr["number"]}: {pr["url"]}' for pr in prs)
            if not prs:
                report.append('- None')
        except subprocess.CalledProcessError:
            report.append('FAILED to list vault PRs.')
            failed = True
    record = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'ok': not failed, 'conflicts': conflicts_total, 'results': report}
    (directory / 'last-run.json').write_text(json.dumps(record, indent=1) + '\n')
    output = '\n'.join(report) + '\n'
    print(output, end='')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(output)
    return failed


def _tip(url, branch):
    """Short branch tip via git ls-remote, or None when missing/unreachable."""
    try:
        result = subprocess.run(['git', 'ls-remote', url, f'refs/heads/{branch}'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, check=True)
        fields = result.stdout.decode().split()
        return fields[0][:12] if fields else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _row(label, value=''):
    print(f'{label:<10}{value}')


def cmd_sync(args):
    path = resolve_config(args.config)
    if path is None:
        return 2
    return int(run_sync(path, args.initialize))


def cmd_watch(args):
    path = resolve_config(args.config)
    if path is None:
        return 2
    try:
        settings = config.load(path)
    except (OSError, ValueError) as error:
        print(f'Cannot read config {path}: {error}', file=sys.stderr)
        return 2
    errors = config.validate(settings)
    if errors:
        print('Invalid configuration:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 2
    sync = settings.get('sync', {})
    interval = args.interval if args.interval is not None else sync.get('interval', 900)
    if interval < 60:
        print('Interval must be at least 60 seconds.', file=sys.stderr)
        return 2
    print(f'Watching {path} — sync every {interval}s, Ctrl+C stops.')
    try:
        while True:
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            failed = run_sync(path, initialize=False)
            print(f'[{stamp}] sync {"ok" if not failed else "FAILED"}; next run in {interval}s')
            time.sleep(interval)
    except KeyboardInterrupt:
        print('Stopped.')
    return 0


def cmd_status(args):
    path = resolve_config(args.config)
    if path is None:
        return 2
    try:
        settings = config.load(path)
    except (OSError, ValueError) as error:
        _row('Config', f'{path} — INVALID')
        _row('Error', str(error))
        return 1
    errors = config.validate(settings)
    _row('Config', f'{path} — ' + ('valid' if not errors else 'INVALID'))
    for error in errors:
        _row('', f'- {error}')
    if not errors:
        vault = settings['vault']
        branch = vault.get('branch', 'main')
        vault_url = engine.repo_url(vault['repo'])
        tip = _tip(vault_url, branch)
        _row('Vault', f'{vault["repo"]} ({branch}) ' +
             (f'@ {tip}' if tip else '— unreachable (credentials? branch?)'))
        prs = None
        try:
            prs = list_prs(vault['repo'], branch)
        except subprocess.CalledProcessError:
            _row('PRs', 'gh unavailable — run `gh auth login`')
        if prs is not None:
            _row('PRs', f'{len(prs)} open vault PR(s)' + ('' if prs else ' — none need attention'))
        for name, entry in settings['collaborators'].items():
            external_branch = entry.get('branch', 'main')
            external_tip = _tip(engine.repo_url(entry['repo']), external_branch)
            _row(name, f'{entry["repo"]} ({external_branch}) ' +
                 (f'@ {external_tip}' if external_tip else '— unreachable'))
            granted = len(config.allowed_paths(settings, name))
            baseline = _tip(vault_url, f'sync-state/{name}')
            details = [f'{granted} granted path(s)',
                       f'baseline sync-state/{name}: ' + (f'@ {baseline}' if baseline else 'MISSING')]
            if baseline is None:
                details.append('new pair? `sc sync --initialize`; lost state? restore the branch')
            if prs is not None:
                review = [pr for pr in prs
                          if str(pr.get('headRefName', '')).startswith(f'sync-review/{name}/')]
                details.append(f'{len(review)} open review PR(s)'
                               + (f', e.g. #{review[0]["number"]}' if review else ''))
            _row('', ' · '.join(details))
    directory = state_dir()
    try:
        with (directory / 'sync.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _row('Lock', 'free')
            except BlockingIOError:
                _row('Lock', 'held (a sync is running)')
    except OSError as error:
        _row('Lock', f'unknown ({error})')
    record_file = directory / 'last-run.json'
    if record_file.is_file():
        try:
            record = json.loads(record_file.read_text())
            stamp = str(record.get('time', ''))[:19].replace('T', ' ')
            outcome = 'ok' if record.get('ok') else 'FAILED'
            _row('Last run', f'{stamp} UTC — {outcome}, {record.get("conflicts", 0)} conflicts')
        except (ValueError, OSError):
            _row('Last run', 'unreadable')
    else:
        _row('Last run', 'never on this host')
    log = directory / 'sync-error.log'
    size = log.stat().st_size if log.is_file() else 0
    _row('Log', f'{log} — ' + ('empty' if not size else f'{size} bytes (details private)'))
    return 1 if errors else 0


def cmd_doctor(args):
    failures = 0

    def check(label, ok, detail='', hint=''):
        nonlocal failures
        state = 'PASS' if ok else 'FAIL'
        if not ok:
            failures += 1
        line = f'[{state}] {label}'
        if detail:
            line += f' — {detail}'
        if not ok and hint:
            line += f'\n       hint: {hint}'
        print(line)

    check('python >= 3.11', sys.version_info >= (3, 11), f'{sys.version.split()[0]}')
    for tool, hint in (('git', 'install git'), ('gh', 'https://cli.github.com/')):
        try:
            version = subprocess.run([tool, '--version'], stdout=subprocess.PIPE,
                                      check=True).stdout.decode().splitlines()[0]
            check(f'{tool} available', True, version)
        except (subprocess.CalledProcessError, OSError):
            check(f'{tool} available', False, hint=hint)
    result = subprocess.run(['gh', 'auth', 'status'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check('gh authenticated', result.returncode == 0,
          '' if result.returncode == 0 else 'not logged in', hint='gh auth login && gh auth setup-git')
    path = config.resolve(args.config)
    if path is None:
        print('[info] no config found — create one with `sc init`')
    elif not path.is_file():
        check(f'config {path}', False, 'file not found')
    else:
        try:
            errors = config.validate(config.load(path))
            check(f'config {path}', not errors, 'valid' if not errors else '; '.join(errors[:3]))
        except (OSError, ValueError) as error:
            check(f'config {path}', False, str(error))
    try:
        directory = state_dir()
        probe = directory / '.probe'
        probe.write_text('')
        probe.unlink()
        check(f'state dir writable ({directory})', True)
    except OSError as error:
        check('state dir writable', False, str(error))
    return 1 if failures else 0


def cmd_prs(args):
    path = resolve_config(args.config)
    if path is None:
        return 2
    try:
        settings = config.load(path)
    except (OSError, ValueError) as error:
        print(f'Cannot read config {path}: {error}', file=sys.stderr)
        return 1
    vault = settings.get('vault', {})
    if not vault.get('repo'):
        print('Config has no [vault] repo.', file=sys.stderr)
        return 1
    try:
        prs = list_prs(vault['repo'], vault.get('branch', 'main'))
    except subprocess.CalledProcessError:
        print('Failed to list vault PRs (gh authenticated?).', file=sys.stderr)
        return 1
    if not prs:
        print('No open vault PRs.')
        return 0
    for pr in prs:
        print(f'#{pr["number"]}: {pr["title"]}')
        print(f'  {pr["url"]}')
    return 0


def cmd_init(args):
    target = args.path
    if target.exists() and not args.force:
        print(f'{target} exists; use --force to overwrite.', file=sys.stderr)
        return 2
    from importlib.resources import files
    template = (files('sc') / 'templates' / 'config.template.toml').read_text()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template)
    print(f'Wrote {target}.')
    print('Next: edit it, check `sc doctor`, then run `sc sync --initialize` once for new pairs.')
    return 0


def cmd_runner(args):
    from sc import deploy
    if args.action == 'install':
        return deploy.install_runner(args)
    if args.action == 'remove':
        return deploy.remove_runner(args)
    return deploy.runner_status(args)


def cmd_workflow(args):
    from sc import deploy
    return deploy.install_workflow(args)


def cmd_help(args):
    parser = build_parser()
    if args.topic:
        parser.parse_args([args.topic, '--help'])  # prints and exits
    parser.print_help()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='sc', description='Allowlisted bidirectional sync between a private '
        'Git vault and collaborator repos.',
        epilog='Run `sc help COMMAND` for details on a command.')
    parser.add_argument('--version', action='version', version=f'sc {__version__}')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')

    sync = sub.add_parser('sync', help='sync every configured collaborator once')
    sync.add_argument('-c', '--config', type=Path, default=None)
    sync.add_argument('--initialize', action='store_true',
                      help='allow an empty baseline for new pairs; '
                      'may restore previously deleted files')
    sync.set_defaults(func=cmd_sync)

    watch = sub.add_parser('watch', help='run sync in a loop')
    watch.add_argument('-c', '--config', type=Path, default=None)
    watch.add_argument('-i', '--interval', type=int, default=None,
                       help='seconds between runs (default 900, or [sync] interval)')
    watch.set_defaults(func=cmd_watch)

    status = sub.add_parser('status', help='show a read-only report (no writes)')
    status.add_argument('-c', '--config', type=Path, default=None)
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser('doctor', help='check prerequisites and configuration')
    doctor.add_argument('-c', '--config', type=Path, default=None)
    doctor.set_defaults(func=cmd_doctor)

    prs = sub.add_parser('prs', help='list open vault PRs needing attention')
    prs.add_argument('-c', '--config', type=Path, default=None)
    prs.set_defaults(func=cmd_prs)

    init = sub.add_parser('init', help='write a commented configuration template')
    init.add_argument('path', nargs='?', type=Path, default=Path('sc.toml'))
    init.add_argument('--force', action='store_true')
    init.set_defaults(func=cmd_init)

    runner = sub.add_parser('runner', help='manage the self-hosted Actions runner')
    runner_sub = runner.add_subparsers(dest='action', required=True)
    for action, help_text in (('install', 'download, register and install a runner'),
                              ('remove', 'unregister and remove the runner'),
                              ('status', 'show local runner state')):
        subparser = runner_sub.add_parser(action, help=help_text)
        if action != 'status':
            subparser.add_argument('--repo', help='GitHub repo (default: vault from config)')
        subparser.add_argument('--dir', help='runner directory '
                           '(default ~/.local/share/sc/runner)')
        if action != 'status':
            subparser.add_argument('-c', '--config', type=Path, default=None)
    runner_install = runner_sub.choices['install']
    runner_install.add_argument('--labels', default='sc-sync',
                                help='runner labels the workflow must match')
    runner_install.add_argument('--service', action=argparse.BooleanOptionalAction, default=True,
                                help='install and start a systemd service (needs sudo)')
    runner_remove = runner_sub.choices['remove']
    runner_remove.add_argument('--service', action=argparse.BooleanOptionalAction, default=True,
                               help='stop and uninstall the systemd service first')
    runner.set_defaults(func=cmd_runner)

    workflow = sub.add_parser('workflow', help='manage the vault sync workflow')
    workflow_sub = workflow.add_subparsers(dest='action', required=True)
    workflow_install = workflow_sub.add_parser('install', help='install the workflow into the vault repo')
    workflow_install.add_argument('--config-repo', required=True,
                                  help='URL of the trusted private config repo to clone')
    workflow_install.add_argument('--config-branch', default='main')
    workflow_install.add_argument('--config-file', default='sharing.toml',
                                  help='config path inside the config repo')
    workflow_install.add_argument('--name', default='sc-sync.yml', help='workflow file name')
    workflow_install.add_argument('-c', '--config', type=Path, default=None)
    workflow.set_defaults(func=cmd_workflow)

    help_parser = sub.add_parser('help', help='show help for a command')
    help_parser.add_argument('topic', nargs='?')
    help_parser.set_defaults(func=cmd_help)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return 2
    return args.func(args)
