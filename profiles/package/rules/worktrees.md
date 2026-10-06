---
priority: 1
description: Every development cycle starts in its own worktree off the base branch, made and removed with the worktree skill
---

# Worktrees

- First act of every development cycle, any repo, any harness: `wt.sh new` from the `worktree` skill → `~/dev/worktrees/<repo>/<name>`, branch `<name>` off fresh `origin/<base>`. The base branch is `WT_BASE_BRANCH`, default `dev`. The name comes from code (`agentihooks name worktree|tmp`): the plain swarm agent name or the session, then `-2`, `-3`; a typed name is refused, and stands only outside an agent session.
- The session and every sub-agent it launches work inside that worktree. A second worktree only when files overlap.
- Never edit or commit in a repo's primary checkout, unless the operator explicitly asks to work on the base branch directly for a quick task: then pull, commit only touched files, push, no worktree, no pull request.
- Back to the base branch by pull request, merged on green, then `wt.sh done <name>`, which pulls the base branch into the primary checkout. Whoever merges runs `done`, in the same turn.
- Throwaway checkouts (plant and proof runs, base trees, merge probes): `wt.sh tmp --from <ref>`, then `wt.sh done "$T"`. Never `git worktree add` into a scratch dir.
- `wt.sh new` refuses below `WT_MIN_FREE_GB` (50) free or at `WT_MAX_PER_REPO` (60) worktrees per repo: finish worktrees and run `agentihooks gc`; never raise either limit to get past it.
