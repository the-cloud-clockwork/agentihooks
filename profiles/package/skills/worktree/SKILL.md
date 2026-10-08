---
name: worktree
description: >
  Create, list and remove the git worktree an agentic session works in, the same
  way in every repo: ~/dev/worktrees/REPO/NAME, branch NAME cut from fresh
  origin/BASE, where BASE is the WT_BASE_BRANCH setting (default dev). Run it as the first act of any development cycle, before the first
  edit. Use when a session starts work in a repo, when the operator says
  "worktree", "new worktree", "wt new", "list worktrees", "clean up the worktree",
  or when overlapping sub-agent work needs its own isolated checkout.
argument-hint: "new [--from REF] | tmp [--from REF] | ls [--all] | done <name> [--force | --pushed]"
---

# Worktree — one per development cycle, always off the base branch

## Script Resolution

```bash
WT="$(dirname "$(readlink -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/worktree/SKILL.md)")/scripts/wt.sh"
```

## Start a cycle

```bash
DEST="$("$WT" new --repo <repo-dir>)"
```

- The name comes from code, never typed: `agentihooks name worktree` builds it from the session (the plain swarm agent name, else `session-<id>`), then `-2`, `-3` for a further one; it is also the branch name. A name the session did not build is refused. Outside an agent session a typed name stands. `--repo` defaults to the repo containing the current directory; the primary checkout is resolved even from inside another worktree.
- Every edit, test, commit and push of this cycle happens under `$DEST`, by this session and by every sub-agent it launches. Brief each sub-agent with `$DEST` as its working directory and absolute paths inside it.
- A sub-agent gets its own worktree only when its files overlap another agent's. Create it with the same command, which takes the next built name; it is also cut from the base branch, and its branch ships to it on its own.
- A task stacked on an open dependency starts from that dependency's branch: `"$WT" new --repo <repo-dir> --from origin/<dependency-branch>` fetches it fresh and cuts the branch there. Names, refusals and limits are unchanged, and `done` still judges merged against `origin/<base>`.

Completion criterion: `$DEST` exists, `git -C "$DEST" rev-parse --abbrev-ref HEAD` prints `<name>`.

`new` refuses when free space (the smaller of the worktree filesystem and, on WSL, `/mnt/c`) is below `WT_MIN_FREE_GB` (50) or the repo already has `WT_MAX_PER_REPO` (60) worktrees. Answer a refusal by finishing worktrees (`done`) and running `agentihooks gc`, never by raising the limit. `new` records the calling session as owner (`agentihooks lease`), so the hourly lifecycle sweep never removes a worktree whose session is alive.

## Throwaway checkouts

```bash
T="$("$WT" tmp --repo <repo-dir> --from <ref>)"   # detached, under <repo>/_tmp/
# ... plant run, base-tree check, merge probe ...
"$WT" done "$T"
```

Every scratch checkout — plant runs, proof runs, base trees, merge probes — is a `tmp` worktree, never `git worktree add` into a scratch dir. `done` removes a `tmp` worktree even when dirty. One left behind is removed by the lifecycle sweep once its session is gone.

## Ship

Commit only the files this cycle touched, push `<name>`, open the PR with `--base` set to the base branch, wait for green checks, then submit the squash merge without `--delete-branch` (older `gh` then switches the worktree off its branch and `done` refuses). On a base branch with a merge queue, the merge only queues the PR; in a swarm, on a `dev` base, queue it instead with `agentihooks swarm <slug> merge queue <pr url>`, which uses the GitHub API and works with any `gh`. Confirm `gh pr view <pr> --json state --jq .state` reads `MERGED` before deleting the remote branch or running `done`. While queued or open, keep the branch and worktree. GitHub refuses a push to a branch whose PR sits in the queue. To fix a queued PR, dequeue it first (in a swarm, `agentihooks swarm <slug> merge dequeue <pr url>`), then push the fix, and once its checks pass queue it again the same way. A PR that leaves the queue without merging is fixed and queued the same way. After confirmed `MERGED`, run `done`, which deletes the remote branch itself. A repo that ships its own ceremony (`scripts/ship.sh`) uses it from inside `$DEST`.

## Inspect

```bash
"$WT" ls            # worktrees of the current repo
"$WT" ls --all      # every repo under the worktree root
```

## Finish

```bash
"$WT" done <name> --repo <repo-dir>
```

Whoever submits the merge owns it until the PR state reads `MERGED`, or reads closed and is dropped with `done --force`, then runs `done` for its worktree in that turn. Refuses a dirty worktree. Queued, open or unreadable PRs retain the remote branch and worktree, even with `--force`. A PR closed without merge is refused unless `--force`, which drops the worktree and local branch and keeps the remote branch. A confirmed merged PR permits remote branch deletion before teardown. Then fetches the base branch and fast-forwards the primary checkout to `origin/<base>` when it is on a clean base branch, and names the blocker otherwise. Deletes the local branch when it is on the remote base branch or its PR into it merged; otherwise keeps it and says so. `--force` overrides the dirty, closed without merge and local branch checks only. `--pushed` is for parked work: it skips the pull request checks, removes a clean worktree and its local branch once origin holds the worktree head, and keeps the remote branch; any other head is refused.

## Base-direct

When the operator explicitly asks to work on the base branch directly, skip this skill for that task: `git pull --ff-only` in the primary checkout, commit only touched files, push.

## Settings

`WT_BASE_BRANCH` names the base branch every worktree is cut from, compared against and synced back into; default `dev`. `WORKTREE_ROOT` overrides `~/dev/worktrees`; `"$WT" root` prints the active value.
