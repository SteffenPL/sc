# sc

Sync parts of a private vault — Obsidian, knowledge base, notes — with
collaborator repos. Built mainly for **shared agent context**: you and your
collaborator (person or AI agent) each commit to your own repo, and the
explicitly granted files stay in sync, both directions, directly on `main`.

- private by default — only granted paths sync; whole files, never history
- concurrent edits auto-merge; conflicts freeze with review PRs on both sides
- plain TOML config; stdlib-only Python 3.11+; needs git + gh
- replaces [Copybara](https://github.com/google/copybara) for this use case:
  no Java/Bazel, no generated Starlark
- **one sync host per vault** — local install, Docker, or Actions runner;
  never two against the same repositories

## Quick start

```sh
uv tool install git+https://github.com/SteffenPL/sc.git@v0.3.0
sc init                       # writes an annotated sc.toml
$EDITOR sc.toml               # define maps + permissions
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

- `[[maps]]` = folder-level routing between two repos; the path suffix is a
  prefix, stripped from that side and added to the other.
- `[[permissions]]` = the only grants. Globs pre-consent whole folders:
  files created later by **either** side sync automatically. Preview with
  `sc sync --dry-run`.
- defaults: `merge = "text"`, `conflict = "swap"`, `bi_directional = true`;
  `bi_directional = false` = one-way mirror (source overwrites target).
- revoking a path removes it from the other side; your side keeps it. Old
  copies can never be taken back.

## Sync behavior

| Situation | Result |
| --- | --- |
| one-sided change (add/edit/delete) | propagates both ways |
| concurrent edits, clean hunks | 3-way auto-merge |
| overlapping text edits | union-merge when `merge = "union"`, else conflict |
| conflict (binary, mode, modify/delete, overlap without union) | freeze: each main keeps its own version; review PR on **both** repos; merge one → next sync converges both and auto-closes the other |

- normal pushes only — no force, no history rewrites; failed runs converge on rerun
- baselines: private `sync-state/<map>` branches on the `from` repo
- incoming files never checked out or executed — object-level Git only,
  temp dir deleted per run

## Commands

| Command | Purpose |
| --- | --- |
| `sc init [PATH]` | write config template |
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

The workflow triggers on vault pushes plus a 15-minute schedule, clones the
trusted private config repo, and runs the pinned `sc` (labels: `sc-sync`).
Local alternative to a runner: `sc watch` via the systemd unit template.

## Docker

```sh
cp compose.example.yml compose.yml
export GH_TOKEN=<fine-grained PAT: contents rw + PR create/list>
docker compose up -d    # runs sc watch; mount config at /config/sc.toml
```

`GH_TOKEN` is the only secret; the container is stateless — baselines live in
the repositories. Either local install, Docker, or runner — never two.

## Development

```sh
uv run python -m unittest discover -s tests -v
```

Real local Git repos; only GitHub PR calls are stubbed.

MIT — see [LICENSE](LICENSE).
