# sc — shared context

Allowlisted **bidirectional sync** between a private Git vault and collaborator
repos. Private by default: only files explicitly granted in a TOML config ever
leave the vault, and only file contents — never private history — are shared.

Both sides commit directly to `main`. One-sided changes and clean concurrent
text edits propagate in both directions. On a nontrivial conflict the
collaborator's file state wins and the vault alternative is preserved in a
private vault PR for review; merging that PR sends the resolution back to the
collaborator on the next sync.

Keep configuration and credentials inaccessible to collaborators, and use
exactly **one sync host** (one machine, VM or runner) per vault.

## Prerequisites

- Linux (macOS works too)
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `git`
- GitHub CLI, authenticated: `gh auth login && gh auth setup-git`
  (the credential needs read/write contents on the vault and each
  collaborator repo, plus PR create/list on the vault)

That's all: no pip packages, no Java, no Copybara. `sc` is Python 3.11+
standard library only; uv provides a suitable Python automatically.

## Install

```sh
uv tool install git+https://github.com/SteffenPL/sc.git@v0.2.0
uv tool update-shell     # ensure ~/.local/bin is on PATH, if needed
```

Updates: `uv tool upgrade sc`. One-off use without installing:

```sh
uvx --from git+https://github.com/SteffenPL/sc.git sc status
```
## Quick start

```sh
sc init                    # writes an annotated sc.toml
$EDITOR sc.toml            # define your vault, collaborators and granted paths
sc doctor                  # verify prerequisites and the config
sc sync --initialize       # first run for a NEW pair (no baseline yet)
sc status                  # read-only report
sc watch                   # keep syncing every 15 minutes
```

## Commands

| Command | Description |
| --- | --- |
| `sc init [PATH]` | write a commented configuration template (default `./sc.toml`) |
| `sc doctor [-c F]` | check python/git/gh, authentication, config and state dir |
| `sc status [-c F]` | read-only report: branch tips, baselines, PRs, lock, last run |
| `sc sync [-c F] [--initialize]` | sync every configured collaborator once |
| `sc watch [-c F] [-i SECONDS]` | run `sc sync` in a loop (default every 900s) |
| `sc prs [-c F]` | list open vault PRs needing attention |
| `sc runner install\|remove\|status` | manage a self-hosted Actions runner |
| `sc workflow install` | install the sync workflow into the vault repo |
| `sc help [COMMAND]` | show help |

Config resolution: `--config`/`-c` > `$SC_CONFIG` > `./sc.toml` > `./sharing.toml`
> `~/.config/sc/config.toml`. Private logs and the lock live under
`$XDG_STATE_HOME/sc` (default `~/.local/state/sc`).

## Configuration

```toml
[vault]
repo = "OWNER/VAULT"                 # your private vault repository
# branch = "main"                   # optional; defaults to "main"

[collaborators.jenny]
repo = "OWNER/COLLABORATOR"        # the collaborator's shared repository
include = [                          # exact vault-relative paths only
    "Projects/Diet.md",
]

[collaborators.joi]
repo = "OWNER/SC-JOI"
prefix = "steffen-notes"             # optional: all granted files appear under
include = ["Projects/Henkaku Duties.md"]  # steffen-notes/... in the collaborator repo

[collaborators.bob]
repo = "ORG/BOB-SHARED"
include = ["Projects/Bob Project.md"]

# Optional additive grants: extra whole files for listed collaborators.
# A collaborator's effective set = its include + every matching [[shares]].
[[shares]]
paths = ["Projects/Example.md"]
to = ["jenny", "bob"]

[sync]                              # entirely optional
interval = 900                       # `sc watch` poll interval, seconds (>= 60)
```

Managing the map:

| Operation | How |
| --- | --- |
| Add collaborator | Add a `[collaborators.<name>]` block; run `sc sync --initialize` once (new pair has no baseline) |
| Remove collaborator | Set `include = []`, drop `[[shares]]` grants to them, run `sc sync` (removes shared files from their repo), then delete the block |
| Grant a file | Add the path to `include`, or add a `[[shares]]` entry |
| Revoke a file | Remove it from `include`/`shares`; the next sync deletes it from the collaborator repo |

Rules are strict on purpose: exact paths only — globs, `..`, `.git` and
absolute paths are rejected. Files are shared whole, not redacted; revocation
cannot erase old history or downloaded copies.

Path mapping: without `prefix`, a granted vault path appears at the same path
in the collaborator repo. With `prefix`, it appears at `<prefix>/<vault path>`
— useful to mirror your whole vault namespace inside a collaborator repo.
Baselines and recovery state stay keyed by vault path. **Caution:** adding or
changing `prefix` on an *existing* pair makes the mapped paths look deleted on
the collaborator side, which propagates deletions to your vault — first move
the files to the new prefix inside the collaborator repo (same content), then
change the config.

## `sc status` example

```
$ sc status
Config    /home/you/sc-config/sharing.toml — valid
Vault     OWNER/VAULT (main) @ 7c3f1ab
PRs       1 open vault PR(s)
jenny     OWNER/COLLABORATOR (main) @ 9a2d4e0
          1 granted path(s) · baseline sync-state/jenny: @ 7c3f1ab · 1 open review PR(s), e.g. #2
Lock      free
Last run  2026-09-15T09:45:00 UTC — ok, 0 conflicts
Log       /home/you/.local/state/sc/sync-error.log — empty
```

## Sync semantics

- Each allowed file is compared with its last shared snapshot on the private
  `sync-state/<name>` branch. One-sided additions, edits and deletions
  propagate either way; clean concurrent changes to printable-UTF-8 text
  merge automatically.
- Nontrivial conflicts (including modify/delete, non-text, mode conflicts)
  accept the **entire collaborator file state**; the vault alternative is
  preserved on a `sync-review/<name>/...` branch and a private vault PR.
  Merge the PR to restore the vault alternative, or close it to keep the
  collaborator's version. Resolutions flow back on the next sync.
- Normal (non-force) pushes only; concurrent edits reject the push. A failed
  run can leave one side ahead — rerun to converge. A local lock serializes
  runs on one host.
- Missing baselines stop sync by default. `--initialize` is for genuinely new
  pairs only; after state loss, restore the `sync-state` branch instead.
- Incoming files are never checked out or executed; only shared blobs and
  collaborator ancestry are pushed externally, never private vault history.
- The engine never checks out file contents into a working tree. Each run
  works on Git objects in a temporary directory (under `$XDG_STATE_HOME/sc`,
  deleted after each collaborator), so a sync host retains only the lock
  file, `last-run.json` and the private error log — no file copies persist.

## Automation on GitHub Actions

Two commands set up a self-hosted runner and the vault workflow:

```sh
sc runner install                     # registers this machine as a runner
                                      # (needs sudo for the systemd service)
sc workflow install --config-repo https://github.com/OWNER/SC-CONFIG.git
                                      # pushes .github/workflows/sc-sync.yml
                                      # into the vault repo
```

The workflow clones the **trusted private config repo** for `sharing.toml`
and runs the pinned `sc` installed on the runner host — trusted rules stay
private, the engine stays pinned. It triggers on vault pushes and every 15
minutes. Configure once on a dedicated VM:

```sh
uv tool install git+https://github.com/SteffenPL/sc.git@v0.2.0
gh auth login && gh auth setup-git
sc runner install
```

Instead of Actions, a VM can also run `sc watch` as a systemd user service
(template in `src/sc/templates/sc-watch.service`). Either way: one sync host.

## Development

```sh
uv run python -m unittest discover -s tests -v
```

Tests use real local Git repositories in temporary directories and stub only
the GitHub PR API; no network or credentials are needed.

## License

[MIT](LICENSE)
