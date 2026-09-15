# sc — shared context

Allowlisted **bidirectional sync** between mapped Git repositories. Private by
default: only files granted in a TOML config ever sync, and only file
contents — never private history — are shared.

A **map** pairs two repositories (each optionally under a folder prefix that
is stripped/added per side). A **permission** grants path patterns through a
named map. Both sides commit directly to their default branch; one-sided
changes and clean concurrent text edits propagate both ways, and conflicts
freeze with review PRs on **both** sides.

Keep configuration and credentials inaccessible to collaborators, and use
exactly **one sync host** (one machine, VM or runner) per vault.

## Relation to [Copybara](https://github.com/google/copybara)

sc overlaps with Copybara, Google's Starlark-configured tool for moving code
between repositories. This project began as a Copybara setup and replaced it:
the use case — allowlisted, bidirectional sharing of individual files between
a private vault and collaborator repositories, with whole-file privacy
semantics and conflict review PRs — needs policies that Copybara does not
express directly. sc is standard-library Python (no Java/Bazel, no generated
Starlark), configured in plain TOML, and transfers only granted file
contents, never repository history.

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

**Pick exactly one deployment — a local install, [Docker](#run-in-docker), or
a self-hosted Actions runner — and never run two against the same
repositories.** Concurrent syncs fail safely (non-force pushes reject each
other), but they violate the one-sync-host rule and produce noise and races.

```sh
uv tool install git+https://github.com/SteffenPL/sc.git@v0.3.0
uv tool update-shell     # ensure ~/.local/bin is on PATH, if needed
```

Updates: `uv tool upgrade sc`. One-off use without installing:

```sh
uvx --from git+https://github.com/SteffenPL/sc.git sc status
```

## Run in Docker

Alternative to a local install — the same **either/or** rule applies: run a
local install *or* the container, never both. The repository ships an example
[`Dockerfile`](Dockerfile) (Debian slim with git, a current GitHub CLI and the
pinned `sc`) and [`compose.example.yml`](compose.example.yml):

```sh
cp compose.example.yml compose.yml      # adjust the config mount if needed
export GH_TOKEN=<fine-grained PAT>      # contents rw on both repos + PR create/list
docker compose up -d                    # runs `sc watch` (default every 900s)
docker compose logs -f
```

- Mount your config at `/config/sc.toml` — `sc` picks it up automatically.
- `GH_TOKEN` is all the container needs: `gh` reads it directly and the image
  preconfigures git's credential helper. No `gh auth login` inside.
- The container is stateless: baselines live in your repositories
  (`sync-state/*` branches). The `/state` volume only keeps the lock, the
  last-run report and the private error log — optional, but nice to persist.
- Build another engine version with `docker build --build-arg SC_VERSION=…`.

## Quick start

```sh
sc init                    # writes an annotated sc.toml
$EDITOR sc.toml            # define maps, permissions, granted paths
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
| `sc sync [-c F] [--initialize] [--dry-run]` | sync every configured map once; `--dry-run` previews without pushing |
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
[[maps]]
name = "jenny"                                # unique; names sync-state/jenny
from = "OWNER/VAULT"                          # prefix "" (repository root)
to   = "OWNER/COLLABORATOR"                   # files appear at their vault paths

[[maps]]
name = "joi"
from = "OWNER/VAULT"                          #   .../Projects/Henkaku Duties.md
to   = "OWNER/SC-JOI/steffen-notes"          # ↔ .../steffen-notes/Projects/Henkaku Duties.md
merge = "union"                               # opt-in union auto-merge (see below)

[[permissions]]
repo = "OWNER/VAULT"                          # optional context for relative paths
paths = [
    "Projects/Diet.md",
    "Projects/Mums birthday.md",
    "{Projects,Views}/*",                     # globs: '*' one segment, '**' any depth
]
maps = ["jenny"]

[[permissions]]
repo = "OWNER/VAULT"
paths = ["Projects/Henkaku Duties.md"]
maps = ["joi"]

[sync]                                        # entirely optional
interval = 900                                 # `sc watch` poll interval, seconds (>= 60)
```

- **Maps** are folder-level routing: `from`/`to` are `Owner/Repo[/folder...]`;
  the folder prefix is stripped from that side's paths and added to the other
  side's outgoing files, so `A/prefix_a/x.md` maps to `B/prefix_b/x.md`.
  Defaults: `from_branch`/`to_branch` = `main`, `merge = "text"`,
  `conflict = "swap"`, `bi_directional = true`.
- **Permissions** are the grants — a map shares nothing by itself. Paths may
  anchor on either endpoint of a named map. Globs (`*`, `**`, `{a,b}`) are
  folder-level pre-consent: files later created under them by either side
  sync automatically. Preview the expanded matches with `sc sync --dry-run`.
- Paths and prefixes reject `..`, `.git`, globs-in-prefixes and absolute
  paths; validation errors stop sync before anything is pushed.

Managing the map:

| Operation | How |
| --- | --- |
| Add a pair | Add a `[[maps]]` entry + permissions; run `sc sync --initialize` once (no baseline yet) |
| Stop sharing someone | Remove their permissions, run `sc sync` (their side drops previously shared files), then remove the map |
| Grant a file | Add the path to a permission for that map (or a glob) |
| Revoke a file | Remove it from the permission; next sync removes it from the other side (your side keeps it; old copies can't be taken back) |

Rules are strict on purpose. Files are shared whole, not redacted; revocation
cannot erase old history or downloaded copies. Changing a side's folder prefix
on an existing map requires moving the files there first (same content), or
they read as deletions.

## `sc status` example

```
$ sc status
Config    /home/you/sc-config/sharing.toml — valid
jenny     OWNER/VAULT (main) @ 7c3f1ab
          OWNER/COLLABORATOR (main) @ 9a2d4e0
          2 path pattern(s) · baseline sync-state/jenny: @ 7c3f1ab · merge=text, conflict=swap
joi       OWNER/VAULT (main) @ 7c3f1ab
          OWNER/SC-JOI/steffen-notes (main) @ 3f80a12
          1 path pattern(s) · baseline sync-state/joi: MISSING · merge=union, conflict=swap
          new pair? `sc sync --initialize`; lost state? restore the branch
PRs       1 open PR(s) across mapped repositories
Lock      free
Last run  2026-09-15T09:45:00 UTC — ok, 0 conflicts
Log       /home/you/.local/state/sc/sync-error.log — empty
```

## Sync semantics

Per granted file, the merge ladder applies:

1. Identical or one-sided changes (including deletions and new files
   matching a glob) propagate in both directions.
2. Clean concurrent changes to printable-UTF-8 text merge automatically
   (3-way merge against the private baseline).
3. With `merge = "union"` (opt-in per map), overlapping edits union-merge:
   both sides' lines are kept. Both repositories see the combined result
   immediately; fix anything awkward by editing, the next sync propagates it.
4. Everything else — overlapping edits without union, binary/non-UTF-8
   content, mode changes, modify/delete conflicts — is **frozen** according
   to the map's `conflict` mode:
   - `"swap"` (default): each side keeps its own version on main and gets a
     review PR offering the other side's version. Merge at most one of the
     two PRs; the next sync converges both repositories and auto-closes the
     counterpart PR. Hand-editing anywhere works too.
   - `"to-wins"`: the `to` side's whole file state lands on both mains; the
     `from` alternative is preserved in a PR on the `from` repository.
   - `"freeze"`: freeze only, no PRs.

`bi_directional = false` turns a map into a one-way mirror: the `from` side is
authoritative, `to`-side edits are overwritten on the next sync (counted in
the report), and nothing is ever imported from `to`.

Other invariants:

- Normal (non-force) pushes only; concurrent edits reject the push. A failed
  run can leave one side ahead — rerun to converge. A local lock serializes
  runs on one host.
- Missing baselines stop sync by default. `--initialize` is for genuinely new
  pairs only; after state loss, restore the `sync-state` branch instead.
- Incoming files are never checked out or executed; only shared blobs are
  pushed between endpoints, never either side's history.
- The engine never checks out file contents into a working tree. Each run
  works on Git objects in a temporary directory (under `$XDG_STATE_HOME/sc`,
  deleted after each map), so a sync host retains only the lock file,
  `last-run.json` and the private error log — no file copies persist.

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
uv tool install git+https://github.com/SteffenPL/sc.git@v0.3.0
gh auth login && gh auth setup-git
sc runner install
```

Instead of Actions, a VM can also run `sc watch` as a systemd user service
(template in `src/sc/templates/sc-watch.service`). Whichever you choose —
Actions runner, Docker, or local install — run exactly one sync host.

## Development

```sh
uv run python -m unittest discover -s tests -v
```

Tests use real local Git repositories in temporary directories and stub only
the GitHub PR API; no network or credentials are needed.

## License

[MIT](LICENSE)
