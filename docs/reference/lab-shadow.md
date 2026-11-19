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

Phase 4 of the grading lab plan, shadow and cutover, sets this rule for every gate flip, printed
with `--heads 50` and pasted in the flip pull request. It words the
window as heads "with both verdicts"; the report reads it as heads with an
Actions verdict, because a window of heads that all carry a lab verdict would make the presence
count meaningless. A head without a lab verdict stays in the window and counts against presence.

The rule counts heads; it never sets a time limit. A lab verdict that completes at the same
second as the last Actions job is late.

### Windows other than 50

For a window other than 50 the two thresholds scale and round up, `scripts.ci_lab.shadow_report.rule`:

| `--heads` | Lab verdict present | Lab verdict first |
|---|---|---|
| 100 | 96 | 90 |
| 50 | 48 | 45 |
| 25 | 24 | 23 |
| 20 | 20 | 18 |
| 10 | 10 | 9 |
| 3 | 3 | 3 |

Below 25 heads every head needs a lab verdict, and below 10 every lab verdict must also come
first. Phase 4 of the grading lab plan uses the default 50.

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

### Reading a table with lab verdicts

Four illustrative heads with made up shas, rendered by `scripts.ci_lab.shadow_report.render`:

```text
gate tests event pull_request
head          actions  lab      order  agreement
1a2b3c4d5e6f  pass     pass     first  match
2b3c4d5e6f7a  fail     fail     late   match
3c4d5e6f7a8b  pass     fail     first  mismatch
4d5e6f7a8b9c  pass     missing
heads 4 of 4
lab verdict present 3 of at least 4
mismatches 1 of at most 0
lab verdict first 2 of at least 4
rule fails
```

| Head | Why its row reads that way |
|---|---|
| `1a2b3c4d5e6f` | Both sides passed and the lab check run completed before the last Actions job of the gate |
| `2b3c4d5e6f7a` | Both sides failed; the lab check run completed in the same second as the last Actions job, which is late |
| `3c4d5e6f7a8b` | The lab failed a head Actions passed: one mismatch, which alone fails the rule |
| `4d5e6f7a8b9c` | Actions graded the head and the lab posted no verdict; it stays in the window and counts against presence |

The counts then fail three ways: 3 lab verdicts against the 4 a window of 4 needs, 1 mismatch
against 0, and 2 first verdicts against 4.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Every row shows `missing` | The lab is not posting `lab-shadow/<gate>` check runs on these heads | Check the lab scheduler and worker are live and grading in shadow |
| One head shows `missing` although it has a `lab-shadow/<gate>` check run | The run came from another App or a workflow token, or the newest run of that name is still in progress or concluded `cancelled`, `skipped` or `neutral` | `gh api 'repos/<repo>/commits/<sha>/check-runs?check_name=lab-shadow/<gate>'` shows each run's `app.slug`, `status` and `conclusion` |
| `heads N of 50` below 50 | Too few completed Tests runs with a graded job of the gate in the newest 1000 runs | Use a smaller `--heads`, or wait for more heads |
| `heads 0 of 50` and `rule fails`, exit 1 | An unknown or mistyped gate id: `gate_of` maps no Actions job to it, so every head leaves the window. A wrong id is no argument error, and the report reads every distinct head of the newest 1000 runs before it prints: 10 pages of runs plus at least one API call per distinct head | Check the id against the `job` field of `python -m scripts.ci_lab.static_gates` and `scripts.ci_lab.shadow.gate_of` before running; run from a checkout of current dev |
| `error: --app or AGENTIHOOKS_LAB_APP names the lab App`, exit 2 | Neither the flag nor the variable names the lab App | Pass `--app <lab App slug>` or export `AGENTIHOOKS_LAB_APP` |
| `error: --heads must be at least 1`, exit 2 | `--heads` is 0 or negative | Pass a window of at least 1 |
| `error: argument --event: invalid choice`, exit 2 | The event is neither `pull_request` nor `merge_group` | Pass one of the two |
| `error: unrecognized arguments: --since ...`, exit 2 | `--since` and `--gate` in one command | Drop `--gate` for the recorder's report, or `--since` for the acceptance report |
| `GitHub API rate limit reached; waiting N seconds for the reset` on standard error | The shared limit is spent | The helper sleeps until the reset, at most four times and 900 seconds in all; do not loop the command |
| Exit 1 with a traceback and no table | A `gh api` call failed, ran past its 30 second timeout, or the rate limit helper stopped waiting | Read the error; on a hive, rerun once through `python -m scripts.github_app_cli exec --` so `gh` holds a fresh token |
