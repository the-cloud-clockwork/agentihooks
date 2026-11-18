# Post merge testing with automatic revert

**Verdict: no go.** Dev is already red on 33.4 % of its graded pushes, and its newest graded head is red 29.9 % of the time. Merging on the lab's pull request verdict, grading dev after the merge and reverting the culprit automatically would raise that to an estimated 36.4 % to 46.5 % of the time, even if every revert were right. The blocking number is the red dev baseline. A culprit cannot be told apart from noise while a third of dev grades are red and half of the red streaks end on the next grade with no fix. The reversible first step is shadow culprit attribution on today's red dev pushes, which changes no merge path.

Measured on 18 November 2026 for the `eval-post-merge-testing` task ([issue 11905](https://github.com/the-cloud-clockwork/agentihooks/issues/11905)).

## Question

Should a pull request merge into `dev` on its pull request verdict alone, with the merge queue removed, and `dev` graded after the merge, so that a red grade reverts the pull request that caused it? This page gives the red dev rate today, the predicted rate under that model, the blast radius of a revert, and the gates that would make the change safe.

## Window and sources

The window is the 14 UTC days from 4 November to 17 November 2026: runs `created=2026-11-04..2026-11-17` and commits `--since 2026-11-04T00:00:00Z --until 2026-11-18T00:00:00Z`. Every number below names its command. The commands are listed under [Method](#method).

`python -m scripts.ci_metrics report`, named in the task contract, does not exist on `dev` yet. It belongs to the `meas-ci-cost-metrics` task, which had no pull request when this page was written:

```console
$ python -m scripts.ci_metrics report
/opt/venv/bin/python: No module named scripts.ci_metrics
```

The numbers therefore come from GitHub run conclusions of the `Tests` workflow and from `dev` history, as the master ruled. The CI lab's push grade folds only its static gates (see the known limits in [CI lab](../reference/ci-lab.md)), so the lab holds no full post merge grade of `dev` to measure yet.

## Measurements

### Red dev rate today

| Measure | Value | Command |
|---|---|---|
| `Tests` runs on `push` to `dev` | 903: 398 success, 200 failure, 305 cancelled | C1 |
| Red share of graded dev pushes | 200 / 598 = **33.4 %** | C1 |
| Share of the window with the newest graded `dev` head red | **29.9 %** | C4 |
| Merges into `dev` (first parent commits) | 943, 2.81 per hour; per active hour p50 3, p90 6, max 18 | C3, C4 |
| Reverts on `dev` | 0 | C3 |
| Red streaks that ended on a green grade | 76: 39 of one grade, 22 of two or three, 15 of four or more, longest 19 | C4 |
| Red streak length by commit time | p50 40 min, p90 173 min, max 637 min | C4 |
| Push to verdict, created to completed | p50 33 min, p90 100 min | C4 |

Today's `dev` already runs its slow checks after the merge: the promotion tier grades each `dev` push, and a red push holds only promotion ([Delivery tiers](../reference/delivery-tiers.md)). Every red above was fixed forward; none was reverted. A sample of 25 red dev push runs, evenly spaced in time, failed in `coverage-ratchet` in 12, unit shards in 2, `worker-image` in 4, `feature-suite` in 4, `sonar` in 3, and `swarm-image`, `helm-kind`, `brain-smoke`, `ledger-load`, `hierarchy-cpu`, `dep-manifest` and `promote` in one or two each (C5).

### What the queue catches today

| Measure | Value | Command |
|---|---|---|
| `Tests` runs on `merge_group` | 1211: 942 success, 4 failure, 265 cancelled | C2 |
| Green merge group run, created to completed | p50 13 min, p90 21 min | C6 |
| Cancelled merge group runs that hid a failed job | 15 of a 36 run sample: 14 unit shard failures, 1 `select` failure; the other 21 were cancelled from outside | C7 |
| Estimated queue rejects in the window | 4 + 265 × 15 / 36 ≈ **114**, or 12.1 per 100 merges | C2, C7 |
| Rejected pull requests that changed before merging | 13 of 19 took a new commit after the red group; 6 merged with no new commit | C8 |

On a rejected merge group the queue verifier, named `Gate — Required` on `merge_group`, cancels its own run, so most rejects show as `cancelled` rather than `failure`. Every reject that the queue catches today would land on `dev` under post merge testing. The 13 of 19 that took a new commit are an upper bound on real culprits, because a new commit can also be a merge of `dev`.

### Predicted red dev rate under post merge testing

The prediction is a model and should be read as a hypothesis. It assumes that a culprit keeps `dev` red for two verdict cycles at today's p50 (2 × 33 = 66 min), one to detect it and one to grade the revert. It also assumes that every revert names the right pull request, and that the queue's rejects and today's promotion reds are independent.

| Term | Value | Command |
|---|---|---|
| Today's red time if every red streak ended 66 min after it began | 14.5 % of the window | C9 |
| Red time added by queue rejects landing on `dev`, 78 real (114 × 13 / 19) to 114 in total, × 66 min / 20118 min | 25.6 % to 37.4 % | C9 |
| **Predicted share of the window with `dev` red** | 1 − (1 − 0.145)(1 − 0.256) = **36.4 %** to 1 − (1 − 0.145)(1 − 0.374) = **46.5 %** | C9 |
| Predicted red share of graded dev pushes before any revert: 1 − (1 − 0.334)(1 − 0.121)^1.58 | **45.7 %** | C9 |

The exponent 1.58 is the mean number of merges a graded `dev` push covers, because a newer push replaces the pending run of its lane (C4). The automatic revert shortens today's long red streaks: the share falls from 29.9 % to 14.5 % for those alone. That gain is smaller than the reds the queue stops today.

### Revert blast radius

| Measure | Value | Command |
|---|---|---|
| Merges landing on a graded head before its verdict | p50 2, p90 7, max 38 | C4 |
| Green to red transitions whose range holds more than one merge, so the culprit needs bisection | 25 of 75 (33 %); ranges p50 1, p90 3, max 6 merges | C9 |
| A revert of each window merge that conflicts with `dev` 1, 2, 7 and 20 merges later | 1.6 %, 4.0 %, 18.3 %, 36.8 % | C10 |
| Candidate culprits of the 75 green to red transitions, reverted onto `dev` at verdict time | 11 of 118 conflict (9.3 %) | C10 |
| Files behind the conflicts at 7 merges | `feature-map.json` 98 of 171, `tests/TYPE_BASELINE.json` 18, `tests/registry/FROZEN_GATES.json` 7, then single digits | C10 |
| Red streaks that were a single grade followed by green | 39 of 76 (51 %) | C4 |
| Rejected pull requests that merged later with no new commit | 6 of 19 (32 %) | C8 |

At the p90 verdict time, 7 merges sit on a culprit, and nearly one revert in five then conflicts, mostly on shared registry files that almost every pull request appends to. The innocent revert risk is larger. Half of today's red streaks clear on the next grade with no fix, and a third of queue rejects merged unchanged. An automatic revert on the first red grade would revert those pull requests even though the next grade passed.

## Gates that would make it safe

Each gate is measured with the same commands, so the master can recheck it in a later window.

| Gate | Go when | Today | Command |
|---|---|---|---|
| Green dev baseline | red share of graded dev pushes at most 5 % over 14 days | 33.4 % (**blocking**) | C1 |
| Confirmed red | a red unit fails again on one lab rerun before it names a culprit, and single grade red streaks are at most 10 % of red streaks | 51 % | C4 |
| Culprit by merge | every `dev` merge graded alone, or the red range bisected from the lab cache by the queue culprit rule extended to `dev` pushes | 33 % of red ranges hold more than one merge | C9 |
| Detection within the queue budget | dev push verdict p90 at most 21 min, today's merge group p90, so a culprit carries at most about 2 later merges | p90 100 min | C4, C6 |
| Revert safe shared files | `feature-map.json`, `tests/TYPE_BASELINE.json` and `tests/registry/FROZEN_GATES.json` merge without a textual conflict on revert, by a merge driver or one file per entry | 123 of 171 conflicts at 7 merges | C10 |
| Revert path | the App opens the revert as a pull request graded by the lab, reopens the culprit's task, and holds merges while a revert conflicts | absent | scouter read of `dev`, no revert or culprit code |

## Reversible first step

Add shadow culprit attribution to today's red dev pushes, with no change to the merge path. When a dev push grade is red, the lab reruns the failing units once, bisects the push's range from its cache, and records "would revert pull request N" beside the grade. The queue keeps gating merges, so turning the shadow off is one flag. After 14 days, compare each would be revert with what actually turned `dev` green: the fixer task's pull request or the next green grade. Take post merge testing up again only when that precision is at least 95 % and the green dev baseline and detection gates hold. Until then the queue stays. The phase's own work, the lab grading the merge group from its cache, reduces queue cost without accepting a red `dev`.

## Method

`gh` calls on a hive run through `python -m scripts.github_app_cli exec gh`. `R` is `repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs`.

C1 and C2, conclusion counts:

```bash
for ev in "push&branch=dev" merge_group pull_request; do for st in success failure cancelled; do
  echo "$ev $st $(gh api "$R?event=$ev&status=$st&created=2026-11-04..2026-11-17&per_page=1" --jq .total_count)"
done; done
```

```text
push&branch=dev success 398
push&branch=dev failure 200
push&branch=dev cancelled 305
merge_group success 942
merge_group failure 4
merge_group cancelled 265
pull_request success 1197
pull_request failure 1231
pull_request cancelled 2315
```

C3, merges and reverts:

```bash
git log origin/dev --first-parent --since=2026-11-04T00:00:00Z --until=2026-11-18T00:00:00Z \
  --format='%H %cI %s' > dev-commits.txt
wc -l < dev-commits.txt
grep -c -E '^[0-9a-f]+ \S+ Revert' dev-commits.txt
```

```text
943
0
```

C4, streaks, latency and landing, over `dev-push.jsonl` from
`gh api --paginate "$R?event=push&branch=dev&created=2026-11-04..2026-11-17&per_page=100" --jq '.workflow_runs[] | {id, head_sha, conclusion, created_at, run_started_at, updated_at}'`:

```python
import json
from datetime import datetime


def at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


commits = [line.split(" ", 2) for line in open("dev-commits.txt")][::-1]
pos = {c[0]: i for i, c in enumerate(commits)}
ctime = {c[0]: at(c[1]) for c in commits}
runs = [json.loads(line) for line in open("dev-push.jsonl")]
graded = sorted((r for r in runs if r["conclusion"] in ("success", "failure")), key=lambda r: pos[r["head_sha"]])
pairs = list(zip(graded, graded[1:]))
span = (ctime[graded[-1]["head_sha"]] - ctime[graded[0]["head_sha"]]).total_seconds()
red = sum((ctime[b["head_sha"]] - ctime[a["head_sha"]]).total_seconds() for a, b in pairs if a["conclusion"] == "failure")
print("time weighted red %.1f%%" % (100 * red / span))
streaks, cur = [], []
for r in graded:
    if r["conclusion"] == "failure":
        cur.append(r)
    elif cur:
        streaks.append((cur, r))
        cur = []
lens = [len(s) for s, _ in streaks]
print("streaks", len(lens), "one", lens.count(1), "two or three", sum(2 <= n <= 3 for n in lens), "four plus", sum(n >= 4 for n in lens), "max", max(lens))
dur = [(ctime[g["head_sha"]] - ctime[s[0]["head_sha"]]).total_seconds() / 60 for s, g in streaks]
print("streak minutes p50 %.0f p90 %.0f max %.0f" % (pct(dur, 0.5), pct(dur, 0.9), max(dur)))
lat = [(at(r["updated_at"]) - at(r["created_at"])).total_seconds() / 60 for r in graded]
print("verdict minutes p50 %.0f p90 %.0f" % (pct(lat, 0.5), pct(lat, 0.9)))
hours = {}
for c in commits:
    hours[c[1][:13]] = hours.get(c[1][:13], 0) + 1
print("merges per hour %.2f, per active hour p50 %d p90 %d max %d" % (len(commits) / 336, pct(list(hours.values()), 0.5), pct(list(hours.values()), 0.9), max(hours.values())))
landed = [sum(ctime[r["head_sha"]] < ctime[c[0]] <= at(r["updated_at"]) for c in commits) for r in graded]
print("landed p50 %d p90 %d max %d" % (pct(landed, 0.5), pct(landed, 0.9), max(landed)))
gaps = [pos[b["head_sha"]] - pos[a["head_sha"]] for a, b in pairs]
print("merges per graded push mean %.2f" % (sum(gaps) / len(gaps)))
```

```text
time weighted red 29.9%
streaks 76 one 39 two or three 22 four plus 15 max 19
streak minutes p50 40 p90 173 max 637
verdict minutes p50 33 p90 100
merges per hour 2.81, per active hour p50 3 p90 6 max 18
landed p50 2 p90 7 max 38
merges per graded push mean 1.58
```

C5, failed jobs of 25 red dev pushes spaced evenly by time:

```bash
gh api --paginate "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/jobs?filter=latest&per_page=100" \
  --jq '.jobs[] | select(.conclusion=="failure") | .name'
```

C6, green merge group duration over
`gh api --paginate "$R?event=merge_group&status=success&created=2026-11-04..2026-11-17&per_page=100" --jq '.workflow_runs[] | {id, created_at, updated_at}'`:
the created to completed minutes give p50 13 and p90 21.

C7, 36 of the 265 cancelled merge group runs spaced evenly by time, classified by their failed jobs:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/jobs?filter=latest&per_page=100" \
  --jq '[.jobs[] | select(.conclusion=="failure") | .name]'
```

```text
14 a failed unit shard, with unit-lost and the queue gate summary
 1 a failed select job
21 only the queue gate summary failed, the rest cancelled from outside
```

C8, for the 15 failed sampled runs and the 4 `failure` runs, the pull request named in the queue branch `gh-readonly-queue/dev/pr-<n>-<sha>`:

```bash
gh api "repos/the-cloud-clockwork/agentihooks/pulls/$n/commits?per_page=100" \
  --jq "[.[] | .commit.committer.date] | map(select(. > \"$red_group_created_at\")) | length"
```

The result was 13 pull requests with at least one later commit and 6 with none. Every one of the 19 merged.

C9, the model, over the C4 data:

```python
transitions = [pos[b["head_sha"]] - pos[a["head_sha"]] for a, b in pairs if a["conclusion"] == "success" and b["conclusion"] == "failure"]
print("transitions", len(transitions), "one merge", transitions.count(1), "p90", pct(transitions, 0.9), "max", max(transitions))
capped, start = 0.0, None
for a, b in pairs:
    if a["conclusion"] == "failure" and start is None:
        start = ctime[a["head_sha"]]
    if b["conclusion"] == "success" and start is not None:
        capped += min(66, (ctime[b["head_sha"]] - start).total_seconds() / 60)
        start = None
minutes = span / 60
print("capped %.1f%%" % (100 * capped / minutes))
for rejects in (114 * 13 / 19, 114):
    added = rejects * 66 / minutes
    print("rejects %.0f added %.1f%% predicted %.1f%%" % (rejects, 100 * added, 100 * (1 - (1 - capped / minutes) * (1 - added))))
print("per graded push %.1f%%" % (100 * (1 - (1 - 0.334) * (1 - 114 / 943) ** 1.58)))
```

```text
transitions 75 one merge 50 p90 3 max 6
capped 14.5%
rejects 78 added 25.6% predicted 36.4%
rejects 114 added 37.4% predicted 46.5%
per graded push 45.7%
```

C10, the revert simulation. `git merge-tree` in git 2.39 has no `--merge-base`, so the trivial three tree form merges the revert, base `c`, ours a later `dev` head and theirs `c^`, and counts a section as a conflict when it holds conflict markers or deletes a file that the later head changed:

```bash
git merge-tree "$c" "$later" "$c^"
```

```text
1 later merge   15/942 conflict (1.6%)
2 later merges  38/941 conflict (4.0%)
7 later merges  171/936 conflict (18.3%)
20 later merges 340/923 conflict (36.8%)
at 7: feature-map.json 98, tests/TYPE_BASELINE.json 18, tests/registry/FROZEN_GATES.json 7, tests/swarm/tick_scenarios/goldens.json 5, docs/reference/index.md 5, scripts/swarm/tick.py 5
candidate culprits of green to red transitions at verdict time: 11/118 conflict (9.3%)
```
