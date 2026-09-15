"""TOML configuration: maps, permissions, resolution and validation.

A map pairs two repositories (each with an optional folder prefix that is
stripped from that side's paths and added to the other side's outgoing
files). A permission grants path patterns to travel through named maps.
Paths are compared in the stripped, "logical" namespace.
"""
import os
from pathlib import Path, PurePosixPath
import re
import tomllib

NAME = re.compile(r'[a-zA-Z0-9_-]+')
MERGE_MODES = ('text', 'union')
CONFLICT_MODES = ('swap', 'to-wins', 'freeze')


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


def parse_location(value):
    """Parse 'Owner/Repo[/folder...]' into (repo, prefix).

    Absolute paths address local repositories directly and carry no prefix;
    a 'https://github.com/' prefix is accepted and stripped.
    """
    if not isinstance(value, str) or not value:
        raise ValueError('map endpoints must be non-empty strings')
    location = value
    if location.startswith('https://github.com/'):
        location = location[len('https://github.com/'):]
    if location.startswith('/'):
        return location, ''
    parts = location.split('/')
    if len(parts) < 2 or not all(parts[:2]):
        raise ValueError(f'expected Owner/Repo[/folder]: {value!r}')
    prefix = '/'.join(parts[2:])
    _check_prefix(prefix, value)
    return '/'.join(parts[:2]), prefix


def _check_prefix(prefix, source):
    if not prefix:
        return
    parts = PurePosixPath(prefix).parts
    if (not parts or prefix.startswith('/') or '..' in parts or
            any(p.lower() == '.git' for p in parts) or
            any(c in prefix for c in '*?[]{}\n\r\0\\') or
            str(PurePosixPath(prefix)) != prefix):
        raise ValueError(f'unsafe folder prefix in {source!r}: {prefix!r}')


def check_pattern(pattern):
    """Validate a (possibly globbed) repo-relative path pattern."""
    if not isinstance(pattern, str) or not pattern or pattern.startswith('/'):
        raise ValueError(f'unsafe path pattern: {pattern!r}')
    if any(c in pattern for c in '[]\\\n\r\0'):
        raise ValueError(f'unsafe characters in path pattern: {pattern!r}')
    for part in pattern.split('/'):
        if part in ('', '.', '..') or part.lower() == '.git':
            raise ValueError(f'unsafe path pattern: {pattern!r}')


def expand_braces(pattern):
    """Expand {a,b} alternatives; nesting is allowed, unbalanced is an error."""
    start = pattern.find('{')
    if start == -1:
        return [pattern]
    depth, end = 0, -1
    for index in range(start, len(pattern)):
        if pattern[index] == '{':
            depth += 1
        elif pattern[index] == '}':
            depth -= 1
            if depth == 0:
                end = index
                break
    if end == -1:
        raise ValueError(f'unbalanced braces in pattern: {pattern!r}')
    body, alternatives, level, last = pattern[start + 1:end], [], 0, 0
    for index, char in enumerate(body):
        if char == '{':
            level += 1
        elif char == '}':
            level -= 1
        elif char == ',' and level == 0:
            alternatives.append(body[last:index])
            last = index + 1
    alternatives.append(body[last:])
    if not all(alternatives):
        raise ValueError(f'empty brace alternative in pattern: {pattern!r}')
    out = []
    for alternative in alternatives:
        out.extend(expand_braces(pattern[:start] + alternative + pattern[end + 1:]))
    return out


def pattern_regex(pattern):
    """Compile a logical path pattern; '*' one segment, '**' any depth."""
    pieces = []
    parts = pattern.split('/')
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == '**':
            pieces.append('(?:[^/]+/)*[^/]+' if last else '(?:[^/]+/)*')
            continue
        piece = ''
        for char in part:
            if char == '*':
                piece += '[^/]*'
            elif char == '?':
                piece += '[^/]'
            else:
                piece += re.escape(char)
        pieces.append(piece + ('' if last else '/'))
    return re.compile(''.join(pieces) + r'\Z')


def _anchor(pattern, spec, repo, source):
    """Translate a repo-anchored pattern into the map's logical namespace."""
    for side in ('from', 'to'):
        if repo == spec[side]['repo']:
            prefix = spec[side]['prefix']
            if not prefix:
                return pattern
            if pattern.startswith(prefix + '/'):
                return pattern[len(prefix) + 1:]
            raise ValueError(f'permission path {source!r} is not under the {side} '
                             f'folder {prefix!r} of map {spec["name"]!r}')
    raise ValueError(f'permission path {source!r} belongs to neither endpoint '
                     f'of map {spec["name"]!r}')


def resolve_maps(settings):
    """Build resolved map specs (with logical patterns) or raise ValueError."""
    if not isinstance(settings, dict):
        raise ValueError('configuration must be a TOML table')
    maps = settings.get('maps')
    if not isinstance(maps, list) or not maps:
        raise ValueError('at least one [[maps]] entry is required')
    specs, seen = [], set()
    for entry in maps:
        if not isinstance(entry, dict):
            raise ValueError('[[maps]] entries must be tables')
        name = entry.get('name')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f'map name {name!r} must contain only letters, digits, _ or -')
        if name in seen:
            raise ValueError(f'duplicate map name {name!r}')
        seen.add(name)
        from_repo, from_prefix = parse_location(entry.get('from'))
        to_repo, to_prefix = parse_location(entry.get('to'))
        if from_repo == to_repo:
            raise ValueError(f'map {name!r} cannot pair a repository with itself')
        merge = entry.get('merge', 'text')
        conflict = entry.get('conflict', 'swap')
        if merge not in MERGE_MODES:
            raise ValueError(f'map {name!r}: merge must be one of {MERGE_MODES}')
        if conflict not in CONFLICT_MODES:
            raise ValueError(f'map {name!r}: conflict must be one of {CONFLICT_MODES}')
        bi = entry.get('bi_directional', True)
        if not isinstance(bi, bool):
            raise ValueError(f'map {name!r}: bi_directional must be true or false')
        for key, side in (('from_branch', 'from'), ('to_branch', 'to')):
            branch = entry.get(key, 'main')
            if not isinstance(branch, str) or not branch:
                raise ValueError(f'map {name!r}: {key} must be a branch name')
        specs.append({'name': name,
                      'from': {'repo': from_repo, 'branch': entry.get('from_branch', 'main'),
                               'prefix': from_prefix},
                      'to': {'repo': to_repo, 'branch': entry.get('to_branch', 'main'),
                             'prefix': to_prefix},
                      'merge': merge, 'conflict': conflict, 'bi_directional': bi,
                      'patterns': []})
    by_name = {spec['name']: spec for spec in specs}
    permissions = settings.get('permissions', [])
    if not isinstance(permissions, list):
        raise ValueError('[[permissions]] must be a list of tables')
    for entry in permissions:
        if not isinstance(entry, dict):
            raise ValueError('[[permissions]] entries must be tables')
        context = entry.get('repo')
        if context is not None:
            context_repo, context_prefix = parse_location(context)
        raw_paths = entry.get('paths')
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError('[[permissions]] must define a non-empty paths list')
        targets = entry.get('maps')
        if not isinstance(targets, list) or not targets:
            raise ValueError('[[permissions]] must define a non-empty maps list')
        for target in targets:
            if target not in by_name:
                raise ValueError(f'permission references unknown map {target!r}')
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw or raw.startswith('/'):
                raise ValueError(f'unsafe path pattern: {raw!r}')
            if context is not None:
                repo, pattern = context_repo, (context_prefix + '/' + raw if context_prefix else raw)
            else:
                parts = raw.split('/')
                if len(parts) < 3:
                    raise ValueError(f'path {raw!r} must be Owner/Repo/path or '
                                     'use the permission\'s repo for relative paths')
                repo, pattern = '/'.join(parts[:2]), '/'.join(parts[2:])
            check_pattern(pattern)
            for expanded in expand_braces(pattern):
                for target in targets:
                    spec = by_name[target]
                    spec['patterns'].append(_anchor(expanded, spec, repo, raw))
    for spec in specs:
        spec['patterns'] = sorted(set(spec['patterns']))
    return specs


def validate(settings):
    """Return a list of configuration errors; an empty list means valid."""
    errors = []
    try:
        resolve_maps(settings)
    except (ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    sync = settings.get('sync', {}) if isinstance(settings, dict) else {}
    if not isinstance(sync, dict):
        errors.append('[sync] must be a table')
    else:
        interval = sync.get('interval', 900)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 60:
            errors.append('[sync] interval must be an integer number of seconds (>= 60)')
    return errors
