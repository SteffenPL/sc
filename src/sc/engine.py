"""Symmetric two-way Git sync between mapped repositories.

Python 3.11+, Git and gh. Object-level Git operations only: incoming files
are never checked out or executed. Merge ladder per file: trivial resolution,
3-way text merge, optional union merge, then the map's conflict strategy
(freeze & swap PRs by default).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from sc.config import pattern_regex

BOT = {'GIT_AUTHOR_NAME': 'Shared Context', 'GIT_COMMITTER_NAME': 'Shared Context',
       'GIT_AUTHOR_EMAIL': 'shared-context@users.noreply.github.com',
       'GIT_COMMITTER_EMAIL': 'shared-context@users.noreply.github.com',
       'GIT_TERMINAL_PROMPT': '0'}

SWAP_BODY = ('Both repositories changed the same file(s) differently, so sync froze '
             'them: each repository keeps its own version on main. Merging this PR '
             'adopts the other side\'s version; the next sync then converges both '
             'repositories and closes the counterpart PR. Merge at most one of the '
             'two PRs; hand-edit instead if you want a combined result.')

WINNER_BODY = ('Sync accepted the other side\'s file state on main. This PR preserves '
               'this repository\'s previous version of conflicting files only. Review '
               'before merging; merging sends the chosen resolution back on the next sync.')


def command(*args, data=None, cwd=None):
    return subprocess.run(args, input=data, cwd=cwd, env={**os.environ, **BOT},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def gh(*args):
    return command('gh', *args).decode().strip()


def ensure_pr(repo, branch, base, title, body):
    # Include closed PRs: a human rejection must not reopen the same alternative.
    existing = json.loads(gh('pr', 'list', '--repo', repo, '--head', branch,
                             '--state', 'all', '--json', 'number'))
    if not existing:
        gh('pr', 'create', '--repo', repo, '--base', base, '--head', branch,
           '--title', title, '--body', body)


def close_stale_prs(repo, map_name, live_branches):
    """Close swap review PRs whose conflict no longer matches this run."""
    prefix = f'sync-review/{map_name}/'
    prs = json.loads(gh('pr', 'list', '--repo', repo, '--state', 'open',
                        '--limit', '1000', '--json', 'number,headRefName'))
    for pr in prs:
        head = str(pr.get('headRefName', ''))
        if head.startswith(prefix) and head not in live_branches:
            gh('pr', 'close', str(pr['number']), '--repo', repo,
               '--comment', 'The conflicting file(s) were changed or resolved; '
                            'closing as superseded.')


def repo_url(repo):
    # Absolute paths permit the same sync engine to run against local test repos.
    return repo if Path(repo).is_absolute() else f'https://github.com/{repo}.git'


def is_text(blob):
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return all(char.isprintable() or char in '\n\r\t' for char in text)


def _strip(path, prefix):
    if not prefix:
        return path
    if path.startswith(prefix + '/'):
        return path[len(prefix) + 1:]
    return None


def sync_one(spec, work, initialize=False, dry_run=False):
    """Sync one resolved map. Returns a dict with the report and counters."""
    name = spec['name']
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('Map names must contain only letters, digits, _ or -')
    source, target = spec['from'], spec['to']
    merge_mode, conflict_mode = spec['merge'], spec['conflict']
    regexes = [pattern_regex(p) for p in spec['patterns']]

    def git(*args, data=None):
        return command('git', '-C', str(work), *args, data=data)

    def text(*args, data=None):
        return git(*args, data=data).decode().strip()

    def files(ref):
        result = {}
        for entry in git('ls-tree', '-rz', ref).split(b'\0'):
            if entry:
                meta, path = entry.split(b'\t', 1)
                mode, kind, oid = meta.decode().split()
                if kind != 'blob':
                    raise ValueError('Submodules are not supported')
                result[path.decode()] = (mode, oid)
        return result

    def logical(entries, prefix):
        out = {}
        for path, value in entries.items():
            stripped = _strip(path, prefix)
            if stripped is not None:
                out[stripped] = value
        return out

    def tree(entries):
        git('read-tree', '--empty')
        data = b''.join(f'{mode} {oid}\t{path}'.encode() + b'\0'
                        for path, (mode, oid) in sorted(entries.items()))
        git('update-index', '-z', '--index-info', data=data)
        return text('write-tree')

    def commit(entries, parent, message):
        oid = tree(entries)
        if parent and oid == text('rev-parse', f'{parent}^{{tree}}'):
            return parent
        args = ['commit-tree', oid, '-m', message]
        if parent:
            args += ['-p', parent]
        return text(*args)

    def mat(side, path):
        return f'{side["prefix"]}/{path}' if side['prefix'] else path

    def merge_blobs(blobs, mode, union):
        paths = [work / f'merge-{i}' for i in range(3)]
        for target, blob in zip(paths, blobs):
            target.write_bytes(blob)
        args = ['git', 'merge-file', '-p'] + (['--union'] if union else []) + list(map(str, paths))
        result = subprocess.run(args, capture_output=True)
        if result.returncode < 0 or result.returncode > 127:
            raise RuntimeError('git merge-file failed: ' + result.stderr.decode())
        if result.returncode == 0 or (union and b'<<<<<<<' not in result.stdout):
            return (mode, text('hash-object', '-w', '--stdin', data=result.stdout))
        return None

    git('init', '-q')
    for remote, side in (('source', source), ('target', target)):
        git('check-ref-format', f'refs/heads/{side["branch"]}')
        git('remote', 'add', remote, repo_url(side['repo']))
        git('fetch', '-q', '--no-tags', remote,
            f'+refs/heads/{side["branch"]}:refs/remotes/{remote}/tip')
    # Baselines live on the source side; review branches are re-ensured per mode below.
    if git('ls-remote', '--heads', 'source', f'sync-state/{name}').strip():
        git('fetch', '-q', '--no-tags', 'source',
            f'+refs/heads/sync-state/{name}:refs/remotes/state/{name}')
    if conflict_mode == 'to-wins':
        # Winner conflicts are already resolved on mains; retry any pending review PRs.
        git('fetch', '-q', '--no-tags', 'source',
            f'+refs/heads/sync-review/{name}/*:refs/remotes/review/{name}/*')
        for ref in text('for-each-ref', '--format=%(refname)',
                        f'refs/remotes/review/{name}/').splitlines():
            ensure_pr(source['repo'], ref.replace('refs/remotes/review/', 'sync-review/', 1),
                      source['branch'], f'Sync conflict: {name} — source alternative', WINNER_BODY)

    vhead, ehead = text('rev-parse', 'refs/remotes/source/tip'), text('rev-parse', 'refs/remotes/target/tip')
    state = text('for-each-ref', '--format=%(objectname)', f'refs/remotes/state/{name}') or None
    vfiles = logical(files(vhead), source['prefix'])
    efiles = logical(files(ehead), target['prefix'])
    base = files(state) if state else {}
    if not state and not initialize:
        raise ValueError('Missing baseline; restore sync-state or use --initialize for a new pair')

    candidates = set(vfiles)
    if spec['bi_directional']:
        candidates |= set(efiles)
    granted = {path for path in candidates if any(rx.fullmatch(path) for rx in regexes)}

    merged, frozen, conflicts, auto_merged = {}, [], [], 0
    for path in sorted(granted):
        b, v, e = base.get(path), vfiles.get(path), efiles.get(path)
        chosen = None
        if not spec['bi_directional']:
            chosen = v                                  # one-way mirror: source is authoritative
        elif v == e or e == b:
            chosen = v
        elif v == b:
            chosen = e
        else:
            # Only regular text files with identical modes can be auto-merged.
            if b and v and e and b[0] == v[0] == e[0] and v[0] in ('100644', '100755'):
                blobs = [git('cat-file', 'blob', item[1]) for item in (v, b, e)]
                if all(is_text(blob) for blob in blobs):
                    chosen = merge_blobs(blobs, v[0], union=False)
                    if chosen is None and merge_mode == 'union':
                        chosen = merge_blobs(blobs, v[0], union=True)
                    if chosen is not None:
                        auto_merged += 1
            if chosen is None:
                conflicts.append(path)
                if conflict_mode == 'to-wins':
                    chosen = e
                else:
                    frozen.append(path)
        if chosen is not None:
            merged[path] = chosen

    # Files outside a side's folder prefix always stay untouched on that side.
    source_tree = {p: value for p, value in files(vhead).items()
                   if _strip(p, source['prefix']) not in granted}
    source_tree.update({mat(source, p): value for p, value in merged.items()})
    # Unknown target files stay external; revoked previously shared files are removed.
    target_tree = {}
    for p, value in files(ehead).items():
        stripped = _strip(p, target['prefix'])
        if stripped is None or (stripped not in granted and stripped not in base):
            target_tree[p] = value
    target_tree.update({mat(target, p): value for p, value in merged.items()})
    for path in frozen:
        if path in vfiles:
            source_tree[mat(source, path)] = vfiles[path]
        if path in efiles:
            target_tree[mat(target, path)] = efiles[path]

    vnew = commit(source_tree, vhead, f'Sync shared context: {name}')
    enew = commit(target_tree, ehead, 'Sync shared context')
    unmanaged = len({p for p in efiles if p not in granted and p not in base})
    pushes_source = [f'{vnew}:refs/heads/{source["branch"]}']
    pushes_target = [f'{enew}:refs/heads/{target["branch"]}']
    review = None

    if conflicts and conflict_mode in ('swap', 'to-wins'):
        fingerprint = '\0'.join(sorted(
            f'{path}\0{base.get(path) or ("-", "-")}\0{vfiles.get(path) or ("-", "-")}\0'
            f'{efiles.get(path) or ("-", "-")}' for path in conflicts))
        review = f'sync-review/{name}/' + hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
        if conflict_mode == 'swap':
            # Offer each side the other side's version of the frozen files.
            source_alt, target_alt = dict(source_tree), dict(target_tree)
            for path in frozen:
                if path in efiles:
                    source_alt[mat(source, path)] = efiles[path]
                else:
                    source_alt.pop(mat(source, path), None)
                if path in vfiles:
                    target_alt[mat(target, path)] = vfiles[path]
                else:
                    target_alt.pop(mat(target, path), None)
            pushes_source.append(f'{commit(source_alt, vnew, f"Adopt target side: {name}")}'
                                 f':refs/heads/{review}')
            pushes_target.append(f'{commit(target_alt, enew, f"Adopt source side: {name}")}'
                                 f':refs/heads/{review}')
        elif conflict_mode == 'to-wins':
            # The winner is already on both mains; preserve the source alternative.
            alternative = dict(source_tree)
            for path in conflicts:
                if path in vfiles:
                    alternative[mat(source, path)] = vfiles[path]
                else:
                    alternative.pop(mat(source, path), None)
            pushes_source.append(f'{commit(alternative, vnew, f"Preserve source alternative: {name}")}'
                                 f':refs/heads/{review}')

    if dry_run:
        detail = _conflict_detail(conflicts, conflict_mode, dry=True)
        report = (f'{name} (dry run, nothing pushed): from '
                  f'{"updated" if vnew != vhead else "unchanged"}, to '
                  f'{"updated" if enew != ehead else "unchanged"}; '
                  f'{len(granted)} granted path(s); {auto_merged} auto-merged; {detail}; '
                  f'{unmanaged} unmanaged file(s) ignored.')
        return {'report': report, 'conflicts': len(conflicts), 'auto_merged': auto_merged,
                'granted': len(granted), 'dry_run': True}

    git('push', '--atomic', 'source', *pushes_source)
    git('push', '--atomic', 'target', *pushes_target)
    # Record the agreed snapshot right after both mains are updated, so a later
    # PR API hiccup cannot lose the baseline.
    new_state = commit(dict(merged, **{p: base[p] for p in frozen if p in base}), state,
                       f'Sync baseline: {name}')
    git('push', 'source', f'{new_state}:refs/heads/sync-state/{name}')
    if review:
        if conflict_mode == 'swap':
            title = f'Sync conflict ({name}): adopt the other side\'s version of {len(conflicts)} file(s)'
            ensure_pr(source['repo'], review, source['branch'], title, SWAP_BODY)
            ensure_pr(target['repo'], review, target['branch'], title, SWAP_BODY)
        elif conflict_mode == 'to-wins':
            ensure_pr(source['repo'], review, source['branch'],
                      f'Sync conflict: {name} — source alternative', WINNER_BODY)
    if conflict_mode == 'swap':
        close_stale_prs(source['repo'], name, {review} if review else set())
        close_stale_prs(target['repo'], name, {review} if review else set())
    detail = _conflict_detail(conflicts, conflict_mode, dry=False)
    report = (f'{name}: from {"updated" if vnew != vhead else "unchanged"}, '
              f'to {"updated" if enew != ehead else "unchanged"}; '
              f'{auto_merged} auto-merged; {detail}; {unmanaged} unmanaged file(s) ignored.')
    return {'report': report, 'conflicts': len(conflicts), 'auto_merged': auto_merged,
            'granted': len(granted), 'dry_run': False}


def _conflict_detail(conflicts, conflict_mode, dry):
    if not conflicts:
        return 'no conflicts'
    if conflict_mode == 'swap':
        return f'{len(conflicts)} conflict(s) frozen (review PRs on both sides)'
    if conflict_mode == 'to-wins':
        return f'{len(conflicts)} conflict(s) resolved to-wins (source alternative in PR)'
    return f'{len(conflicts)} conflict(s) frozen (no PRs)'
