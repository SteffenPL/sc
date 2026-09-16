# sc

Sync parts of a private vault — Obsidian, knowledge base, notes — with
collaborator repos. Built for **shared agent context**: you and a
collaborator — person or AI agent — each edit your own repo; granted files
sync both ways on `main`.

- private by default — only granted paths sync; whole files, never history
- concurrent edits auto-merge; conflicts freeze with review PRs on both sides
- plain TOML config; stdlib-only Python 3.11+; needs git + gh
- `sc ui`: local web editor for maps and permissions, with a live vault
  tree, gh repo picker and one-click git commits of the config
- replaces [Copybara](https://github.com/google/copybara) for this use case:
  no Java/Bazel, no generated Starlark
- one sync host — don't run two parallel sync services

## Quick start

```sh
uv tool install git+https://github.com/SteffenPL/sc.git@v0.4.2
sc init                       # writes an annotated sc.toml
sc ui                         # edit maps + permissions in the browser
sc doctor                     # verify prerequisites and config
sc sync --initialize          # once per new pair
sc status                     # read-only report
sc watch                      # keep syncing — or use a runner / Docker below
```

## Config

```toml
[[maps]]
name = "maya"
from = "you/notes"                # prefix inferred from the path: "" here
to   = "maya/context/inbox"      # granted paths land under inbox/ there

[[maps]]
name = "kai"
from = "you/notes"
to   = "kai/shared"
merge = "union"                   # opt-in: keep both sides' overlapping lines

[[permissions]]
repo = "you/notes"                # context; allows relative paths below
paths = [
    "projects/agent-logs.md",
    "{projects,references}/*",     # globs: * one segment, ** any depth
]
maps = ["maya", "kai"]

[sync]
interval = 300                    # sc watch poll, seconds (>= 60)
```

- `[[maps]]` pairs two repos. A path suffix is a folder prefix:
  stripped on one side, added to the other.
- `[[permissions]]` define what syncs — nothing else moves. Globs grant
  whole folders: new files from either side sync automatically. Check with
  `sc sync --dry-run`.
- defaults: `merge = "text"`, `conflict = "swap"`, `bi_directional = true`;
  `bi_directional = false` = one-way mirror (source overwrites target).
- revoking a path removes it from the other side; your side keeps it. Old
  copies can never be taken back.
- `sc ui [-c F] [-p PORT]` edits this file without a text editor: edits are
  buffered in the browser, applied surgically (comments and formatting are
  preserved), validated, and committed to the config's git repository on
  demand — never pushed. The permissions tab shows the live file tree of a
  mapped repo with the active maps per folder.

## Sync behavior

| Situation | Result |
| --- | --- |
| one-sided change (add/edit/delete) | propagates both ways |
| concurrent edits, clean hunks | 3-way auto-merge |
| overlapping text edits | union-merge when `merge = "union"`, else conflict |
| conflict (binary, mode, modify/delete, overlap without union) | files freeze — each repo keeps its version and gets a review PR; merge one, the next sync does the rest |

- regular pushes only; safe to rerun after failures
- baselines: private `sync-state/<map>` branches on the `from` repo
- incoming files are never checked out or executed; the sync host keeps no file copies

## Commands

| Command | Purpose |
| --- | --- |
| `sc init [PATH]` | write config template |
| `sc ui [-c F] [-p PORT]` | local web editor for maps + permissions |
| `sc doctor [-c F]` | preflight: python/git/gh, auth, config |
| `sc status [-c F]` | read-only report: tips, baselines, PRs, last run |
| `sc sync [-c F] [--initialize] [--dry-run]` | sync all maps once |
| `sc watch [-c F] [-i SECONDS]` | loop `sc sync` |
| `sc prs [-c F]` | list open PRs in mapped repos |
| `sc runner install\|remove\|status` | self-hosted Actions runner |
| `sc workflow install` | push workflow into the mapped repo |

Config lookup: `-c` > `$SC_CONFIG` > `./sc.toml` > `~/.config/sc/config.toml`.
Host state (lock, last-run, private error log): `~/.local/state/sc`.

## Automation — GitHub Actions runner

```sh
sc runner install      # register this machine as a runner (sudo for the service)
sc workflow install --config-repo https://github.com/you/sc-config.git
```

Runs on vault pushes and every 15 minutes. The runner clones your private
config repo and runs the pinned `sc`. Prefer no Actions at all? Run
`sc watch` yourself (systemd template included).

## Docker

```sh
cp compose.example.yml compose.yml
export GH_TOKEN=<fine-grained PAT: contents rw + PR create/list>
docker compose up -d    # runs sc watch; mount config at /config/sc.toml
```

`GH_TOKEN` is the only secret. Baselines live in the repos, so the container
is stateless. Don't run two parallel sync services.

## Development

```sh
uv run python -m unittest discover -s tests -v
```

Real local Git repos; only GitHub PR calls are stubbed.

MIT — see [LICENSE](LICENSE).
