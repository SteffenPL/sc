"""Surgical edits to the sc sharing TOML.

The configuration file is hand-maintained and carries comments plus a
deliberate layout. Instead of regenerating the whole document, every
operation rewrites only the affected [[maps]] or [[permissions]] block and
leaves every other byte of the file untouched.

Operations are applied one at a time, each against the text produced by the
previous operation, so block references stay meaningful inside a batch:
maps are identified by name, permission entries by their index among the
[[permissions]] blocks of the current intermediate state.
"""
import json
import re
import tomllib

from sc import config

HEADER = re.compile(r'^\s*\[\[(maps|permissions)\]\]')
TABLE = re.compile(r'^\s*\[')
COMMENT = re.compile(r'^\s*#')
MAP_KEYS = ('name', 'from', 'to')
MAP_EXTRAS = ('merge', 'conflict', 'bi_directional', 'from_branch', 'to_branch')
MAP_DEFAULTS = {'merge': 'text', 'conflict': 'swap', 'bi_directional': True,
                'from_branch': 'main', 'to_branch': 'main'}
MULTILINE = ('"""', "'''")


class BlockError(ValueError):
    """Raised when an operation cannot be applied to the current text."""


def parse_blocks(text):
    """Return the top-level [[maps]]/[[permissions]] blocks of the file.

    Each block dict carries its zero-based line span (attached leading
    comment lines included; blank lines up to the next block included),
    the parsed entry table and its attached comment lines. Comment lines
    directly above a header (no blank line between) belong to that block.
    """
    lines = text.splitlines(True)
    blocks, current = [], None
    for index, line in enumerate(lines):
        if current is not None and TABLE.match(line):
            end = index
            if HEADER.match(line):
                while end > 0 and COMMENT.match(lines[end - 1]):
                    end -= 1
            current['end'] = end
            current = None
        header = HEADER.match(line)
        if header:
            start = index
            while start > 0 and COMMENT.match(lines[start - 1]):
                start -= 1
            current = {'kind': header.group(1), 'start': start, 'header': index,
                       'end': len(lines)}
            blocks.append(current)
    if current is not None:
        current['end'] = len(lines)
    for block in blocks:
        block['text'] = ''.join(lines[block['start']:block['end']])
        entries = tomllib.loads(block['text']).get(block['kind'], [])
        block['entry'] = dict(entries[0]) if entries else {}
        block['comments'] = [line for line in lines[block['start']:block['header']]]
    return blocks


def _dump(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise BlockError(f'unsupported TOML value: {value!r}')


def _render_map(entry, comments):
    for key in MAP_KEYS:
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise BlockError(f'map entry needs a {key!r} string')
    lines = list(comments) + ['[[maps]]\n']
    width = max(len(key) for key in MAP_KEYS)
    for key in MAP_KEYS:
        lines.append(f'{key:<{width}} = {_dump(entry[key])}\n')
    for key in MAP_EXTRAS:
        value = entry.get(key, MAP_DEFAULTS[key])
        if value != MAP_DEFAULTS[key]:
            lines.append(f'{key} = {_dump(value)}\n')
    return lines


def _render_permission(entry, comments):
    paths, maps = entry.get('paths'), entry.get('maps')
    if not isinstance(paths, list) or not paths:
        raise BlockError('permission entry needs a non-empty paths list')
    if not isinstance(maps, list) or not maps:
        raise BlockError('permission entry needs a non-empty maps list')
    lines = list(comments) + ['[[permissions]]\n']
    if entry.get('repo') is not None:
        lines.append(f'repo = {_dump(entry["repo"])}\n')
    if len(paths) == 1:
        lines.append(f'paths = [{_dump(paths[0])}]\n')
    else:
        lines.append('paths = [\n')
        lines.extend(f'    {_dump(path)},\n' for path in paths)
        lines.append(']\n')
    lines.append('maps = [' + ', '.join(_dump(name) for name in maps) + ']\n')
    return lines


def _render(kind, entry, comments):
    return _render_map(entry, comments) if kind == 'maps' else _render_permission(entry, comments)


def _join(lines):
    text = ''.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    if text and not text.endswith('\n'):
        text += '\n'
    return text


def _guard(text):
    if any(marker in text for marker in MULTILINE):
        raise BlockError('the UI editor cannot edit configs with multiline strings')


def _replace(lines, block, replacement):
    """Swap a block's line span for replacement lines, adding the single
    separating blank line unless the block is the last thing in the file."""
    span = replacement if block['end'] >= len(lines) else replacement + ['\n']
    lines[block['start']:block['end']] = span
    return lines


def _insert_at(lines, position, replacement):
    """Insert a new rendered block at a line position."""
    before = ''.join(lines[:position])
    separator = []
    if before and not before.endswith('\n'):
        separator = ['\n', '\n']
    elif before and not before.endswith('\n\n'):
        separator = ['\n']
    lines[position:position] = separator + replacement + (['\n'] if position < len(lines) else [])
    return lines


def _remove(lines, block):
    lines[block['start']:block['end']] = []
    if block['start'] < len(lines) and block['start'] > 0:
        before = ''.join(lines[:block['start']])
        if before and not before.endswith('\n\n') and lines[block['start']].strip():
            lines.insert(block['start'], '\n')
    return lines


def _of_kind(blocks, kind):
    return [block for block in blocks if block['kind'] == kind]


def _map_block(blocks, name):
    for block in _of_kind(blocks, 'maps'):
        if block['entry'].get('name') == name:
            return block
    return None


def _perm_block(blocks, index):
    permissions = _of_kind(blocks, 'permissions')
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(permissions):
        raise BlockError(f'permission entry {index!r} not found; reload the page')
    return permissions[index]


def _insert_position(blocks, kind):
    """Line position for a new block of kind: after the last block of that
    kind, else after the last map block, else at the end of the file."""
    own = _of_kind(blocks, kind)
    if own:
        return own[-1]['end']
    if kind == 'permissions':
        maps = _of_kind(blocks, 'maps')
        if maps:
            return maps[-1]['end']
    return None


def _entry_repo_paths(entry):
    """Return (repo, {absolute path: stored path}) when every path of the
    entry addresses the same repository, else (None, {}).

    Absolute paths are in the repository's own namespace (folder prefixes
    of a repo context are already part of the stored relative path here).
    """
    paths = entry.get('paths')
    if not isinstance(paths, list) or not paths:
        return None, {}
    if entry.get('repo') is not None:
        repo, prefix = config.parse_location(entry['repo'])
        if prefix:
            return repo, {f'{prefix}/{path}': path for path in paths}
        return repo, {path: path for path in paths}
    mapping, repo = {}, None
    for path in paths:
        parts = path.split('/')
        if len(parts) < 3:
            return None, {}
        current = '/'.join(parts[:2])
        if repo is None:
            repo = current
        elif repo != current:
            return None, {}
        mapping['/'.join(parts[2:])] = path
    return repo, mapping


def _validate_map_entry(entry):
    name = entry.get('name')
    if not isinstance(name, str) or not config.NAME.fullmatch(name):
        raise BlockError(f'invalid map name {name!r}')
    for side in ('from', 'to'):
        config.parse_location(entry.get(side))
    for key, allowed in (('merge', config.MERGE_MODES), ('conflict', config.CONFLICT_MODES)):
        if entry.get(key, MAP_DEFAULTS[key]) not in allowed:
            raise BlockError(f'map {name!r}: {key} must be one of {allowed}')
    for key in ('from_branch', 'to_branch'):
        if entry.get(key, MAP_DEFAULTS[key]) in (None, ''):
            raise BlockError(f'map {name!r}: {key} must be a branch name')


def _validate_perm_entry(entry):
    repo = entry.get('repo')
    if repo is not None:
        config.parse_location(repo)
    paths = entry.get('paths')
    if not isinstance(paths, list) or not paths:
        raise BlockError('permission needs a non-empty paths list')
    for path in paths:
        if not isinstance(path, str) or not path or path.startswith('/'):
            raise BlockError(f'unsafe path pattern: {path!r}')
        if repo is None and len(path.split('/')) < 3:
            raise BlockError(f'path {path!r} must be Owner/Repo/path or use a repo context')
        config.check_pattern(path.split('/', 2)[2] if repo is None else path)
    maps = entry.get('maps')
    if not isinstance(maps, list) or not maps:
        raise BlockError('permission needs a non-empty maps list')


def op_map_upsert(text, entry):
    if not isinstance(entry, dict):
        raise BlockError('map entry must be a table')
    _validate_map_entry(entry)
    _guard(text)
    blocks = parse_blocks(text)
    lines = text.splitlines(True)
    existing = _map_block(blocks, entry['name'])
    if existing is not None:
        return _join(_replace(lines, existing, _render('maps', entry, existing['comments'])))
    position = _insert_position(blocks, 'maps')
    if position is None:
        return _join(_insert_at(lines, len(lines), _render('maps', entry, [])))
    return _join(_insert_at(lines, position, _render('maps', entry, [])))


def op_map_remove(text, name):
    _guard(text)
    blocks = parse_blocks(text)
    existing = _map_block(blocks, name)
    if existing is None:
        return text
    return _join(_remove(text.splitlines(True), existing))


def op_perm_upsert(text, index, entry):
    if not isinstance(entry, dict):
        raise BlockError('permission entry must be a table')
    _validate_perm_entry(entry)
    _guard(text)
    blocks = parse_blocks(text)
    lines = text.splitlines(True)
    if index is not None:
        block = _perm_block(blocks, index)
        return _join(_replace(lines, block, _render('permissions', entry, block['comments'])))
    position = _insert_position(blocks, 'permissions')
    if position is None:
        position = len(lines)
    return _join(_insert_at(lines, position, _render('permissions', entry, [])))


def op_perm_remove(text, index):
    _guard(text)
    blocks = parse_blocks(text)
    return _join(_remove(text.splitlines(True), _perm_block(blocks, index)))


def _grant_target(entry, repo):
    """{absolute path: stored path} when the entry addresses exactly this
    repository (same repo, with or without an explicit repo context)."""
    entry_repo, mapping = _entry_repo_paths(entry)
    if entry_repo is None or config.parse_location(repo)[0] != entry_repo:
        return None
    return mapping


def op_perm_grant(text, repo, path, maps):
    if not isinstance(maps, list) or not maps:
        raise BlockError('grant needs a non-empty maps list')
    location = config.parse_location(repo)
    if location[1]:
        raise BlockError(f'grant repo {repo!r} must not carry a folder prefix')
    config.check_pattern(path)
    _guard(text)
    blocks = parse_blocks(text)
    lines = text.splitlines(True)
    for block in _of_kind(blocks, 'permissions'):
        mapping = _grant_target(block['entry'], repo)
        if mapping is None or path not in mapping:
            continue
        entry = block['entry']
        if set(maps) <= set(entry['maps']):
            return text
        merged = dict(entry, maps=sorted(set(entry['maps']) | set(maps)))
        return _join(_replace(lines, block, _render('permissions', merged, block['comments'])))
    for block in _of_kind(blocks, 'permissions'):
        entry = block['entry']
        if _grant_target(entry, repo) is None or set(maps) != set(entry['maps']):
            continue
        merged = dict(entry, paths=sorted(set(entry['paths']) | {stored_for(entry, repo, path)}))
        return _join(_replace(lines, block, _render('permissions', merged, block['comments'])))
    entry = {'repo': repo, 'paths': [path], 'maps': sorted(set(maps))}
    position = _insert_position(blocks, 'permissions')
    if position is None:
        position = len(lines)
    return _join(_insert_at(lines, position, _render('permissions', entry, [])))


def stored_for(entry, repo, path):
    """Path as stored inside a repo-context entry: relative to its prefix."""
    if entry.get('repo') is None:
        return f'{repo}/{path}'
    prefix = config.parse_location(entry['repo'])[1]
    return path[len(prefix) + 1:] if prefix else path


def op_perm_revoke(text, repo, path, maps):
    if not isinstance(maps, list) or not maps:
        raise BlockError('revoke needs a non-empty maps list')
    location = config.parse_location(repo)
    if location[1]:
        raise BlockError(f'revoke repo {repo!r} must not carry a folder prefix')
    config.check_pattern(path)
    _guard(text)
    blocks = parse_blocks(text)
    lines = text.splitlines(True)
    edits = []
    for block in _of_kind(blocks, 'permissions'):
        entry = block['entry']
        mapping = _grant_target(entry, repo)
        if mapping is None or path not in mapping:
            continue
        stored = mapping[path]
        current = set(entry['maps'])
        if not current & set(maps):
            continue
        keep = sorted(current - set(maps))
        rest = [item for item in entry['paths'] if item != stored]
        if keep:
            replacement = []
            if rest:
                replacement = _render('permissions',
                                      dict(entry, paths=rest, maps=sorted(current)),
                                      block['comments'])
            replacement += _render('permissions',
                                   {'repo': entry.get('repo'), 'paths': [stored], 'maps': keep},
                                   [])
            edits.append((block['start'], block['end'], replacement))
        elif rest:
            edits.append((block['start'], block['end'],
                          _render('permissions', dict(entry, paths=rest), block['comments'])))
        else:
            edits.append((block['start'], block['end'], []))
    if not edits:
        return text
    for start, end, replacement in sorted(edits, key=lambda edit: -edit[0]):
        span = replacement if end >= len(lines) else replacement + ['\n']
        lines[start:end] = span
    return _join(lines)


OPS = {'map_upsert': lambda text, op: op_map_upsert(text, op.get('entry')),
       'map_remove': lambda text, op: op_map_remove(text, op.get('name')),
       'perm_upsert': lambda text, op: op_perm_upsert(text, op.get('index'), op.get('entry')),
       'perm_remove': lambda text, op: op_perm_remove(text, op.get('index')),
       'perm_grant': lambda text, op: op_perm_grant(text, op.get('repo'), op.get('path'), op.get('maps')),
       'perm_revoke': lambda text, op: op_perm_revoke(text, op.get('repo'), op.get('path'), op.get('maps'))}


def apply_op(text, op):
    if not isinstance(op, dict) or op.get('op') not in OPS:
        raise BlockError(f'unknown operation: {op!r}')
    try:
        return OPS[op['op']](text, op)
    except BlockError:
        raise
    except ValueError as error:
        raise BlockError(str(error)) from None


def apply_ops(text, ops):
    """Apply a list of operations in order, each against the previous result."""
    if not isinstance(ops, list):
        raise BlockError('ops must be a list')
    for op in ops:
        text = apply_op(text, op)
    return text


def build_model(text):
    """A JSON-ready view of the configuration for the UI."""
    maps, permissions = [], []
    for block in parse_blocks(text):
        if block['kind'] == 'maps':
            entry = block['entry']
            if not entry.get('name'):
                continue
            maps.append({'name': entry.get('name'), 'from': entry.get('from'),
                         'to': entry.get('to'),
                         'merge': entry.get('merge', MAP_DEFAULTS['merge']),
                         'conflict': entry.get('conflict', MAP_DEFAULTS['conflict']),
                         'bi_directional': entry.get('bi_directional', True),
                         'from_branch': entry.get('from_branch', 'main'),
                         'to_branch': entry.get('to_branch', 'main')})
        else:
            entry = block['entry']
            permissions.append({'index': len(permissions),
                                'repo': entry.get('repo'),
                                'paths': list(entry.get('paths', [])),
                                'maps': list(entry.get('maps', [])),
                                'comment': ''.join(block['comments']).strip() or None})
    return {'maps': maps, 'permissions': permissions}
