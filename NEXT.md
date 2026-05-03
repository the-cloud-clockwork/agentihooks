# NEXT — Agentihooks Hardening Backlog

Audit run: 2026-05-03. Two bugs already shipped (worktree auto-spawn + daemon sprawl).
Below: 9 remaining caveats, ordered by execution priority. Each entry: the bug, the
fix, and the file(s) to touch.

---

## 1. Concurrent `agentihooks init` race + `~/.claude.json` writer race

**Bug.** No top-level lock around `cmd_init` / `_install_global_inner`. When two
claude sessions both fire `agentihooks init` (SessionStart hook, agenticore boot,
operator opening a second terminal), they race on `~/.claude/`. Tonight `skills/`
vanished mid-session because Init A had `rmtree`'d it while Init B was halfway
through a different profile install. Same race exists between agentihooks merges
into `~/.claude.json` and Claude Code's own writes to that file.

**Fix.** Open `~/.agentihooks/init.lock` with `fcntl.flock(LOCK_EX | LOCK_NB)` at the
start of `cmd_init`. Bail with `"[sync] init already running, skipping"` if held.
Same pattern in `_merge_mcp_to_user_scope` to serialize against Claude Code.

**Files.**
- `scripts/install.py` (cmd_init, _install_global_inner, _merge_mcp_to_user_scope)

**Closes.** Caveats 1, 6, 8.

---

## 2. agenticore stomps operator state when run locally

**Bug.** `agenticore/agenticore/hooks.py:run_agentihooks_init()` runs
`agentihooks init --profile <agenticore_profile>` (default = `default`). On a pod
it writes to `/shared/.agentihooks` (line 478), isolated. Locally, `AGENTIHOOKS_HOME`
is unset → it lands in the operator's `~/.agentihooks/state.json` and overwrites
the pinned profile. We saw this drive `profile=anton` → `profile=default` tonight,
which then fed the worktree explosion.

**Fix.** Export `AGENTIHOOKS_HOME` before invoking `agentihooks init`. On a pod, use
`/shared/.agentihooks-${HOSTNAME}`. Locally, use a dedicated dir like
`~/.agentihooks-agenticore`. Per-pod isolation already works in agentihooks; agenticore
just isn't using it.

**Files.**
- `agenticore/agenticore/hooks.py` (run_agentihooks_init)

**Closes.** Caveat 2.

---

## 3. Silent failure when profile resolution returns None

**Bug.** `_resolve_profile_dir("anton")` returns `None` if `state.bundle.path` is
missing/wrong. `cmd_claude` swallows that into `claude_flags = {}` → builds `claude`
with no permission_mode, no model, no effort. Operator launches → no bypass mode,
raw permission prompts. Looks identical to "agentihooks broke" with no signal.

**Fix.** If `_resolve_profile_dir(primary)` is None in `cmd_claude`, print
`[ERROR] Profile '<name>' not found — bundle path stale; run 'agentihooks bundle link <path>'`
to stderr and `sys.exit(1)` instead of building an empty-flag claude command.

**Files.**
- `scripts/install.py` (cmd_claude)

**Closes.** Caveat 3.

---

## 4. Drop the `,brain` chain element from `~/.agentihooks.json`

**Bug.** `~/.agentihooks.json` has `"profile": "anton,brain"`. Bundle has
`agenticore`, `anton`, `smith` — no `brain`. Chain logic skips silently with a WARN
during init. Brain profile was decoupled to `agentibrain-kernel` repo on 2026-04-27
and never got a stub in the bundle.

**Fix.** Operator edit `~/.agentihooks.json` → `"profile": "anton"`. One line.
(Or restore a brain stub in the bundle that pulls overlay config from
agentibrain-kernel.)

**Files.**
- `~/.agentihooks.json` (operator's home, not the repo)

**Closes.** Caveat 7.

---

## 5. Debounce daemon "removed" detection during git operations

**Bug.** `sync_daemon._diff_hashes` hashes bundle source files at every poll tick.
Any transient git state — rebase, stash, checkout, worktree add — surfaces as
"file removed" → daemon fires `mcp_sync` + `blacklist projects` actions. We saw 30+
false removals during tonight's rebase, each triggering downstream `~/.claude.json`
churn.

**Fix.** Require N=2 consecutive ticks of "removed" before action fires. Persist
removal candidates in `~/.agentihooks/removal-candidates.json`. Promote to
real-removal only after second confirmation. Alternative: hash via
`git show HEAD:<path>` so working-tree edits don't register.

**Files.**
- `scripts/sync_daemon.py` (_diff_hashes, _execute_actions)

**Closes.** Caveat 5.

---

## 6. Bundle-watcher healthcheck on daemon tick

**Bug.** Every `~/.claude/skills/*`, `agents/*`, `commands/*`, `rules/*` is a symlink
into the bundle dir. If bundle is moved/renamed, every skill breaks silently. No
failsafe.

**Fix.** On daemon tick, `os.path.exists(state.bundle.path)`. If False:
- Log loud warning.
- Refuse `_install_user_assets` mass-update (it would orphan symlinks).
- Surface a banner via `inject_context` so the next claude session sees
  "Bundle dir gone — run `agentihooks bundle link <path>` to recover".

**Files.**
- `scripts/sync_daemon.py` (poll loop)
- `scripts/install.py` (_install_user_assets)

**Closes.** Caveat 9.

---

## 7. Pytest test isolation

**Bug.** `tests/test_install.py`, `test_status_checker.py`, `test_sync_daemon.py`,
`test_install_validation.py` call `_load_state()` / `_save_state()` /
`_bundle_link()` without monkeypatching `AGENTIHOOKS_HOME`. They write directly to
the operator's real state. Running `pytest tests/` against a live install poisons
state.json — exactly how tonight's bundle path got pointed at a `/tmp/pytest-…`
directory that no longer exists.

**Fix.** Add `tests/conftest.py` autouse fixture:

```python
import pytest
from pathlib import Path

@pytest.fixture(autouse=True)
def _isolate_agentihooks_home(tmp_path, monkeypatch):
    home = tmp_path / "agentihooks-home"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    # Force re-evaluation of module-level constants
    import importlib
    from hooks import config
    importlib.reload(config)
    from scripts import install
    monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", home)
    monkeypatch.setattr(install, "STATE_JSON", home / "state.json")
```

**Files.**
- `tests/conftest.py` (new file)

**Closes.** Caveat 1 hardening (the fix in #1 stops races; this stops poisoning).

---

## 8. Per-pod K8s state isolation

**Bug.** `flock` semantics on NFS / CIFS / many ReadWriteMany backends are advisory
or unreliable. Multiple agenticore worker pods sharing `/shared/.agentihooks` over
NFS will still get sprawl because flocks aren't honored cross-node.

**Fix.** Set `env: AGENTIHOOKS_HOME` per pod via Downward API in chart values:

```yaml
env:
  - name: AGENTIHOOKS_HOME
    value: /shared/.agentihooks-$(POD_NAME)
```

Each pod gets its own state dir and its own lock-file inode. No code change.

**Files.**
- `k8s/charts/agenticore/values.yaml` (and any chart spawning workers that call agentihooks)

**Closes.** Caveat 4.

---

## 9. Garbage-collect `~/.claude/backups/`

**Bug.** Claude Code writes `~/.claude.json.backup.<timestamp>` files to
`~/.claude/backups/` on every internal write. They're never pruned. Slow disk-bloat.
Not critical but lives forever.

**Fix.** Add to `sync_daemon` periodic housekeeping (e.g., once per hour): delete
backup files older than 24h. ~10 lines.

**Files.**
- `scripts/sync_daemon.py` (poll loop or hourly hook)

**Closes.** Tail of caveat 8.

---

## Execution order (recommendation)

| # | Item | Effort | Impact |
|---|------|--------|--------|
| 1 | init flock | 30m | High — stops tonight's class of breakage |
| 2 | agenticore AGENTIHOOKS_HOME | 10m | High — stops state.json drift |
| 3 | cmd_claude loud-fail on missing profile | 5m | High — visibility |
| 4 | Drop `,brain` from `~/.agentihooks.json` | 1m | Low (already silently handled) |
| 5 | Daemon debounce | 1h | Medium — kills churn noise |
| 6 | Bundle healthcheck | 30m | Medium — catches catastrophic moves |
| 7 | Pytest isolation | 30m | High — prevents future operator state poisoning |
| 8 | K8s per-pod env | 10m chart edit | Medium — only matters when scaling pods |
| 9 | Backup GC | 20m | Low — disk hygiene |

Bundle them however makes sense. #1 + #2 + #3 + #7 in one PR closes the entire
"agentihooks randomly breaks" class of bugs.

---

## Already shipped this session (do NOT reopen)

- `fix(claude): never auto-pass --worktree from profile.yml` (commit `2e7c2aa`)
- `fix(daemon): singleton flock survives PID-file deletion` (commit `ab06db9`)
- `feat(controls): bypass mode lifts secrets-in-files block` (commit `6ab173d`)
- `fix(mcp): reseed bundle+profile mcpServers on every sync` (commit `ecf1d10`)
- `feat(bashrc): also source ~/.env if present` (commit `2442eef`)
