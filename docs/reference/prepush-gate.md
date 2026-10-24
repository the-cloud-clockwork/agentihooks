---
title: Pre push gate
parent: Reference
nav_order: 44
---

# Pre push gate

`python -m scripts.ci_prepush` runs the cheap CI gates on the committed `HEAD`
before a push: `ruff check`, `ruff format --check` and the size and complexity
grader. `agentihooks swarm <id> publish` runs it after its commit and pushes
only when it passes. The size stage grades the branch against the allowlist
and grader at the merge base of the branch and its base, so a unit the branch
never touched does not fail the push after `dev` shrinks or removes that
unit's allowlist entry.

## Usage

Commit first; the gate grades `HEAD` and refuses a dirty tree:

```bash
V=~/dev/tcc-ecosystem/.venv/bin
$V/python -m scripts.ci_prepush
```

```text
ci_prepush: ruff check
ci_prepush: ruff format
ci_prepush: size limits
412 units over a limit, 412 on the base allowlist, 0 errors
ci_prepush: every cheap gate passed on 1a2b3c4d5e6f; push it.
```

A pass writes the graded commit to `ci_prepush` in the repository's git
directory. A failure names the failed stages:

```text
ci_prepush: size limits failed; fix them, commit and run again before pushing.
```

| Flag | Default | Effect |
|---|---|---|
| `--base REF` | `origin/dev` | The branch the push targets. With the default the gate first runs `git fetch --no-tags origin dev`; any other ref is used as it stands locally. |

Every environment variable whose name contains `REDIS` is removed from the
gate commands' environment. Local tests run only when
`AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true`; see
[Local test guard](local-test-guard.md).

## Push spacing

The push guard also refuses a passed `git push` to a branch whose last push
from this clone is younger than `AGENTIHOOKS_PUSH_SPACING_S` seconds (default
300; `0` turns it off), in every repository it guards. The time comes from the
`update by push` entries in the remote tracking reflog of the remote the push
names, or of `@{push}` for a bare `git push`, so the check costs no GitHub
call. Over the day measured in issue 6909, pull request Tests runs took at
least 4.6 minutes at the tenth percentile, so a push inside that window cancels
the run the previous push started. The refusal names the time the push may go;
keep committing and push once, so one run grades every commit. Delete
refspecs, dry runs and `diffcheck/` refspecs are not spaced, and a push to a
URL rather than a named remote has no reflog to read and is not spaced.

## How the size stage picks its base

The gate computes `git merge-base <base> HEAD` and copies two files from that
commit into a temporary grading directory:

- `tests/SIZE_ALLOWLIST.json`, the units allowed over a limit and their caps
- `scripts/size_limits.py`, the grader itself

It then runs that grader over the branch's whole tracked tree:

```bash
python -I <grade>/scripts/size_limits.py --base <grade> --head <repo root>
```

The grader measures every unit over a limit in the branch tree and compares
each with the merge base allowlist:

- A unit over a limit with no merge base entry fails:
  `<unit> breaks <rule> at <value>; it is not on the base allowlist`.
- A unit above its merge base cap fails:
  `<unit> grew on <rule> from <cap> to <value>`.
- The branch's own `tests/SIZE_ALLOWLIST.json` must equal the measurement,
  otherwise it fails with
  `tests/SIZE_ALLOWLIST.json differs from the head's measurement; run python -m scripts.size_limits --write`.

A unit the branch did not change measures the same as at the merge base, where
the allowlist recorded it, so it passes. Only units the branch changed can
fail. Because the grader also comes from the merge base, a branch cannot
loosen the limits it is graded by.

### Example

1. A branch is cut from `dev` while `scripts/reconcile.py::reconcile_unit` is 340
   lines and allowlisted at 340.
2. `dev` then shrinks that function to 280 lines and removes its allowlist
   entry.
3. The branch changes only `scripts/beta.py` and runs the gate.

Before
[PR #4770](https://github.com/the-cloud-clockwork/agentihooks/pull/4770), the gate
read the allowlist from current `origin/dev`, so the branch's older 340 line
copy failed with
`scripts/reconcile.py::reconcile_unit breaks function-lines at 340; it is not on the base allowlist`
and the branch had to merge `dev` to push. The gate now reads the merge base
allowlist, where the entry still reads 340, and the push passes.

## Where real growth is still refused

The pre push gate never sees units `dev` changed after the branch was cut. The
pull request tier of the Tests workflow grades them: it checks out
`pull_request.base.sha` as the base and the pull request's merged tree as the
head, and runs the grader from the protected base copy:

```bash
python -m scripts.size_limits --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
```

A branch whose merge with current `dev` grows a unit, or whose own allowlist
file no longer matches that merged tree, fails there. See
[Delivery tiers](delivery-tiers.md) for which tier runs each check.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| A `subprocess.CalledProcessError` traceback naming `git merge-base` | The base ref is missing locally or shares no history with `HEAD`. | Run `git fetch origin dev`, or pass a `--base` that exists and shares history with the branch. |
| `<unit> breaks <rule> ... not on the base allowlist` for a unit the branch changed | The branch pushed a unit over a limit. | Split the unit below the limit. The allowlist only shrinks; an entry added on the branch has no effect. |
| The same refusal for a unit the branch never touched | The gate predates pull request 4770, or `scripts/ci_prepush` in the worktree is older than that change. | Merge `dev` into the branch once to take the gate change. |
| `tests/SIZE_ALLOWLIST.json differs from the head's measurement` | The branch shrank or split a listed unit and left its entry. | Run `python -m scripts.size_limits --write`, commit the allowlist and run the gate again. |
| The pre push gate passes but the pull request's size step fails | The merged tree with current `dev` grows a unit or changes the measurement. | Merge `dev` into the branch, fix the named unit or rewrite the allowlist, and push. |
| `ci_prepush: commit or set aside the tracked changes first; the gates grade HEAD.` | Tracked files are modified but not committed. | Commit them, then run the gate. |
| `ci_prepush: missing gate tool ruff; interpreter: <path>` | The interpreter running the gate has no ruff. | Install the project dependencies into that interpreter. |

## Proof

`test_plan_reads_the_allowlist_and_grader_at_the_merge_base` in
`tests/test_ci_prepush.py` moves the base branch's allowlist and grader after
the work branch is cut and asserts that the gate copies the merge base
versions. Its red run on the old gate:
[39241901786](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39241901786).
Before the change, 16 refusal events across 12 sessions on 19 October named
only units `dev` had removed from its allowlist that day.
