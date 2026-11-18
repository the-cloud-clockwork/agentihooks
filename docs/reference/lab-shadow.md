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

## Command

```bash
python -m scripts.ci_lab shadow report --gate <gate> [--heads 50] [--event merge_group]
```

| Option | Default | Meaning |
|---|---|---|
| `--gate` | required | The gate id: `tests`, `mutation`, or a static gate such as `lint` or `type-gate` |
| `--heads` | `50` | How many heads the window holds; at least 1 |
| `--event` | `pull_request` | Read pull request heads, or merge group heads with `merge_group` |

The repository comes from `GITHUB_REPOSITORY`, or `the-cloud-clockwork/agentihooks` when it is
unset. Every GitHub call goes through the rate limited helper `scripts.ci_budget.rate_limit.gh_api`,
so run it where `gh` holds a fresh token (on a hive, through `python -m scripts.github_app_cli exec`).

`shadow report --since <revision>` without `--gate` is the shadow recorder's own report of the
rows in the lab Redis. It is a separate command, `scripts.ci_lab.shadow.main`.

## Which heads it reads

The report lists completed runs of the Tests workflow (`test.yml`) for the event, newest first,
and takes each distinct head once. For each head it reads every check run on the commit with
`filter=latest`, so a job attempt that was rerun counts by its newest attempt.

- **Actions verdict.** Every `github-actions` check run that `scripts.ci_lab.shadow.gate_of` maps
  to the gate, the same mapping the shadow recorder uses. The verdict is `fail` when any of them
  concluded `failure` or `timed_out`, `pass` when all graded ones succeeded. `skipped` and other
  conclusions grade nothing. It completed when the last graded job completed.
- **Lab verdict.** The newest `lab-shadow/<gate>` check run not posted by `github-actions`.
  `success` is `pass`, `failure` and `timed_out` are `fail`. Any other conclusion, a run still in
  progress or no run at all leaves the lab verdict missing.

A head leaves the window, and the next older head takes its place, when any Actions job of the
gate was cancelled, is still running, or none of them graded. A unit job that the workflow reran
after a lost runner (`unit (3.12, 1)` beside `unit rerun (3.12, 1)`) counts only by its rerun, so a
lost runner retry is never a mismatch. The Tests workflow creates `unit rerun` jobs only on merge
group runs, and only for the shards its `unit-lost` job names through `scripts.ci_unit_rerun` as
having lost their runner, so a rerun never hides a shard that failed on a live runner.

## The acceptance rule

Over the window of the newest `--heads` heads that hold an Actions verdict:

| Count | Holds when |
|---|---|
| Heads | the window is full, `--heads` heads were found |
| Mismatches | zero heads where both verdicts exist and disagree |
| Lab verdict present | at least 48 of every 50 heads |
| Lab verdict first | at least 45 of every 50 heads have a lab verdict that completed before the Actions verdict |

The plan words the window as heads "with both verdicts"; the report reads it as heads with an
Actions verdict, because a window of heads that all carry a lab verdict would make the presence
count meaningless. A head without a lab verdict stays in the window and counts against presence.

For a window other than 50 the two thresholds scale and round up: 10 heads need 10 present and 9
first. The rule counts heads; it never sets a time limit. A lab verdict that completes at the same
second as the last Actions job is late.

## Output

```text
gate lint event pull_request
head          actions  lab      order  agreement
009ab2302950  fail     missing
0edd0402cdb4  fail     missing
ae43ba1e663e  pass     missing
heads 3 of 3
lab verdict present 0 of at least 3
mismatches 0 of at most 0
lab verdict first 0 of at least 3
rule fails
```

Each row is one head: the first twelve characters of its sha, the Actions verdict, the lab
verdict or `missing`, `first` or `late`, and `match` or `mismatch`. The last line is `rule holds`
with exit code 0, or `rule fails` with exit code 1. An argument error exits 2.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Every row shows `missing` | The lab is not posting `lab-shadow/<gate>` check runs on these heads | Check the lab scheduler and worker are live and grading in shadow |
| `heads N of 50` below 50 | Too few completed Tests runs with a graded job of the gate in the newest 1000 runs | Use a smaller `--heads`, or wait for more heads |
| A gate never shows rows | `gate_of` maps no Actions job to that gate id | Compare the id with the static jobs in `test.yml` and `scripts.ci_lab.shadow.gate_of` |
| `GitHub API rate limit reached` on standard error | The shared limit is spent | The helper waits for the reset; do not loop the command |
