---
title: Grader gate steps
parent: Reference
nav_order: 81
---

# Grader gate steps

The lint job of the Tests workflow and the Semgrep workflow grade a pull
request with the scripts and config of a protected grader checkout. They read
the head only as data, so a pull request cannot loosen a check by editing that
check's own module or config.
[Pull request 11361](https://github.com/the-cloud-clockwork/agentihooks/pull/11361)
moved these steps onto the grader.
[Pull request 11387](https://github.com/the-cloud-clockwork/agentihooks/pull/11387)
removed the head copy fallback from the marker key vector step and the two
chart steps. Each of those three steps now runs only the grader's copy of its
script, and the head's copy never runs.

Every step on this page feeds `Gate — Required`, the check the dev ruleset
requires. A red step blocks the pull request from the merge queue.

## The protected grader checkout

Each job fetches one revision and adds it as a detached worktree at
`$RUNNER_TEMP/grader`. The head stays checked out in `$GITHUB_WORKSPACE`.

| Job | Revision | Source |
| --- | --- | --- |
| Tests `lint` | `GRADER: ${{ github.event.pull_request.base.sha \|\| needs.reuse.outputs.grader }}` | A pull request uses the base sha its event carries. A manual dispatch uses the revision the `reuse` job records in `Record the protected grader revision`, which is the `dev` tip that job checked out |
| Semgrep `scan` and `charts` | `BASE: ${{ inputs.base }}`, fetched as `${BASE#origin/}` | The caller's `base` input. The Tests workflow passes `origin/dev` for a pull request into `dev`, so the grader is the `dev` tip when the job runs. Otherwise it passes the pull request base sha, the merge group base sha, the push `before` sha or its own `base` input, in that order. The Proofs workflow's `semgrep` proof passes its `base` input |

The grader checkout step in both workflows:

```bash
git fetch --no-tags origin "$GRADER"            # lint job
git fetch --no-tags origin "${BASE#origin/}"    # Semgrep scan job; the charts job adds --depth=1
git worktree add --detach "$RUNNER_TEMP/grader" FETCH_HEAD
```

The Tests workflow skips its `lint` and `semgrep` jobs on `merge_group` and
`push` events. Both jobs run on pull requests and on admitted manual
dispatches. Either job is also skipped when the [CI lab](ci-lab.md) already
holds an authoritative verdict for it.

## What each step runs

| Workflow and job | Step | From the grader | From the head |
| --- | --- | --- | --- |
| Tests `lint` | `Refuse marker key vector drift from kernel dev` | `python -m scripts.ci_marker_vectors`, run from `$RUNNER_TEMP/grader` | `tests/fixtures/marker-idempotency-vectors.json` under `--root "$GITHUB_WORKSPACE"` |
| Tests `lint` | `Lint check` | `ruff check` with the grader's `pyproject.toml` | The code. A second `ruff check` also applies the head's own config, so a rule the head adds holds too |
| Tests `lint` | `Format check` | `ruff format --check` with the grader's `pyproject.toml` | The code |
| Tests `lint` | `Check every pull request job is a need of Gate Required` | `python -m scripts.ci_wiring`, run from `$RUNNER_TEMP/grader` | The workflows under `--root "$GITHUB_WORKSPACE"` |
| Semgrep `scan` | `Own rules hold on their fixtures and the code` | The rules in the grader's `.semgrep` | The code, scanned with the head's `.semgrep` as well. The head rules' own fixture tests run first |
| Semgrep `scan` | `Fail on findings new against the base` | The rules in the grader's `.semgrep-incidents` | The code, compared against the merge base with the head's `.semgrep-incidents` and the `p/ci`, `p/secrets` and `p/python` registry rules |
| Semgrep `charts` | `A chart version bump leaves every pod template unchanged` | `tests/ci_charts/chart_pod_templates.py` | The charts under `deploy` in `--root "$GITHUB_WORKSPACE"` |
| Semgrep `charts` | `Charts render no CronJob outside the scheduler` | `tests/ci_charts/chart_timers.py` | The charts under `deploy` in `--root "$GITHUB_WORKSPACE"` |

The ruff steps run `python -P`, which keeps the current directory off the
import path. A head that ships its own `ruff` package therefore cannot stand in
for the installed one. Every lint step carries `if: ${{ !cancelled() }}`, so
each one reports even after an earlier step turns red.

The marker vector step compares the head's fixture byte for byte with
`tests/fixtures/marker-idempotency-vectors.json` from `agentibrain-kernel` on
`dev`. The kernel file is checked out at `.marker-kernel` in the step
`Check out kernel marker vector authority`, which runs before the marker step.

## No head copy fallback

Pull request 11361 guarded the marker step and both chart steps with a
fallback. When the grader's copy of the script did not yet carry `--root`, the
step ran the head's own copy:

```bash
if grep -q -- '--root' "$RUNNER_TEMP/grader/scripts/ci_marker_vectors.py"; then
  cd "$RUNNER_TEMP/grader"
  python -m scripts.ci_marker_vectors --root "$GITHUB_WORKSPACE" "$vectors"
else
  python -m scripts.ci_marker_vectors "$vectors"
fi
```

The chart steps used the same shape and tested for `tree(argv)` in the
grader's script. In that `else` branch a head could edit its own copy of the
script to exit 0 and pass. Pull request 11387 removed the branch. The steps
now read:

```bash
# Refuse marker key vector drift from kernel dev
vectors="$GITHUB_WORKSPACE/.marker-kernel/tests/fixtures/marker-idempotency-vectors.json"
cd "$RUNNER_TEMP/grader"
python -m scripts.ci_marker_vectors --root "$GITHUB_WORKSPACE" "$vectors"

# A chart version bump leaves every pod template unchanged
script=tests/ci_charts/chart_pod_templates.py
python "$RUNNER_TEMP/grader/$script" --root "$GITHUB_WORKSPACE"

# Charts render no CronJob outside the scheduler
script=tests/ci_charts/chart_timers.py
python "$RUNNER_TEMP/grader/$script" --root "$GITHUB_WORKSPACE"
```

What each step does depends on the grader revision:

| Grader revision | Marker vector step | Chart steps |
| --- | --- | --- |
| Carries `--root`, from pull request 11361 onwards | Grades the head's fixture | Grade the head's charts |
| Predates pull request 11361 | argparse refuses the option: `error: unrecognized arguments: --root ...`, exit 2, red | The old scripts have no argument parser. They ignore `--root` and grade the grader's own charts under `deploy`, then exit 0 when those charts are clean. The head's charts are not graded |
| Has no copy of the script | `No module named scripts.ci_marker_vectors`, exit 1, red | `can't open file ...: No such file or directory`, exit 2, red |

The head's copy runs in no row.

### Stale base limit for the chart steps

On a base older than pull request 11361, the chart steps pass on the base's
own charts. They do not fail closed. With such a grader and a head carrying a
planted CronJob template, the timer check printed
`0 rendered CronJob objects and 0 failed renders across 11 renders of 1 charts.`
and exited 0. The same head under the `dev` grader printed the CronJob and
exited 1.

The limit reaches only runs whose Semgrep `base` predates pull request 11361.
A pull request into `dev` always grades its charts with the current `dev` tip,
which carries `--root`. The limit applies to a pull request into another
branch whose base predates 11361, and to a dispatch that passes an older
`base`. The stale base tests for the chart steps use a stand-in grader script
that parses arguments, so they pin the argparse refusal and do not exercise
the old scripts. A follow up to fail the chart steps when the grader copy lacks
`--root` is open.

### Stale base on the marker vector step

The lint job grades with the base sha its pull request event carries. On an
older pull request that sha can predate current `dev`. When it predates pull
request 11361, the marker step fails with exit 2 although the head is correct.
Close and reopen the pull request so its next event carries current `dev`,
then let the checks run again.

## Run a step locally

These commands grade a head checkout with a `dev` grader, the same way the
workflow does. They need a clone of this repository with `origin` reachable.
The chart checks also need `helm` and PyYAML.

```bash
head="$PWD"
grader="$(mktemp -d)/grader"
git fetch --no-tags origin dev
git worktree add --detach "$grader" FETCH_HEAD

# Marker vectors: $kernel is a checkout of agentibrain-kernel on dev
(cd "$grader" && python -m scripts.ci_marker_vectors --root "$head" \
  "$kernel/tests/fixtures/marker-idempotency-vectors.json")

# Charts
python "$grader/tests/ci_charts/chart_pod_templates.py" --root "$head"
python "$grader/tests/ci_charts/chart_timers.py" --root "$head"

git worktree remove "$grader"
```

A passing run prints:

```text
Marker key vectors match kernel dev.
0 pod templates change with the chart version, 41 compared, 0 failed renders.
0 rendered CronJob objects and 0 failed renders across 11 renders of 1 charts.
```

The counts follow the charts and values files under `deploy`.

## Options

| Script | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `scripts.ci_marker_vectors` | `kernel_vectors` | required | Path to the kernel's `marker-idempotency-vectors.json` |
| `scripts.ci_marker_vectors` | `--root` | The checkout holding the script | Tree whose `tests/fixtures/marker-idempotency-vectors.json` is compared |
| `chart_pod_templates.py`, `chart_timers.py` | `--root` | The checkout holding the script | Tree whose `deploy` charts are rendered |

No environment variable changes these steps. The grader revision follows the
event, as described under [The protected grader checkout](#the-protected-grader-checkout).

| Script | Exit 0 | Exit 1 |
| --- | --- | --- |
| `scripts.ci_marker_vectors` | The fixture matches the kernel file | The bytes differ, or the fixture is missing |
| `chart_timers.py` | No chart renders a CronJob and every render succeeds | No chart under `deploy`, a CronJob renders, or a render fails |
| `chart_pod_templates.py` | No pod template changes with the chart version | No chart under `deploy`, a pod template changes, a render fails, or no pod template was compared |

Exit 2 comes from Python or argparse: an unknown option or a script file that
does not exist.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ci_marker_vectors.py: error: unrecognized arguments: --root ...`, exit 2 | The lint grader is a base sha older than pull request 11361 | Close and reopen the pull request so its event carries current `dev` |
| `::error::Marker key vectors differ from kernel dev.` | The head's `tests/fixtures/marker-idempotency-vectors.json` differs from the kernel file on `dev` | Copy the kernel's `dev` file into the fixture, or land the kernel change first |
| `FileNotFoundError` naming `tests/fixtures/marker-idempotency-vectors.json` | The head removed or renamed the fixture | Restore the fixture at that path |
| `No chart found under deploy; the check graded nothing.` | `--root` holds no `Chart.yaml` under `deploy` | Restore the charts, or point `--root` at the repository root |
| `<chart> with <values>: CronJob/<name>` | A chart renders a CronJob | Register the job with `scripts.scheduler` and remove the CronJob template |
| `<chart> with <values>: <Kind>/<name>` and `A pod template carries the chart version` | A pod template changes when only the chart version changes | Keep `helm.sh/chart` on object metadata, off the pod template |
| `<chart> with <values> failed to render: ...` | `helm template` failed | Fix the template or values file it names |
| An edit to the head's copy of one of these scripts changes nothing in the run | Only the grader's copy runs | Expected. The edit grades later pull requests once it merges into `dev` |
| A chart step is green on a head with a broken chart | The Semgrep `base` predates pull request 11361 | See [Stale base limit for the chart steps](#stale-base-limit-for-the-chart-steps); run again with a base that carries `--root` |

## Tests and feature map

The behaviour is pinned by two feature map entries in `feature-map.json`,
with tests in `tests/test_ci_grader_gates.py`:

| Feature | What its tests hold |
| --- | --- |
| `grader-gates-fail-closed-on-stale-base` | A stale grader copy that refuses `--root` fails the marker step and each chart step with exit 2, and a planted head copy that exits 0 never runs |
| `lint-gates-grade-from-grader` | A head that drops a ruff rule, ships a shadow `ruff` package, changes the formatter config, or rewrites the wiring, marker or chart check to pass itself stays red. The marker check reads the fixture under its `--root` and runs after the grader checkout. Each Semgrep job checks out the caller's `base` as the grader and scans with both rule sets |

`tests/context/test_marker_key_vectors.py::test_vector_check_is_on_the_required_tests_path`
pins the marker step's run text, its kernel checkout and its order in the lint
job.

## Related pages

- [Docs sanity check](docs-sanity.md): the lint job's public files scan, which
  still runs the head's copy while the base lacks the scanner.
- [Tick pattern gate](tick-pattern-gate.md): a lint job gate that fails when
  `dev` carries no grader.
- [Delivery tiers](delivery-tiers.md): which checks run on pull requests, in
  the merge queue and at promotion.
