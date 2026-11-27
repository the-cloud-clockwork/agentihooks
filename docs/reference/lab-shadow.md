---
title: CI lab shadow acceptance report
parent: Reference
nav_order: 91
---

# CI lab shadow acceptance report
{: .no_toc }

A gate moves from GitHub Actions to the CI lab only after the lab has graded the same heads in
shadow and agreed with Actions. The shadow acceptance report measures that agreement for one
gate from live GitHub data, prints it, and exits 0 only when the acceptance rule holds. A flip
pull request carries the pasted report.

The report reads GitHub, not the lab. The shadow recorder that keeps verdict rows in the lab
Redis, its gate mapping and its own `--since` report are documented in
[CI lab service contract, Shadow comparison](ci-lab.md#shadow-comparison).

## Command

```bash
python -m scripts.ci_lab shadow report --gate <gate> [--heads 50] [--event merge_group] [--app <slug>]
```

| Option | Default | Meaning |
|---|---|---|
| `--gate` | required | The gate id: `tests`, `mutation`, or a static gate id. On dev the static ids are `lint`, `type-gate`, `semgrep/scan`, `semgrep/feature-tests`, `semgrep/charts` and `test-count` |
| `--heads` | `50` | How many heads the window holds; at least 1 |
| `--event` | `pull_request` | Read pull request heads, or merge group heads with `merge_group` |
| `--app` | `AGENTIHOOKS_LAB_APP` | The slug of the lab's GitHub App; the command exits 2 when neither names one |

The repository comes from `GITHUB_REPOSITORY`, or `the-cloud-clockwork/agentihooks` when it is
unset. Every GitHub call goes through the rate limited helper `scripts.ci_budget.rate_limit.gh_api`,
so run it where `gh` holds a fresh token (on a hive, through `python -m scripts.github_app_cli exec`).

`shadow report --since <revision>` without `--gate` is the shadow recorder's own report of the
rows in the lab Redis. It is a separate command, `scripts.ci_lab.shadow.main`. Any `--gate` or
`--gate=<id>` after `shadow report` selects the acceptance report, so the two cannot be combined:
`--since` beside `--gate` exits 2 as an unrecognized argument.

## Configuration

| Setting | Where it is read | Meaning |
|---|---|---|
| `--app` or `AGENTIHOOKS_LAB_APP` | The flag, else the environment | The slug of the lab App whose `lab-shadow/<gate>` check runs count |
| `GITHUB_REPOSITORY` | The environment | The `owner/name` repository whose runs and check runs the report reads; unset or empty reads `the-cloud-clockwork/agentihooks` |
| GitHub token | The `gh` login, or `GH_TOKEN` | Read access to Actions runs and commit check runs. `gh api` makes every call. On a hive, wrap the command in `python -m scripts.github_app_cli exec --`, which refreshes the GitHub App credentials and runs the command with them in `GH_TOKEN` |
| Workflow tree | The checkout that holds the `scripts` package | `scripts.ci_lab.shadow.static_jobs` reads `.github/workflows/test.yml` and its reusable workflows there to learn the static gate ids. Run the report from a checkout of current dev, so a static gate added on dev maps its Actions jobs |

Set the lab App slug to the value of the repository Actions variable `AGENTIHOOKS_LAB_APP`. The
Tests workflow passes that variable to `scripts.ci_lab_verdict` when it waits for lab verdicts, so
the report and the workflow trust the same App.

`python -m scripts.ci_lab.static_gates [--root <checkout>]` prints one JSON line per static step of
the checkout's workflow tree, the current directory unless `--root` names one; its `job` field is
the gate id that `--gate` takes. It also reads the checkout's `.github/input-keys.json`.

## Examples

Check a gate before its flip pull request, over the newest 50 pull request heads:

```bash
export AGENTIHOOKS_LAB_APP=<lab App slug>
python -m scripts.ci_lab shadow report --gate lint
```

The same check on a hive, where `gh` needs the fresh App token:

```bash
python -m scripts.github_app_cli exec -- python -m scripts.ci_lab shadow report --gate lint --app "$AGENTIHOOKS_LAB_APP"
```

Judge the unit and mutation gates on merge group heads, the runs the merge queue grades:

```bash
python -m scripts.ci_lab shadow report --gate tests --event merge_group
python -m scripts.ci_lab shadow report --gate mutation --event merge_group
```

Look at the newest three heads only, a quick check that the lab posts verdicts at all:

```bash
python -m scripts.ci_lab shadow report --gate type-gate --heads 3
```

A small window is strict: see [Windows other than 50](#windows-other-than-50).

Branch on the result in a script. Exit 1 means `rule fails` only when the report printed its
table; an exit 1 without a table is a GitHub error (see [Troubleshooting](#troubleshooting)).
`tee` keeps the table on the terminal and in `lint-report.txt` in the current directory:

```bash
set -o pipefail
if python -m scripts.ci_lab shadow report --gate lint | tee lint-report.txt; then
  echo "lint holds the acceptance rule"
elif [[ "$(tail -n 1 lint-report.txt)" == "rule fails" ]]; then
  echo "lint stays on GitHub Actions"
else
  echo "no report; read standard error"
fi
```

For a flip pull request, paste the command, its exit code and the whole standard output in a
`text` fence in the pull request body.

## Which heads it reads

The report lists completed runs of the Tests workflow (`test.yml`) for the event, newest first,
and takes each distinct head once. For each head it reads every check run on the commit with
`filter=latest`, so a job attempt that was rerun counts by its newest attempt.

- **Actions verdict.** Every `github-actions` check run that `scripts.ci_lab.shadow.gate_of` maps
  to the gate, the same mapping the shadow recorder uses. The verdict is `fail` when any of them
  concluded `failure` or `timed_out`, `pass` when all graded ones succeeded. `skipped` and other
  conclusions grade nothing. It completed when the last graded job completed.
- **Lab verdict.** The newest `lab-shadow/<gate>` check run posted by the lab App, the same trust
  the shadow recorder applies; a check run of that name from any other App, a workflow token
  included, is ignored.
  `success` is `pass`, `failure` and `timed_out` are `fail`. Any other conclusion, a run still in
  progress or no run at all leaves the lab verdict missing. The report reads only the newest run
  by id: when it was cancelled or is still running, an earlier verdict of the same name does not
  count, where the shadow recorder keeps that earlier verdict.
- **Start of the wait.** The `started_at` of the newest `github-actions` check run named
  `lab-verdict` on the head, the job of the Tests workflow that waits for the lab's verdicts. A
  head with no such check run, or whose newest one has not started, holds no start. Merge group
  heads hold none: the job runs on pull requests only. The start comes from the same check run
  listing, so it costs no extra GitHub call.

A head leaves the window, and the next older head takes its place, when any Actions job of the
gate was cancelled, is still running, or none of them graded. A unit job that the workflow reran
after a lost runner (`unit (3.12, 1)` beside `unit rerun (3.12, 1)`) counts only by its rerun, so a
lost runner retry is never a mismatch. The Tests workflow creates `unit rerun` jobs only on merge
group runs, and only for the shards its `unit-lost` job names through `scripts.ci_unit_rerun` as
having lost their runner. When any unit shard failed without a lost runner signature,
`scripts.ci_unit_rerun` reruns none, so a rerun never hides a shard that failed on a live runner.

The report reads at most 10 pages of 100 Tests runs, the newest 1000, and at most 10 pages of 100
check runs per head. It stops listing once the window is full, so a 50 head report makes at least
51 GitHub API calls: one per page of runs and at least one per head it reads, including heads that
leave the window.

## The acceptance rule

Over the window of the newest `--heads` heads that hold an Actions verdict:

| Count | Holds when |
|---|---|
| Heads | the window is full, `--heads` heads were found |
| Mismatches | zero heads where both verdicts exist and disagree |
| Lab verdict present | at least 48 of every 50 heads |
| Lab verdict first | at least 45 of every 50 heads have a lab verdict that completed before the Actions verdict |
| Lab verdict inside the wait | pull request event only: at least 48 of every 50 heads have a lab verdict that completed no later than 1200 seconds after the head's `lab-verdict` job started |

Phase 4 of the grading lab plan, shadow and cutover, sets this rule for every gate flip, printed
with `--heads 50` and pasted in the flip pull request. It words the
window as heads "with both verdicts"; the report reads it as heads with an
Actions verdict, because a window of heads that all carry a lab verdict would make the presence
count meaningless. A head without a lab verdict stays in the window and counts against presence.

A lab verdict that completes at the same second as the last Actions job is late.

First is not enough for a flip. Once a gate is lab authoritative, the `lab-verdict` job of a pull
request waits 1200 seconds for the lab, `WAIT_S` of `scripts/ci_lab_verdict.py`, and a verdict
still missing then fails `Gate — Required`. While Actions jobs wait a long time for a runner, a lab
verdict can be first and still land after that wait. The fourth count measures the wait itself:
the seconds from the start of the head's `lab-verdict` job to the completion of its lab check run.
A verdict at exactly 1200 seconds is inside, one second later is not, and a verdict that completed
before the job started is inside. The job starts a few seconds before its wait does, by its setup
steps, so the count is the stricter of the two. A head with no lab verdict or no started
`lab-verdict` job is not inside. A head whose Tests run was rerun holds a newer job, so its start
moves later.

The merge group event has no such job, so its rule keeps the first three counts.

### Windows other than 50

For a window other than 50 the thresholds scale and round up, `scripts.ci_lab.shadow_report.rule`:

| `--heads` | Lab verdict present | Lab verdict first | Lab verdict inside the wait, pull request only |
|---|---|---|---|
| 100 | 96 | 90 | 96 |
| 50 | 48 | 45 | 48 |
| 25 | 24 | 23 | 24 |
| 20 | 20 | 18 | 20 |
| 10 | 10 | 9 | 10 |
| 3 | 3 | 3 | 3 |

Below 25 heads every head needs a lab verdict, on a pull request window inside the wait, and
below 10 every lab verdict must also come first. Phase 4 of the grading lab plan uses the default 50.

## Output

A live run at dev `3be36adff` of
`python -m scripts.ci_lab shadow report --gate tests --heads 3 --event merge_group --app <lab App slug>`.
The merge group heads carried no `lab-shadow/tests` check runs, so it exits 1:

```text
gate tests event merge_group
head          actions  lab      order  agreement
3be36adfffa2  pass     missing
12e502152123  pass     missing
f17da458dda5  pass     missing
heads 3 of 3
lab verdict present 0 of at least 3
mismatches 0 of at most 0
lab verdict first 0 of at least 3
rule fails
```

Each row is one head: the first twelve characters of its sha, the Actions verdict, the lab
verdict or `missing`, `first` or `late`, and `match` or `mismatch`. A head without a lab verdict
leaves the last two columns empty. The last line is `rule holds` with exit code 0, or `rule fails`
with exit code 1. An argument error exits 2.

A pull request report adds a `seconds` column, the seconds from the start of the head's
`lab-verdict` job to its lab verdict, rounded up and empty when the head lacks either, and the line
`lab verdict inside the wait <n> of at least <m>` after the `lab verdict first` line. A merge group report
keeps the five columns and three counts above.

### Reading a table with lab verdicts

Four illustrative heads with made up shas, rendered by `scripts.ci_lab.shadow_report.render`:

```text
gate tests event pull_request
head          actions  lab      order  agreement  seconds
1a2b3c4d5e6f  pass     pass     first  match      412
2b3c4d5e6f7a  fail     fail     late   match      1530
3c4d5e6f7a8b  pass     fail     first  mismatch   -95
4d5e6f7a8b9c  pass     missing
heads 4 of 4
lab verdict present 3 of at least 4
mismatches 1 of at most 0
lab verdict first 2 of at least 4
lab verdict inside the wait 2 of at least 4
rule fails
```

| Head | Why its row reads that way |
|---|---|
| `1a2b3c4d5e6f` | Both sides passed and the lab check run completed before the last Actions job of the gate, 412 seconds after the `lab-verdict` job started: inside the wait |
| `2b3c4d5e6f7a` | Both sides failed; the lab check run completed in the same second as the last Actions job, which is late, and 1530 seconds after the `lab-verdict` job started: outside the wait |
| `3c4d5e6f7a8b` | The lab failed a head Actions passed: one mismatch, which alone fails the rule. Its lab verdict completed 95 seconds before the `lab-verdict` job started, which is inside the wait |
| `4d5e6f7a8b9c` | Actions graded the head and the lab posted no verdict; it stays in the window and counts against presence, against first and against the wait |

The counts then fail four ways: 3 lab verdicts against the 4 a window of 4 needs, 1 mismatch
against 0, 2 first verdicts against 4, and 2 verdicts inside the wait against 4.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Every row shows `missing` | The lab is not posting `lab-shadow/<gate>` check runs on these heads | Check the lab scheduler and worker are live and grading in shadow |
| One head shows `missing` although it has a `lab-shadow/<gate>` check run | The run came from another App or a workflow token, or the newest run of that name is still in progress or concluded `cancelled`, `skipped` or `neutral` | `gh api 'repos/<repo>/commits/<sha>/check-runs?check_name=lab-shadow/<gate>'` shows each run's `app.slug`, `status` and `conclusion` |
| `lab verdict inside the wait` below its threshold while `lab verdict first` holds | Lab verdicts land more than 1200 seconds after the `lab-verdict` job starts, while Actions jobs wait longer still for a runner | Read the `seconds` column for the slow heads, then the lab's queue wait and worker capacity. The gate stays on GitHub Actions until the count holds |
| A row has no `seconds` although it has a lab verdict | The head holds no started `lab-verdict` job from `github-actions`: the job was skipped, has not started, or the head is older than the job | `gh api 'repos/<repo>/commits/<sha>/check-runs?check_name=lab-verdict'` shows its `status` and `started_at` |
| `heads N of 50` below 50 | Too few completed Tests runs with a graded job of the gate in the newest 1000 runs | Use a smaller `--heads`, or wait for more heads |
| `heads 0 of 50` and `rule fails`, exit 1 | An unknown or mistyped gate id: `gate_of` maps no Actions job to it, so every head leaves the window. A wrong id is no argument error, and the report reads every distinct head of the newest 1000 runs before it prints: 10 pages of runs plus at least one API call per distinct head | Check the id against the `job` field of `python -m scripts.ci_lab.static_gates` and `scripts.ci_lab.shadow.gate_of` before running; run from a checkout of current dev |
| `error: --app or AGENTIHOOKS_LAB_APP names the lab App`, exit 2 | Neither the flag nor the variable names the lab App | Pass `--app <lab App slug>` or export `AGENTIHOOKS_LAB_APP` |
| `error: --heads must be at least 1`, exit 2 | `--heads` is 0 or negative | Pass a window of at least 1 |
| `error: argument --event: invalid choice`, exit 2 | The event is neither `pull_request` nor `merge_group` | Pass one of the two |
| `error: unrecognized arguments: --since ...`, exit 2 | `--since` and `--gate` in one command | Drop `--gate` for the recorder's report, or `--since` for the acceptance report |
| `GitHub API rate limit reached; waiting N seconds for the reset` on standard error | The shared limit is spent | The helper sleeps until the reset, at most four times and 900 seconds in all; do not loop the command |
| Exit 1 with a traceback and no table | A `gh api` call failed, ran past its 30 second timeout, or the rate limit helper stopped waiting | Read the error; on a hive, rerun once through `python -m scripts.github_app_cli exec --` so `gh` holds a fresh token |
