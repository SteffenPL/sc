"""TOML configuration: resolution, loading and validation."""
import os
from pathlib import Path, PurePosixPath
import re
import tomllib

NAME = re.compile(r'[a-zA-Z0-9_-]+')


def resolve(config=None):
    """Return the config path to use: explicit, $SC_CONFIG, or a default.

    An explicit path is returned even when missing, so callers can report it.
    """
    if config is not None:
        return Path(config)
    if os.environ.get('SC_CONFIG'):
        return Path(os.environ['SC_CONFIG'])
    home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'sc'
    for candidate in (Path('sc.toml'), Path('sharing.toml'), home / 'config.toml'):
        if candidate.is_file():
            return candidate
    return None


def load(path):
    with Path(path).open('rb') as source:
        return tomllib.load(source)


def allowed_paths(settings, name):
    paths = set(settings['collaborators'][name].get('include', []))
    for share in settings.get('shares', []):
        if name in share['to']:
            paths.update(share['paths'])
    for path in paths:
        parts = PurePosixPath(path).parts
        if (not parts or path.startswith('/') or '..' in parts or
                any(p.lower() == '.git' for p in parts) or
                any(c in path for c in '*?[]\\\n\r\0') or
                str(PurePosixPath(path)) != path):
            raise ValueError(f'Only exact, safe vault-relative file paths are allowed: {path!r}')
    return paths


def validate(settings):
    """Return a list of configuration errors; an empty list means valid."""
    errors = []
    if not isinstance(settings, dict):
        return ['configuration must be a TOML table']
    vault = settings.get('vault')
    if not isinstance(vault, dict) or not vault.get('repo'):
        errors.append('[vault] must define repo = "owner/name"')
    collaborators = settings.get('collaborators')
    if not isinstance(collaborators, dict) or not collaborators:
        errors.append('at least one [collaborators.<name>] section is required')
        collaborators = {}
    for name, entry in collaborators.items():
        if not NAME.fullmatch(name):
            errors.append(f'collaborator name {name!r} must contain only letters, digits, _ or -')
        if not isinstance(entry, dict) or not entry.get('repo'):
            errors.append(f'[collaborators.{name}] must define repo = "owner/name"')
            continue
        try:
            allowed_paths(settings, name)
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f'collaborators.{name}: {error}')
    shares = settings.get('shares', [])
    if not isinstance(shares, list):
        errors.append('[[shares]] must be a list of tables')
        shares = []
    for index, share in enumerate(shares):
        if not isinstance(share, dict) or not share.get('paths') or not share.get('to'):
            errors.append(f'[[shares]] entry {index} must define paths and to')
            continue
        for target in share['to']:
            if target not in collaborators:
                errors.append(f'[[shares]] entry {index} targets unknown collaborator {target!r}')
    sync = settings.get('sync', {})
    if not isinstance(sync, dict):
        errors.append('[sync] must be a table')
    else:
        interval = sync.get('interval', 900)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 60:
            errors.append('[sync] interval must be an integer number of seconds (>= 60)')
    return errors
