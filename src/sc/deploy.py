"""Deploy a self-hosted GitHub Actions runner and the vault sync workflow."""
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from importlib.resources import files
from pathlib import Path

from sc import engine


def _source_repo(args):
    from sc import config
    path = config.resolve(getattr(args, 'config', None))
    if path is None or not path.is_file():
        print('No config found; run `sc init` or pass --config.', file=sys.stderr)
        return None
    try:
        settings = config.load(path)
        specs = config.resolve_maps(settings)
    except (OSError, ValueError) as error:
        print(f'Cannot resolve config {path}: {error}', file=sys.stderr)
        return None
    if not specs:
        print('Config defines no maps.', file=sys.stderr)
        return None
    return specs[0]['from']['repo'], specs[0]['from']['branch']


def _runner_dir(argument):
    return Path(argument).expanduser() if argument else Path.home() / '.local/share/sc/runner'


def render_workflow(config_repo, config_branch, config_file):
    """Fill the workflow template with the trusted config repo location."""
    template = (files('sc') / 'templates' / 'sync.workflow.yml').read_text()
    return (template.replace('__SC_CONFIG_REPO__', config_repo)
                    .replace('__SC_CONFIG_BRANCH__', config_branch)
                    .replace('__SC_CONFIG_FILE__', config_file))


def install_workflow(args):
    source = _source_repo(args)
    if source is None:
        return 1
    source_repo, source_branch = source
    content = render_workflow(args.config_repo, args.config_branch, args.config_file)
    path = f'.github/workflows/{args.name}'
    sha = None
    try:
        existing = json.loads(engine.gh('api', f'repos/{vault_repo}/contents/{path}'))
        sha = existing.get('sha')
    except subprocess.CalledProcessError:
        sha = None
    payload = ['-f', f'message={"Update" if sha else "Install"} sc sync workflow',
               '-f', 'content=' + base64.b64encode(content.encode()).decode(),
               '-f', f'branch={vault_branch}']
    if sha:
        payload += ['-f', f'sha={sha}']
    result = json.loads(engine.gh('api', '-X', 'PUT',
                                  f'repos/{vault_repo}/contents/{path}', *payload))
    print(f'{"Updated" if sha else "Installed"} {path} on {vault_repo}@{vault_branch} '
          f'(commit {result["commit"]["sha"][:12]}).')
    try:
        listing = json.loads(engine.gh('api', f'repos/{vault_repo}/contents/.github/workflows'))
        legacy = [entry['name'] for entry in listing
                  if entry['name'] != args.name
                  and ('copybara' in entry['name'].lower() or 'sync' in entry['name'].lower())]
        if legacy:
            print(f'Warning: other sync workflows exist ({", ".join(legacy)}); '
                  'disable or remove them to avoid double-syncing.')
    except subprocess.CalledProcessError:
        pass
    return 0


def _arch():
    machine = platform.machine().lower()
    return {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)


def _extract(tarball, directory):
    with tarfile.open(tarball) as tar:
        try:
            tar.extractall(directory, filter='data')
        except TypeError:  # Python < 3.11.4 has no extraction filters
            for member in tar.getmembers():
                if member.name.startswith('/') or '..' in Path(member.name).parts:
                    print(f'Refusing unsafe tar member {member.name!r}.', file=sys.stderr)
                    return False
            tar.extractall(directory)
    return True


def install_runner(args):
    if os.geteuid() == 0:
        print('Refusing to run as root; use a dedicated user account.', file=sys.stderr)
        return 1
    arch = _arch()
    if not arch:
        print(f'Unsupported architecture {platform.machine()}.', file=sys.stderr)
        return 1
    if args.repo:
        repo = args.repo
    else:
        source = _source_repo(args)
        if source is None:
            return 1
        repo = source[0]
    directory = _runner_dir(args.dir)
    if (directory / '.runner').exists():
        print(f'{directory} is already configured; run `sc runner remove` first.',
              file=sys.stderr)
        return 1
    release = json.loads(engine.gh('api', 'repos/actions/runner/releases/latest'))
    version = release['tag_name'].lstrip('v')
    asset = f'actions-runner-linux-{arch}-{version}.tar.gz'
    assets = {entry['name']: entry['browser_download_url'] for entry in release['assets']}
    if asset not in assets:
        print(f'No runner asset {asset} in the latest release.', file=sys.stderr)
        return 1
    token = json.loads(engine.gh('api', '-X', 'POST',
                                 f'repos/{repo}/actions/runners/registration-token'))['token']
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / asset
        print(f'Downloading {asset} ...')
        urllib.request.urlretrieve(assets[asset], tarball)
        checksum = assets.get(asset + '.sha256')
        if checksum:
            digest = urllib.request.urlopen(checksum).read().decode().split()[0]
            if hashlib.sha256(tarball.read_bytes()).hexdigest() != digest:
                print('Runner download failed checksum verification.', file=sys.stderr)
                return 1
        else:
            print('Note: no published checksum for this asset; downloaded over HTTPS only.')
        print(f'Extracting to {directory} ...')
        if not _extract(tarball, directory):
            return 1
    subprocess.run(['./config.sh', '--url', f'https://github.com/{repo}', '--token', token,
                   '--labels', args.labels, '--unattended', '--replace'],
                  cwd=directory, check=True)
    if args.service:
        try:
            subprocess.run(['sudo', './svc.sh', 'install'], cwd=directory, check=True)
            subprocess.run(['sudo', './svc.sh', 'start'], cwd=directory, check=True)
            print('Runner installed and started as a system service.')
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f'Runner configured; install the service manually:\n'
                  f'  cd {directory} && sudo ./svc.sh install && sudo ./svc.sh start')
    else:
        print(f'Runner configured; start it manually with: cd {directory} && ./run.sh')
    print(f'Labels: {args.labels} — the vault workflow must use them in runs-on.')
    return 0


def remove_runner(args):
    directory = _runner_dir(args.dir)
    if not (directory / '.runner').exists():
        print(f'No configured runner at {directory}.', file=sys.stderr)
        return 1
    if args.repo:
        repo = args.repo
    else:
        source = _source_repo(args)
        if source is None:
            return 1
        repo = source[0]
    if args.service and (directory / 'svc.sh').exists():
        try:
            subprocess.run(['sudo', './svc.sh', 'stop'], cwd=directory, check=False)
            subprocess.run(['sudo', './svc.sh', 'uninstall'], cwd=directory, check=False)
        except FileNotFoundError:
            print('sudo unavailable; uninstall the service manually first.')
    token = json.loads(engine.gh('api', '-X', 'POST',
                                 f'repos/{repo}/actions/runners/remove-token'))['token']
    subprocess.run(['./config.sh', 'remove', '--token', token], cwd=directory, check=True)
    print('Runner removed from GitHub; delete the directory if you like.')
    return 0


def runner_status(args):
    directory = _runner_dir(args.dir)
    print(f'Runner dir  {directory}')
    if not directory.is_dir():
        print('             not created; run `sc runner install`')
        return 1
    configured = (directory / '.runner').exists()
    print(f'             configured: {"yes" if configured else "no (run `sc runner install`)"}')
    result = subprocess.run(['systemctl', 'list-units', 'actions.runner.*',
                             '--no-pager', '--plain'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    lines = [line for line in result.stdout.decode().splitlines() if 'actions.runner' in line]
    print('\n'.join(f'             {line}' for line in lines)
          if lines else '             no active actions.runner service found')
    return 0 if configured else 1
