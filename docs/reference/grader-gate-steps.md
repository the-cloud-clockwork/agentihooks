---
title: Grader gate steps
parent: Reference
nav_order: 81
---

# Grader gate steps

The steps listed under [What each step runs](#what-each-step-runs), in the
lint job of the Tests workflow and in the Semgrep workflow, grade a pull
request with the scripts and config of a protected grader checkout. The head
supplies the code, fixtures and charts being graded. Its own ruff config and
Semgrep rules can add checks on top of the grader's, but cannot remove or
replace them, so a pull request cannot loosen one of these checks by editing
the check's own module or config. The one exception in the table is the guard
rule floors step, which still runs the head's copy on a base that lacks it.
[Pull request 11361](https://github.com/the-cloud-clockwork/agentihooks/pull/11361)
moved these steps onto the grader.
[Pull request 11387](https://github.com/the-cloud-clockwork/agentihooks/pull/11387)
removed the head copy fallback from the marker key vector step and the two
chart steps. Each of those three steps now runs only the grader's copy of its
script, and the head's copy never runs.
[Pull request 12025](https://github.com/the-cloud-clockwork/agentihooks/pull/12025)
made each chart step run the grader's copy over an empty tree first and refuse
unless that copy reports no chart, so a copy that ignores `--root` fails
closed instead of grading the base's own charts.

The other lint gate steps are outside the step table. Several of them look
for their module in the grader and run the head's copy when it is missing. The
[For loop word list gate](for-loop-word-list-gate.md) and the
[Tick pattern gate](tick-pattern-gate.md) run only the grader's copy and fail
when it is missing. The protected gate
step runs `scripts.gate_protected` from the grader, and runs the head's
`scripts.ci_runner_trust` when the grader has no `scripts/ci_runner_trust.py`.
[Protected gate step in lint](#protected-gate-step-in-lint) covers that step
and which revision it grades.

For a pull request, the Tests workflow runs every step on this page unless the
CI lab verdict covers the job, and each step feeds `Gate — Required`, the
check the dev ruleset requires. A red step blocks the pull request from the
merge queue. The Proofs workflow's `semgrep` proof runs the same Semgrep steps
outside that gate.

## The protected grader checkout

Each job fetches one revision and adds it as a detached worktree at
`$RUNNER_TEMP/grader`. The head stays checked out in `$GITHUB_WORKSPACE`.

{% raw %}
| Job | Revision | Source |
| --- | --- | --- |
| Tests `lint` | <code>GRADER: ${{ github.event.pull_request.base.sha &#124;&#124; needs.reuse.outputs.grader }}</code> | A pull request uses the base sha its event carries. A manual dispatch uses the revision the `reuse` job records in `Record the protected grader revision`, which is the `dev` tip that job checked out |
| Semgrep `scan` and `charts` | `BASE: ${{ inputs.base }}`, fetched as `${BASE#origin/}` | The caller's `base` input. The Tests workflow passes `origin/dev` for a pull request into `dev`, so the grader is the `dev` tip when the job runs. Otherwise it passes the pull request base sha, the merge group base sha, the push `before` sha or its own `base` input, in that order. The Proofs workflow's `semgrep` proof passes its `base` input |
{% endraw %}

The grader checkout step in both workflows:

```bash
git fetch --no-tags origin "$GRADER"            # lint job
git fetch --no-tags origin "${BASE#origin/}"    # Semgrep scan job; the charts job adds --depth=1
git worktree add --detach "$RUNNER_TEMP/grader" FETCH_HEAD
```

The Tests workflow skips its `lint`, `lint-actions` and `semgrep` jobs on
`merge_group` and `push` events. They run on pull requests and on admitted
manual dispatches. `lint` and `semgrep` are also skipped when the
[CI lab](ci-lab.md) already holds an authoritative verdict for them;
`lint-actions` holds the steps the lab cannot run, so no lab verdict skips it.

## What each step runs

| Workflow and job | Step | From the grader | From the head |
| --- | --- | --- | --- |
| Tests `lint-actions` | `Refuse marker key vector drift from kernel dev` | `python -m scripts.ci_marker_vectors`, run from `$RUNNER_TEMP/grader` | `tests/fixtures/marker-idempotency-vectors.json` under `--root "$GITHUB_WORKSPACE"` |
| Tests `lint-actions` | `Hold the static gates the released lab reads equal to the gates the head reads` | `python -m scripts.ci_lab.discovery_parity`, run from `$RUNNER_TEMP/grader` | The workflow, its registry and its exclusion catalog under `--head "$GITHUB_WORKSPACE"`, read once by the `scripts` folder of `main` and once by the head's own |
| Tests `lint` | `Lint check` | `ruff check` with the grader's `pyproject.toml` | The code. A second `ruff check` also applies the head's own config, so a rule the head adds holds too |
| Tests `lint` | `Format check` | `ruff format --check` with the grader's `pyproject.toml` | The code |
| Tests `lint` | `Check every pull request job is a need of Gate Required` | `python -m scripts.ci_wiring`, run from `$RUNNER_TEMP/grader` | The workflows under `--root "$GITHUB_WORKSPACE"` |
| Semgrep `scan` | `Own rules hold on their fixtures and the code` | The rules in the grader's `.semgrep` | The code, scanned with the head's `.semgrep` as well. The head rules' own fixture tests run first |
| Semgrep `scan` | `Outage rules for tests hold on every test file` | The grader's copy of each rule the step names in `RULES`, when the grader holds it | Every test file under `tests`, scanned with the head's copy of each rule as well. `.semgrepignore` skips `tests/`, so the step passes `--x-ignore-semgrepignore-files`, and any finding fails it |
| Semgrep `scan` | `New test waits keep a bound of ten seconds or more` | The grader's copy of the test wait bound rule `RULE` names, when the grader holds it | The test files under `tests` that changed since the merge base of the head and the base, scanned past `.semgrepignore` with the head's copy of the rule as well and compared with `--baseline-commit` against that merge base. A wait the merge base already holds passes; a new one fails it |
| Semgrep `scan` | `Fail on findings new against the base` | The rules in the grader's `.semgrep-incidents` | The code, compared against the merge base with the head's `.semgrep-incidents` and the `p/ci`, `p/secrets` and `p/python` registry rules |
| Semgrep `charts` | `Guard rules hold on their fixtures and at their computed floors` | `scripts/ci_semgrep_floors.py`, read from the fetched base with `git show`. This step keeps a fallback: when the base has no copy of the script, the head's copy runs | The guard rules and their fixtures |
| Semgrep `charts` | `A chart version bump leaves every pod template unchanged` | `tests/ci_charts/chart_pod_templates.py` | The charts under `deploy` in `--root "$GITHUB_WORKSPACE"` |
| Semgrep `charts` | `Charts render no CronJob outside the scheduler` | `tests/ci_charts/chart_timers.py` | The charts under `deploy` in `--root "$GITHUB_WORKSPACE"` |

The ruff steps run `python -P`, which keeps the current directory off the
import path. A head that ships its own `ruff` package therefore cannot stand in
for the installed one. Every lint gate step carries
{% raw %}`if: ${{ !cancelled() }}`{% endraw %}, so each one reports even after
an earlier step turns red.

The marker vector step compares the head's fixture byte for byte with
`tests/fixtures/marker-idempotency-vectors.json` from `agentibrain-kernel` on
`dev`. The kernel file is checked out at `.marker-kernel` in the step
`Check out kernel marker vector authority`, which runs before the marker step.

## Head copy fallback removed from the marker and chart steps

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
script to exit 0 and pass. Pull request 11387 removed the branch, and pull
request 12025 added the empty tree probe to the chart steps. The steps now
read:

```bash
# Refuse marker key vector drift from kernel dev
vectors="$GITHUB_WORKSPACE/.marker-kernel/tests/fixtures/marker-idempotency-vectors.json"
cd "$RUNNER_TEMP/grader"
python -m scripts.ci_marker_vectors --root "$GITHUB_WORKSPACE" "$vectors"

# A chart version bump leaves every pod template unchanged
script=tests/ci_charts/chart_pod_templates.py
empty="$(mktemp -d -p "$RUNNER_TEMP")"
if [[ "$(python "$RUNNER_TEMP/grader/$script" --root "$empty" 2>&1)" != *"No chart found under deploy"* ]]; then
  echo "::error::The protected grader's copy of $script does not grade the tree --root names, so nothing trusted can grade the head's charts."
  exit 1
fi
python "$RUNNER_TEMP/grader/$script" --root "$GITHUB_WORKSPACE"
```

`Charts render no CronJob outside the scheduler` runs the same lines with
`script=tests/ci_charts/chart_timers.py`.

What each step does depends on the grader revision:

| Grader revision | Marker vector step | Chart steps |
| --- | --- | --- |
| Carries `--root`, from pull request 11361 onwards | Grades the head's fixture | The probe prints `No chart found under deploy`, then the step grades the head's charts |
| Predates pull request 11361 | argparse refuses the option: `error: unrecognized arguments: --root ...`, exit 2, red | The old scripts have no argument parser, so the probe grades the grader's own charts. The step prints `::error::The protected grader's copy of tests/ci_charts/<script> does not grade the tree --root names, ...` and exits 1, red. The head's charts are not graded |
| Has no copy of the script | `No module named scripts.ci_marker_vectors`, exit 1, red | The probe gets `can't open file ...: No such file or directory`, and the step refuses with the same error, exit 1, red |

### Stale base on the chart steps

On a base older than pull request 11361, the grader's chart scripts ignore
`--root` and grade the base's own charts under `deploy`. Before pull request
12025 the chart steps passed on that verdict: with such a grader and a head
carrying a planted CronJob template, the timer check printed
`0 rendered CronJob objects and 0 failed renders across 11 renders of 1 charts.`
and exited 0.

Each chart step now runs the grader's copy over an empty temporary tree
before it grades the head. A copy that grades the tree `--root` names prints
`No chart found under deploy; the check graded nothing.` there. Any other
output means the copy graded another tree, rejected `--root` or did not run,
so the step refuses with exit 1 and the head's charts are not graded by it.

A pull request into `dev` always grades its charts with the current `dev` tip,
which carries `--root`. The refusal reaches a pull request into another
branch whose base predates 11361, and a dispatch that passes an older
`base`. The chart step tests run each step over two stand-in grader copies:
one that rejects `--root` through argparse, and one that ignores its
arguments, prints a clean summary and exits 0, the way the old scripts behave
on a clean base.

### Stale base on the marker vector step

A lint step that runs the grader's module grades with the base sha its pull
request event carries. On an older pull request the event sha can predate current `dev`. When it
predates pull request 11361, the marker step fails with exit 2 although the
head is correct. GitHub
[re-runs a workflow](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/re-running-workflows-and-jobs)
with the event of the original run, so a re-run keeps the old sha. Close and
reopen the pull request so a new event carries current `dev`, then let the
checks run again.

### Stale base on the guard rule floors step

`Guard rules hold on their fixtures and at their computed floors` reads
`scripts/ci_semgrep_floors.py` from the fetched base and runs the head's copy
when the base has none:

```bash
grader=scripts/ci_semgrep_floors.py
if git cat-file -e FETCH_HEAD:scripts/ci_semgrep_floors.py; then
  grader="$RUNNER_TEMP/ci_semgrep_floors.py"
  git show FETCH_HEAD:scripts/ci_semgrep_floors.py > "$grader"
fi
python "$grader" check --base FETCH_HEAD
```

The script arrived with
[pull request 7222](https://github.com/the-cloud-clockwork/agentihooks/pull/7222),
so only a Semgrep `base` older than that change runs the head's copy. On such
a base a head can edit its own copy of the floors check to pass. A pull
request into `dev` always fetches the `dev` tip, which carries the script.

## Protected gate step in lint

The Tests `lint` step `Require trusted actors before private runner admission`
runs `scripts.gate_protected` from the grader checkout. It grades the pull
request head commit against the head's merge base with the event base. It
does not grade the synthetic merge commit that the job checks out.
`Gate — Protected` grades the same head commit, with the base branch tip it
checks out as `--base`.
[Pull request 11515](https://github.com/the-cloud-clockwork/agentihooks/pull/11515)
made this change after
[issue 11510](https://github.com/the-cloud-clockwork/agentihooks/issues/11510)
showed the step blaming frozen changes that had already landed on `dev` on a
pull request behind `dev`.

{% raw %}
```yaml
- name: Require trusted actors before private runner admission
  if: ${{ !cancelled() }}
  env:
    HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: |
    if [[ -f "$RUNNER_TEMP/grader/scripts/ci_runner_trust.py" ]]; then
      cd "$RUNNER_TEMP/grader"
      python -m scripts.gate_protected --head "$HEAD_SHA"
    else
      python -m scripts.ci_runner_trust
    fi
```
{% endraw %}

The step passes no `--base` or `--root`. The script therefore uses the grader
worktree for both: `--base` is that worktree's `HEAD`, and git reads every
revision from the repository it shares with the job's checkout.

| Event | `--head` | `--base` | Merge base the gate grades from |
| --- | --- | --- | --- |
| Pull request | `github.event.pull_request.head.sha`, the head commit | The grader revision, `github.event.pull_request.base.sha`, which is the base sha the event carries | `git merge-base <event base> <head>` |
| Admitted manual dispatch | `github.sha`, the dispatched commit, because the event has no pull request | The grader revision, the `dev` tip that the `reuse` job records | `git merge-base <dev tip> <dispatched commit>` |

The lint job does not run on `merge_group` or `push` events. When the grader
has no `scripts/ci_runner_trust.py`, the step instead runs the head's
`scripts.ci_runner_trust` and skips the protected gate. Every grader on `dev`
carries the file.

`scripts.gate_protected` reads the base workflows from the merge base, and the
frozen gate registry `tests/registry/FROZEN_GATES.json` and the runner trust
policy from `--base`. It reads the head workflows and files from `--head`. A
frozen file counts as changed only when its blob at the head differs from its
blob at the merge base and from its blob at `--base`. The full option list is
in [Running the grader by hand](delivery-tiers.md#running-the-grader-by-hand).

### Why the step grades the head instead of the synthetic merge

On a pull request the lint job checks out `github.sha`, the merge commit that
GitHub builds from the head and the base branch. GitHub can build that commit
against a newer `dev` than the event base the grader checks out. Before pull
request 11515 the step ran `--head "$GITHUB_SHA"`. The merge base of the
event base and that merge commit is the event base itself, so every commit
`dev` landed after the event base counted as one of the pull request's
changes.

On 15 November 2026, `dev` merged a frozen registry announcement, pull request
11405 at `5868238ae4e0`, and then the frozen grader change it announced, pull
request 11404 at `c133027b0a`. Pull request 11341, opened before both merges,
changed only the registry, and its event base was `5868238ae4e0`. Its lint job
[132988934421](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/43763250585/job/132988934421),
in Tests run 43763250585 attempt 2, graded the merge commit `02bc2642ab85`
and failed:

```text
graded 02bc2642ab85 against merge base 5868238ae4e0: 1 protected gate problems
The head changes tests/registry/FROZEN_GATES.json and the frozen gate file scripts/gate_protected.py together; land the registry by its own pull request first.
```

The head `e4900bc2ed` and the event base held the same `scripts/gate_protected.py`
blob. The changed blob came from `dev`. Pull request 11335, also opened before
both merges, had merged a newer `dev` into its head. Its lint job
[132997128606](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/43774359032/job/132997128606),
on 15 November with the step text from before pull request 11515, graded the
merge commit `670672445e66` and found `0 protected gate problems`.

### Pull requests behind dev

The regression tests plant a repository whose `dev` landed a registry
announcement and the frozen file change it announces after the event base.
The current column comes from those tests, which run the real step text. The
column before pull request 11515 is derived from `_frozen_problems` on the
same planted commits. Tests run 43830625093, which ran the tests on the old
step text, is its observed evidence:

| The head changes | Step before pull request 11515 | Current step |
| --- | --- | --- |
| A file outside the registry | Red: `The head changes tests/registry/FROZEN_GATES.json and the frozen gate file gates/one.py together; ...` | Green: `0 protected gate problems` |
| Only the registry, announcing a blob for another frozen file | Red, with the same problem | Green: `0 protected gate problems` |
| A frozen file, to a blob the base registry does not accept | Red: `The head changes tests/registry/FROZEN_GATES.json and the frozen gate file <path> together; ...` twice, once for `gates/one.py`, which `dev` changed, and once for `gates/two.py` | Red: `The head changes the frozen gate file gates/two.py to content tests/registry/FROZEN_GATES.json on the base does not accept; announce its blob there by its own pull request first.` The problem names only the head's own file |

A frozen file change in the head is still refused. To change a frozen file,
announce its blob first, as
[Change the job](ci-lab.md#change-the-job) describes for the writer lock
workflow.

### Grade a pull request the way the lint step does

In the lint job log, the `Check out the protected grader` step prints its env
block, and its `GRADER:` value is the event base. `gh pr view <number> --json baseRefOid`
returns the current base instead, which differs from the event base once
`dev` moves. Then, from a clone of this repository:

```bash
grader="$(mktemp -d)/grader"
git fetch --no-tags origin <event base sha>
git worktree add --detach "$grader" FETCH_HEAD
git fetch --no-tags origin "refs/pull/<number>/head"
head="$(git rev-parse FETCH_HEAD)"
(cd "$grader" && python -m scripts.gate_protected --head "$head")
git worktree remove "$grader"
```

To see what the step graded before pull request 11515, fetch
`refs/pull/<number>/merge` instead of `refs/pull/<number>/head`. GitHub
rebuilds that ref when `dev` moves, so it can differ from the run's
`GITHUB_SHA`.

On 18 November 2026 the grader code at `5868238ae4e0` was run locally over
planted commits that repeat pull request 11341. One head adds a registry blob
on top of `5868238ae4e0`, and one merge commit, built with
`git merge-tree --write-tree` and `git commit-tree`, merges that head into
`c133027b0a`. The planted commits exist only in that local run, so their shas
differ on every rerun. For a real pull request, the snippet above is the
reproduction:

```text
$ python -m scripts.gate_protected --head <synthetic merge>
graded a5611c6ed966 against merge base 5868238ae4e0: 1 protected gate problems
::error::The head changes tests/registry/FROZEN_GATES.json and the frozen gate file scripts/gate_protected.py together; land the registry by its own pull request first.

$ python -m scripts.gate_protected --head <pull request head>
graded 1b55abb7a616 against merge base 5868238ae4e0: 0 protected gate problems
```

A planted head that edits `scripts/gate_protected.py` on top of
`5868238ae4e0` is still refused, with exit 1:

```text
graded c11f19e52f28 against merge base 5868238ae4e0: 1 protected gate problems
::error::The head changes the frozen gate file scripts/gate_protected.py to content tests/registry/FROZEN_GATES.json on the base does not accept; announce its blob there by its own pull request first.
```

### Guards

| Guard | What it holds |
| --- | --- |
| `tests/test_gate_protected.py::test_the_protected_lint_step_passes_a_pull_request_behind_landed_frozen_changes` | Two cases, `unrelated` and `registry`. A head behind `dev` that changes an unrelated file or only the registry grades `0 protected gate problems` against the event base, exit 0 |
| `tests/test_gate_protected.py::test_the_protected_lint_step_still_refuses_a_frozen_edit_behind_dev` | A head behind `dev` that edits a frozen file exits 1 with the unaccepted content problem for that file, and the output never names the file `dev` changed |
| `tests/test_gate_protected.py::test_the_protected_lint_step_grades_the_run_revision_outside_a_pull_request` | With no pull request head in the event, the step grades `github.sha` |
| Feature map entry `protected-lint-grades-the-pull-request-head` | Origin `regression`. Lists the three tests above. Its seam is the lint step run text that invokes `scripts.gate_protected` |
| Semgrep incident rule `protected-gate-grades-synthetic-merge` in `.semgrep-incidents/` | Refuses a workflow line that runs `scripts.gate_protected` with `--head` set to `$GITHUB_SHA`, `${GITHUB_SHA...}` or {% raw %}`${{ github.sha }}`{% endraw %} |

The tests read the step from `.github/workflows/test.yml` and run its `run`
text with `bash --noprofile --norc -eo pipefail`, the shell GitHub uses. They
resolve the step's `env` with `scripts.ci_lab.expressions.interpolate` and
run from a grader worktree at the event base. The grader imports the
checkout's own gate code. On the old step text, at `f223d38af`, Tests run
43830625093 attempt 2, job
[133185537748](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/43830625093/job/133185537748),
failed both behind `dev` cases and blamed `gates/one.py` in the frozen edit
case. The case with no pull request head passed on the old step text. On the
fixed step, at `1b9b25c460`, Tests run 43835878743, job
[133193737010](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/43835878743/job/133193737010),
passed all four cases.

The Semgrep `scan` step `Fail on findings new against the base` runs the
incident rules from the grader and the head, and fails only on findings new
against the merge base. The `feature-tests` step
`Outage rules fire on their planted faults and pass their clean cases` checks
the rule against
`.semgrep-incidents/protected-gate-grades-synthetic-merge.test.yaml`, which
holds four planted forms and two clean ones. A refused line and the accepted
form:

{% raw %}
```yaml
# Refused
run: python -m scripts.gate_protected --head "${{ github.sha }}"

# Accepted
env:
  HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
run: python -m scripts.gate_protected --head "$HEAD_SHA"
```
{% endraw %}

### Limitations

- The trusted base is still the event base. A re-run keeps the head and base
  sha of its original event. `dev` commits that the head itself contains,
  merged in after the event base, count as the head's changes.
- Only this step's `--head` changed. The other lint steps still grade the
  checked out merge commit.
- The Semgrep rule matches `--head` only on the same line as
  `scripts.gate_protected`. A run sha passed through another variable, such as
  `head="$GITHUB_SHA"` followed by `--head "$head"`, is not caught.

## Run a step locally

These commands grade a head checkout with a `dev` grader, the same way the
workflow does. They need a clone of this repository with `origin` reachable.
The chart checks also need `helm` and PyYAML.

```bash
head="$PWD"
kernel="${KERNEL_DIR:?set KERNEL_DIR to a checkout of agentibrain-kernel on dev}"
grader="$(mktemp -d)/grader"
git fetch --no-tags origin dev
git worktree add --detach "$grader" FETCH_HEAD

# Marker vectors
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
| `::error::The protected grader's copy of tests/ci_charts/<script> does not grade the tree --root names, ...`, exit 1 | The Semgrep `base` predates pull request 11361, or its grader has no copy of the script. See [Stale base on the chart steps](#stale-base-on-the-chart-steps) | Run again with a base that carries `--root`. A pull request into `dev` always grades with the `dev` tip |
| `The head changes tests/registry/FROZEN_GATES.json and the frozen gate file <path> together; ...` from `Require trusted actors before private runner admission`, on a pull request whose own commits leave `<path>` unchanged | The step graded a revision that carries `dev`'s frozen changes: a run of the step text from before pull request 11515, or a head that merged in `dev` commits newer than the event base. See [Why the step grades the head instead of the synthetic merge](#why-the-step-grades-the-head-instead-of-the-synthetic-merge) | Compare the sha on the `graded` line with the pull request head sha. Run `git diff --name-only <merge base> <head> -- tests/registry/FROZEN_GATES.json <path>` with the two shas from that line to list what the graded revision changed |
| `The head drops the frozen gate file <path> from tests/registry/FROZEN_GATES.json; a frozen file stays frozen.` from the same step | The head's registry no longer lists a file the event base registry freezes | Restore the entry in `tests/registry/FROZEN_GATES.json`. A frozen file stays frozen |
| `The head changes the frozen gate file <path> to content tests/registry/FROZEN_GATES.json on the base does not accept; ...` from the same step | The head changes a frozen file to a blob the event base registry does not list | Land a pull request that adds the blob to the registry first. See [Change the job](ci-lab.md#change-the-job) |
| Semgrep reports `protected-gate-grades-synthetic-merge` | A new workflow line runs `scripts.gate_protected` with `--head` set to the run sha | Pass the pull request head instead, through {% raw %}<code>HEAD_SHA: ${{ github.event.pull_request.head.sha &#124;&#124; github.sha }}</code>{% endraw %}, as in [Protected gate step in lint](#protected-gate-step-in-lint) |

## Tests and feature map

The behaviour is pinned by four feature map entries in `feature-map.json`,
with tests in `tests/test_ci_grader_gates.py` and, for the protected gate
step, `tests/test_gate_protected.py`:

| Feature | What its tests hold |
| --- | --- |
| `grader-gates-fail-closed-on-stale-base` | A stale grader copy that refuses `--root` fails the marker step with exit 2 and each chart step with the refusal, exit 1. A grader without the for loop gate fails that step with exit 1. A planted head copy that exits 0 never runs |
| `chart-checks-refuse-a-grader-that-ignores-root` | A grader copy that ignores its arguments, prints a clean summary and exits 0 makes each chart step refuse with exit 1, and its summary never reaches the step output |
| `lint-gates-grade-from-grader` | A head that drops a ruff rule, ships a shadow `ruff` package, changes the formatter config, or rewrites the wiring, marker, for loop or chart check to pass itself stays red. The marker check reads the fixture under its `--root` and runs after the grader checkout. Each Semgrep job checks out the caller's `base` as the grader and scans with both rule sets |
| `protected-lint-grades-the-pull-request-head` | The protected gate step grades the pull request head, or `github.sha` outside a pull request, so frozen changes that landed on `dev` after the event base are never blamed on a pull request behind `dev`, and a frozen file edit in the head is still refused. See [Guards](#guards) |

`tests/context/test_marker_key_vectors.py::test_vector_check_is_on_the_required_tests_path`
pins the marker step's run text, its kernel checkout and its order in the
`lint-actions` job. No test covers the guard rule floors step's fallback to the head copy.
`tests/ci_lab/test_discovery_parity.py::test_the_lint_actions_job_reads_the_head_with_the_scripts_of_main_after_the_base_and_grader_checkouts`
pins the run text of the released lab step and its place after the base and grader checkouts.

## Related pages

- [Docs sanity check](docs-sanity.md): the lint job's public files scan, which
  still runs the head's copy while the base lacks the scanner.
- [Tick pattern gate](tick-pattern-gate.md): a lint job gate that fails when
  `dev` carries no grader.
- [Delivery tiers](delivery-tiers.md): which checks run on pull requests, in
  the merge queue and at promotion.
