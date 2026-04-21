# Memory Mirror — cross-machine auto-memory sync

Claude Code's native auto-memory lives at `~/.claude/projects/<project-key>/memory/`
and is machine-local. The **memory mirror** feature syncs only those `memory/`
subtrees across your fleet using [gitfoam](https://github.com/The-Cloud-Clock-Work/gitfoam)
for push (~500ms latency) and a lightweight consumer on the sync daemon tick
for pull (~60s by default).

**Scope:** memory only. Transcripts, session JSONLs, `ctx_refresh_*.json`
snapshots, and `todos/` are explicitly excluded by the rsync filter.

## Topology

```
machine A                                       machine B
─────────                                       ─────────
~/.claude/projects/*/memory/                    ~/.claude/projects/*/memory/
      │                                               ▲
      │ rsync (memory-only filter)                    │ merge (with conflict-file fallback)
      ▼                                               │
~/.agentihooks/memory-mirror/                   ~/.agentihooks/memory-mirror/
      │                                               ▲
      │ gitfoam push 500ms                            │ git fetch every tick
      ▼                                               │
                        GitHub private repo
                         gitfoam/<host>/main branches
```

Each machine pushes to its own branch (`gitfoam/<hostname>/main`) and fetches
every other branch. Self-branch is skipped to avoid loops.

## Prerequisites

- `git`, `rsync`, and `tar` on PATH (they usually already are on Linux/macOS)
- A private GitHub repo you own, seeded with one commit on `main`
- `gitfoam` binary — either install globally or build locally from a checkout

## Enable

### 1. Configure `~/.agentihooks/.env`

```bash
MEMORY_MIRROR_ENABLED=true
MEMORY_MIRROR_REMOTE=git@github.com:YOUR-ORG/claude-memory-mirror.git
# Optional (defaults shown):
# MEMORY_MIRROR_DIR=~/.agentihooks/memory-mirror
# MEMORY_MIRROR_BRANCH_PREFIX=gitfoam
# MEMORY_MIRROR_INTERVAL_SEC=60
# GITFOAM_BINARY=~/.cargo/bin/gitfoam
# GITFOAM_LOCAL_SOURCE=/path/to/gitfoam    # for cargo install --path
```

### 2. Install

```bash
agentihooks memory-sync install
```

This will:
- Verify or build `gitfoam` (from `$GITFOAM_LOCAL_SOURCE` via `cargo install --path …`)
- `git init` `$MEMORY_MIRROR_DIR` and add your remote
- Register the mirror with `gitfoam init`
- Start the gitfoam daemon with its PID at `~/.agentihooks/gitfoam.pid`

The sync daemon (already running if you ran `agentihooks init`) will start
calling the pull side on every poll.

### 3. Verify

```bash
agentihooks memory-sync status
# enabled:   True
# remote:    git@github.com:…
# mirror:    /home/you/.agentihooks/memory-mirror
# prefix:    gitfoam
# interval:  60s
# gitfoam:   /home/you/.cargo/bin/gitfoam
# daemon:    running (PID …)
```

Touch a file in `~/.claude/projects/*/memory/` and within a second or two
you should see a force-push on `gitfoam/<hostname>/main` in your GitHub repo.

## Operations

```bash
agentihooks memory-sync start       # start gitfoam daemon
agentihooks memory-sync stop        # stop gitfoam daemon
agentihooks memory-sync sync-now    # manual tick (snapshot + fetch + merge)
agentihooks memory-sync uninstall           # stop daemon
agentihooks memory-sync uninstall --purge   # stop daemon AND delete mirror dir
```

The gitfoam log is at `~/.agentihooks/logs/gitfoam.log`. The pull side is
logged by the sync daemon at `~/.agentihooks/logs/sync-daemon.log`.

## Conflicts

When two machines modify the same memory file between ticks, the merge step
refuses to overwrite. Instead it writes a sibling file named
`<stem>.conflict-<hostname>-<epoch><ext>` next to the target. Inspect with
`/memory`, reconcile manually, delete the conflict file when done.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `MEMORY_MIRROR_REMOTE is not set` | Remote missing from `.env` | Set it, re-run `install` |
| `gitfoam not found` on install | Neither binary nor `GITFOAM_LOCAL_SOURCE` present | Install rust + gitfoam checkout, or use the upstream installer |
| Nothing pushes to GitHub | gitfoam daemon not running | `agentihooks memory-sync status` → `start` |
| Other machines' memory never lands | Sync daemon not running, or mirror repo has no remote | `agentihooks daemon status`, `agentihooks memory-sync status` |
| Transcripts leak into GitHub commits | rsync filter bug | Report a bug — this is P0 |

## Known limitations (V1)

- **Same username required across machines.** Project keys under
  `~/.claude/projects/` encode the absolute path, so `/home/alice/...` and
  `/home/bob/...` produce different sub-directories. A project-identity
  resolver is on the roadmap.
- **Last-write-wins per tick.** Simultaneous writes to the same file are
  turned into conflict-file siblings rather than being merged content-wise.
- **Deletion is not authoritative.** A file deleted on machine A will be
  re-created by machine B's next push if B still has the file. Until a
  proper tombstone mechanism lands, prefer editing over deleting.

## Design notes

- Push is delegated to `gitfoam`. It handles throttling, entropy-based secret
  detection, and per-host branch isolation. This feature intentionally does
  not reimplement any of that.
- Pull runs inside the existing `scripts/sync_daemon.py` poll loop rather
  than a new process — one daemon, one tick. Gated on `MEMORY_MIRROR_ENABLED`.
- The feature is opt-in. A machine that does not set the env vars is a
  no-op, even with the code installed.
