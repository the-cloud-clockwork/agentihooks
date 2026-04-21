# Memory Mirror — cross-machine auto-memory sync (PR-gated)

Claude Code's native auto-memory lives at `~/.claude/projects/<project-key>/memory/`
and is machine-local. The **memory mirror** feature syncs only those `memory/`
subtrees across your fleet using [gitfoam](https://github.com/The-Cloud-Clock-Work/gitfoam)
for push (~500ms latency per machine) and a lightweight main-only consumer
on the sync daemon tick for pull (~60s).

**Scope:** memory only. Transcripts, session JSONLs, `ctx_refresh_*.json`
snapshots, and `todos/` are excluded by the rsync filter.

## Topology

```
machine A                                       machine B
─────────                                       ─────────
~/.claude/projects/*/memory/                    ~/.claude/projects/*/memory/
      │                                               ▲
      │ rsync (memory-only)                           │ merge — .conflict sibling on divergence
      ▼                                               │
~/.agentihooks/memory-mirror/                   ~/.agentihooks/memory-mirror/
      │                                               ▲
      │ gitfoam force-push 500ms                      │ git fetch origin main every 60s
      ▼                                               │
    gitfoam/A/main  ┐                                origin/main
                    │                                     ▲
                    └────── gh pr create ────────────────┘
                         (operator reviews + merges)
```

Each machine pushes to its OWN `gitfoam/<hostname>/main` branch. Nobody
consumes anyone else's branch directly. Everyone consumes `origin/main`.
Promotion is a GitHub PR.

## Prerequisites

- `git`, `rsync`, `tar` on PATH
- `gh` CLI (for `memory-sync propose`)
- A **private** GitHub repo you own — agentihooks does **NOT** create it
- `gitfoam` binary — either installed upstream or a local checkout pointed at by `GITFOAM_LOCAL_SOURCE`

## Enable

### 1. Create the private repo

```bash
gh repo create <org>/claude-memory-mirror --private --confirm
```

### 2. Configure `~/.agentihooks/.env`

```bash
MEMORY_MIRROR_MODE=write
MEMORY_MIRROR_REMOTE=git@github.com:<org>/claude-memory-mirror.git
# Optional (defaults):
# MEMORY_MIRROR_DIR=~/.agentihooks/memory-mirror
# MEMORY_MIRROR_BRANCH_PREFIX=gitfoam
# MEMORY_MIRROR_INTERVAL_SEC=60
# MEMORY_MIRROR_SWEEP_IDLE_DAYS=15
# GITFOAM_BINARY=~/.cargo/bin/gitfoam
# GITFOAM_LOCAL_SOURCE=/path/to/gitfoam    # for `cargo install --path`
```

Legacy `MEMORY_MIRROR_ENABLED=true` (v1) is still accepted and maps to
`MEMORY_MIRROR_MODE=write`.

### 3. Install

```bash
agentihooks memory-sync install
```

What this does:
- Verifies `gh` and remote reachability
- Builds or finds `gitfoam`
- Runs `ensure_mirror_repo` (git init, add remote)
- **Seeds `origin/main`** from your current memory (if main doesn't exist yet)
- Registers the mirror with `gitfoam init`
- Starts the gitfoam daemon (PID at `~/.agentihooks/gitfoam.pid`)

### 4. Verify

```bash
agentihooks memory-sync status
# mode:       write
# remote:     git@github.com:<org>/claude-memory-mirror.git
# mirror:     /home/you/.agentihooks/memory-mirror
# prefix:     gitfoam
# interval:   60s
# sweep idle: 15d
# gitfoam:    /home/you/.cargo/bin/gitfoam
# daemon:     running (PID …)
```

## Promoting a machine's learnings to main

```bash
agentihooks memory-sync propose                 # open PR, review on GitHub, merge manually
agentihooks memory-sync propose --auto-merge    # arm gh pr merge --auto --squash
```

`propose` compares `gitfoam/<hostname>/main` to `origin/main`; if they're
identical, it exits cleanly with "nothing to propose." Otherwise it opens a
PR with a short log summary as the body.

## Modes

| `MEMORY_MIRROR_MODE` | Behaviour |
|----------------------|-----------|
| `off` (default)      | Feature dormant. Tick no-ops. |
| `write`              | Snapshot + gitfoam push + fetch main + merge. Normal fleet member. |
| `write-local-only`   | Snapshot + gitfoam push. Never fetches or merges. For air-gapped contributors. |

## Housekeeping

```bash
agentihooks memory-sync sweep-branches            # uses MEMORY_MIRROR_SWEEP_IDLE_DAYS (default 15)
agentihooks memory-sync sweep-branches --idle-days 30
```

Deletes remote branches matching `<prefix>/*` that are:
1. Already merged into `origin/main` (via `git merge-base --is-ancestor`), AND
2. Idle (no new commits) longer than the threshold.

Unmerged branches are never deleted. Safe to put on a daily cron.

## Conflict model

`origin/main` evolves via PRs from many machines. When your local memory
differs from main on the same file, the merge step writes the incoming (main)
version to `<name>.conflict-<hostname>-<epoch><ext>` — your local file is
never overwritten. Resolve via `/memory`, delete the conflict sibling.

## Operations

```bash
agentihooks memory-sync start       # start gitfoam daemon
agentihooks memory-sync stop        # stop gitfoam daemon
agentihooks memory-sync sync-now    # force one tick now (snapshot + fetch main + merge)
agentihooks memory-sync uninstall           # stop daemon
agentihooks memory-sync uninstall --purge   # stop daemon AND delete mirror dir
```

Logs:
- gitfoam daemon: `~/.agentihooks/logs/gitfoam.log`
- pull tick: `~/.agentihooks/logs/sync-daemon.log`

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `MEMORY_MIRROR_REMOTE is not set` | env var missing | Set it, re-run `install` |
| `cannot reach MEMORY_MIRROR_REMOTE` | Repo doesn't exist, SSH key missing, or wrong URL | `gh repo create <org>/<name> --private --confirm`; check SSH |
| `gh` CLI not found (on `propose`) | Missing dependency | Install GitHub CLI: <https://cli.github.com/> |
| `gitfoam not found` on install | No binary, no local source | Install upstream OR set `GITFOAM_LOCAL_SOURCE` OR `GITFOAM_BINARY` |
| Transcripts appear on GitHub | Filter bug | **Report immediately — P0** |
| Nothing pushes | gitfoam daemon not running | `agentihooks memory-sync status` → `start` |
| Main stays empty across machines | No one seeded it | Re-run `agentihooks memory-sync install` — seed is idempotent |

## Known limitations (v2)

- **Same username required across machines.** Project keys under
  `~/.claude/projects/` encode the absolute path. Different usernames → different
  keys → memory doesn't match up. V2 still assumes same username fleet-wide.
- **Multi-tenant writers on a single machine race at the filesystem.** Ten pods
  sharing one mount all writing to the same `MEMORY.md` → last writer wins at
  the OS layer, before git sees anything. Upstream problem.
- **Propagation latency = PR review latency.** Machines see each other's writes
  only after a PR is merged to main. By design.
- **Tombstone-free deletion.** A file deleted on machine A is still on main;
  machine B will re-push it. Until a tombstone mechanism lands, prefer editing
  over deleting.

## Design notes

- Push delegated to gitfoam — handles per-host branch naming, secrets scanning,
  force-push throttling.
- Pull runs inside `scripts/sync_daemon.py`'s existing poll loop. No new daemon.
- PR gate via `gh pr create` — no server-side component.
- Seed step uses `git commit-tree` + `git update-ref` so it doesn't touch
  gitfoam's working branch, avoiding any race with the 500ms push loop.
