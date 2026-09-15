"""Allowlisted two-way Git sync between a vault and collaborator repos.

Python 3.11+, Git and gh. Object-level Git operations only: incoming files
are never checked out or executed.
"""
import json
import os
from pathlib import Path
import re
import subprocess

from sc.config import allowed_paths

BOT = {'GIT_AUTHOR_NAME': 'Shared Context', 'GIT_COMMITTER_NAME': 'Shared Context',
       'GIT_AUTHOR_EMAIL': 'shared-context@users.noreply.github.com',
       'GIT_COMMITTER_EMAIL': 'shared-context@users.noreply.github.com',
       'GIT_TERMINAL_PROMPT': '0'}


def command(*args, data=None, cwd=None):
    return subprocess.run(args, input=data, cwd=cwd, env={**os.environ, **BOT},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def gh(*args):
    return command('gh', *args).decode().strip()


def ensure_pr(repo, branch, base):
    # Include closed PRs: a human rejection must not reopen the same alternative.
    existing = json.loads(gh('pr', 'list', '--repo', repo, '--head', branch,
                             '--state', 'all', '--json', 'number'))
    if not existing:
        gh('pr', 'create', '--repo', repo, '--base', base, '--head', branch,
           '--title', f'Sync conflict: {branch.split("/")[1]} — vault alternative',
           '--body', 'Sync accepted the collaborator file state on main. This PR preserves '
           'the previous vault version of conflicting files only. Review before merging; '
           'merging sends the chosen resolution back to collaborators on the next sync.')


def repo_url(repo):
    # Absolute paths permit the same sync engine to run against local test repos.
    return repo if Path(repo).is_absolute() else f'https://github.com/{repo}.git'


def is_text(blob):
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return all(char.isprintable() or char in '\n\r\t' for char in text)


def sync_one(settings, name, work, initialize=False):
    """Sync one collaborator against the vault. Returns (report, conflicts)."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('Collaborator names must contain only letters, digits, _ or -')
    allowed = allowed_paths(settings, name)
    vault = settings['vault']
    external = settings['collaborators'][name]
    vb, eb = vault.get('branch', 'main'), external.get('branch', 'main')

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

    git('init', '-q')
    for remote, config_entry, branch in (('vault', vault, vb), ('external', external, eb)):
        git('check-ref-format', f'refs/heads/{branch}')
        git('remote', 'add', remote, repo_url(config_entry['repo']))
        git('fetch', '-q', '--no-tags', remote,
            f'+refs/heads/{branch}:refs/remotes/{remote}/main')
    # State and review branches never leave the private vault.
    if git('ls-remote', '--heads', 'vault', f'sync-state/{name}').strip():
        git('fetch', '-q', '--no-tags', 'vault',
            f'+refs/heads/sync-state/{name}:refs/remotes/state/{name}')
    git('fetch', '-q', '--no-tags', 'vault',
        f'+refs/heads/sync-review/{name}/*:refs/remotes/review/{name}/*')
    for ref in text('for-each-ref', '--format=%(refname)', f'refs/remotes/review/{name}/').splitlines():
        ensure_pr(vault['repo'], ref.replace('refs/remotes/review/', 'sync-review/', 1), vb)

    vhead, ehead = text('rev-parse', 'refs/remotes/vault/main'), text('rev-parse', 'refs/remotes/external/main')
    state_ref = f'refs/remotes/state/{name}'
    state = text('for-each-ref', '--format=%(objectname)', state_ref) or None
    vfiles, efiles = files(vhead), files(ehead)
    base = files(state) if state else {}
    if not state and not initialize:
        raise ValueError('Missing baseline; restore sync-state or use --initialize for a new pair')

    merged, conflicts = {}, []
    for path in sorted(allowed):
        b, v, e = base.get(path), vfiles.get(path), efiles.get(path)
        if v == e or e == b:
            chosen = v
        elif v == b:
            chosen = e
        else:
            chosen = None
            # Only regular text files with identical modes can be auto-merged.
            if b and v and e and b[0] == v[0] == e[0] and v[0] in ('100644', '100755'):
                blobs = [git('cat-file', 'blob', item[1]) for item in (v, b, e)]
                if all(is_text(blob) for blob in blobs):
                    paths = [work / f'merge-{i}' for i in range(3)]
                    for target, blob in zip(paths, blobs):
                        target.write_bytes(blob)
                    result = subprocess.run(['git', 'merge-file', '-p', *map(str, paths)],
                                            capture_output=True)
                    if result.returncode == 0:
                        chosen = (v[0], text('hash-object', '-w', '--stdin', data=result.stdout))
                    elif result.returncode < 0 or result.returncode > 127:
                        raise RuntimeError('git merge-file failed: ' + result.stderr.decode())
            if chosen is None:
                conflicts.append(path)
                chosen = e
        if chosen is not None:
            merged[path] = chosen

    new_vault = {p: value for p, value in vfiles.items() if p not in allowed}
    new_vault.update(merged)
    # Unknown external files stay external; revoked previously shared files are removed.
    new_external = {p: value for p, value in efiles.items() if p not in allowed and p not in base}
    new_external.update(merged)
    vnew = commit(new_vault, vhead, f'Sync shared context: {name}')
    enew = commit(new_external, ehead, 'Sync shared context')
    pushes = [f'{vnew}:refs/heads/{vb}']
    review = None
    if conflicts:
        recovery = dict(new_vault)
        for path in conflicts:
            recovery.pop(path, None)
            if path in vfiles:
                recovery[path] = vfiles[path]
        review = f'sync-review/{name}/{vhead[:12]}-{ehead[:12]}'
        alternative = commit(recovery, vnew, f'Preserve vault alternative: {name}')
        pushes.append(f'{alternative}:refs/heads/{review}')
    # Preserve the vault alternative atomically before accepting any conflicting data.
    git('push', '--atomic', 'vault', *pushes)
    if review:
        ensure_pr(vault['repo'], review, vb)
    git('push', 'external', f'{enew}:refs/heads/{eb}')
    new_state = commit(merged, state, f'Sync baseline: {name}')
    git('push', 'vault', f'{new_state}:refs/heads/sync-state/{name}')
    return (f'{name}: vault {"updated" if vnew != vhead else "unchanged"}, '
            f'collaborator {"updated" if enew != ehead else "unchanged"}; '
            f'{len(conflicts)} conflict(s) preserved for review; '
            f'{len(set(efiles) - allowed - set(base))} unmanaged external file(s) ignored.',
            len(conflicts))
