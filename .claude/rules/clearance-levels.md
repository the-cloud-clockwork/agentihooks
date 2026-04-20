---
description: Operator clearance levels — what agents may and may not do
alwaysApply: true
---

# Clearance Levels

## Default: Full Clearance

Full clearance is the default. Most operations proceed without asking.

### Always Allowed (no signal needed)
- Git read ops: pull, fetch, log, diff, status, show, clone
- Push to any branch except main/master
- All destructive local ops: rm -rf, kubectl delete, docker rm, DROP TABLE
- Deploy ops: production restarts, rollouts, scaling
- Infrastructure: SSH, SCP, service management

### Permanently Blocked (hook-enforced, no clearance overrides)
- git push to main/master (direct push)
- git merge / git rebase targeting main/master
- git push --force / -f / --force-with-lease (any branch)
- git tag (use the release.yml workflow)
- git commit while HEAD is on main/master
- gh pr create without --base main

### Signal-Gated Operations

| Operation | Required Signal | Persistence |
|---|---|---|
| git checkout -b, git switch -c, git branch \<name\> | "new branch" / "create branch" / "feature branch" | Per-turn |
| gh pr create --base main | "open a PR" / "create a PR" / "make a PR" / "pr please" | Session (max 3, then re-signal) |
| gh pr merge to main, gh workflow run release.yml | "merge to main" / "ship it" / "release to prod" | Session |
| Docker/image ops with :latest/:prod/:stable | "hotfix" / "prod is down" / "outage" | Session |

## Restricting Clearance

| Command | Effect |
|---|---|
| "restrict clearance" / "careful mode" | Destructive ops require confirmation |
| "full clearance" / "back to normal" | Returns to default |

Restricted mode applies for the current task only, then reverts.

## Secrets — Absolute

Never handle credentials, API keys, tokens, or passwords in plaintext. No clearance level overrides this.
