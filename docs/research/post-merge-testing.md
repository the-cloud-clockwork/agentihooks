---
title: Post merge testing with automatic revert
parent: Research
nav_order: 1
---

# Post merge testing with automatic revert

**Verdict: no go.** Dev is already red on 33.4 % of its graded pushes, and its newest graded head is red 29.9 % of the time. Merging on the lab's pull request verdict, grading dev after the merge and reverting the culprit automatically would raise that to an estimated 36.4 % to 46.5 % of the time, even if every revert were right. The blocking number is the red dev baseline. A culprit cannot be told apart from noise while a third of dev grades are red and half of the red streaks end on the next grade. The reversible first step is shadow culprit attribution on today's red dev pushes, which changes no merge path.

Measured on 18 November 2026 for the `eval-post-merge-testing` task ([issue 11905](https://github.com/the-cloud-clockwork/agentihooks/issues/11905)).

## Question

Should a pull request merge into `dev` on its pull request verdict alone, with the merge queue removed, and `dev` graded after the merge, so that a red grade reverts the pull request that caused it? This page gives the red dev rate today, the predicted rate under that model, the blast radius of a revert, and the gates that would make the change safe.

## Window and sources

The window is the 14 UTC days from 4 November to 17 November 2026: runs `created=2026-11-04..2026-11-17` and commits `--since 2026-11-04T00:00:00Z --until 2026-11-18T00:00:00Z`. Every number below names its command, C1 to C11 under [Method](#method).

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
| Time weighted red share: the newest graded `dev` head red, over the 20118 min from the first to the last graded head | **29.9 %** | C4 |
| Merges into `dev` (first parent commits) | 943, 2.81 per hour; per active hour p50 3, p90 6, max 18 | C3, C4 |
| Reverts on `dev` | 0 | C3 |
| Red streaks that ended on a green grade | 76: 39 of one grade, 22 of two or three, 15 of four or more, longest 19 | C4 |
| Red streak length by commit time | p50 40 min, p90 173 min, max 637 min | C4 |
| Push to verdict, created to completed | p50 33 min, p90 100 min | C4 |

Today's `dev` already runs its slow checks after the merge: the promotion tier grades each `dev` push, and a red push holds only promotion ([Delivery tiers](../reference/delivery-tiers.md)). Every red above was fixed forward; none was reverted. In 25 red dev push runs spaced evenly in time, the aggregate `Gate — Required` failed in 22, `coverage-ratchet` in 12, `feature-suite` and both `worker-image` jobs in 4 each, `sonar` in 3, and the remaining jobs in at most 2 (C5).

### What the queue catches today

| Measure | Value | Command |
|---|---|---|
| `Tests` runs on `merge_group` | 1211: 942 success, 4 failure, 265 cancelled | C2 |
| Green merge group run, created to completed | p50 13 min, p90 21 min | C6 |
| Cancelled merge group runs that hid a failed job | 15 of 36 sampled: 14 with a failed unit shard, 1 with a failed `select`; in the other 21 no job other than the summary failed, and the verifier's cancel step did not run, so something other than the verifier cancelled them | C7, C7b |
| Estimated queue rejects in the window | 4 + 265 × 15 / 36 ≈ **114**, or 12.1 per 100 merges | C2, C7 |
| Rejects in C7 and C2 whose pull request changed before merging | 13 of 19 rejects (18 pull requests) were followed by a new commit; after 6 the pull request merged with no new commit | C8 |

On a rejected merge group the queue verifier, named `Gate — Required` on `merge_group`, cancels its own run with its `Cancel a rejected queue run` step, so most rejects show as `cancelled` rather than `failure` (C2). Of the 12 sampled rejects that GitHub still served on 19 November, that step ran in 9. The other 3 were cancelled before the verifier started (C7b).

### Predicted red dev rate under post merge testing

The prediction is a model built on these assumptions:

- a culprit keeps `dev` red for two verdict cycles at today's p50 (2 × 33 = 66 min), one to detect it and one to grade the revert;
- every revert names the right pull request;
- every reject the queue catches today would land on `dev`, with real culprits between the 78 that took a new commit (114 × 13 / 19, which counts a merge of `dev` as a change) and all 114;
- the queue's rejects and today's promotion reds are independent.

| Term | Value | Command |
|---|---|---|
| Today's red time if every red streak ended 66 min after it began | 14.5 % of the graded span | C9 |
| Red time added by queue rejects landing on `dev`: 78 to 114 rejects × 66 min / 20118 min | 25.6 % to 37.4 % | C9 |
| **Predicted time weighted red share of `dev`** | 1 − (1 − 0.145)(1 − 0.256) = **36.4 %** to 1 − (1 − 0.145)(1 − 0.374) = **46.5 %** | C9 |
| Predicted red share of graded dev pushes before any revert: 1 − (1 − 0.334)(1 − 0.121)^1.58 | **45.7 %** | C9 |

The exponent 1.58 is the mean number of merges a graded `dev` push covers, because a newer push replaces the pending run of its lane (C4). The automatic revert shortens today's long red streaks: the share falls from 29.9 % to 14.5 % for those alone. That gain is smaller than the reds the queue stops today.

### Revert blast radius

| Measure | Value | Command |
|---|---|---|
| Merges landing on a graded head before its verdict | p50 2, p90 7, max 38 | C4 |
| Green to red transitions whose range holds more than one merge, so the culprit needs bisection | 25 of 75 (33 %); ranges p50 1, p90 3, max 6 merges | C9 |
| A revert of each window merge that conflicts with `dev` 1, 2, 7 and 20 merges later | 1.6 %, 4.0 %, 18.3 %, 36.8 % | C10 |
| The 118 merges inside the 75 green to red ranges, each reverted onto `dev` as it stood at the red verdict | 11 conflict (9.3 %) | C10 |
| Files behind the conflicts at 7 merges | the feature map (`feature-map.json`) 98 of 171, the type baseline (`tests/TYPE_BASELINE.json`) 18, the frozen gate registry (`tests/registry/FROZEN_GATES.json`) 7, then at most 5 each | C10 |
| Red streaks that were a single grade followed by green | 39 of 76 (51 %) | C4 |
| Rejects after which the pull request merged with no new commit | 6 of 19 (32 %) | C8 |

At the p90 verdict time, 7 merges sit on a culprit, and nearly one revert in five then conflicts, mostly on the shared registries that almost every pull request appends to. The innocent revert risk is larger. Half of today's red streaks end on the next grade, and a third of queue rejects merged unchanged. An automatic revert on the first red grade would revert those pull requests even though the next grade passed.

## Gates that would make it safe

Each gate is measured with the same commands, so the master can recheck it in a later window; [Rechecking in a later window](#rechecking-in-a-later-window) lists what to change.

| Gate | Go when | Today | Command |
|---|---|---|---|
| Green dev baseline | red share of graded dev pushes at most 5 % over 14 days | 33.4 % (**blocking**) | C1 |
| Confirmed red | a red unit fails again on one lab rerun before it names a culprit, and single grade red streaks are at most 10 % of red streaks | 51 % | C4 |
| Culprit by merge | every `dev` merge graded alone, or the red range bisected from the lab cache by the queue culprit rule extended to `dev` pushes | 33 % of red ranges hold more than one merge | C9 |
| Detection within the queue budget | dev push verdict p90 at most 21 min, today's merge group p90; at the p90 busy hour of 6 merges per hour that leaves 6 × 21 / 60 = 2.1 merges on a culprit, where a revert conflicts about 4 % of the time | p90 100 min | C4, C6, C10 |
| Revert safe shared registries | the feature map, the type baseline and the frozen gate registry merge without a textual conflict on revert, by a merge driver or one file per entry | 118 of 171 conflicting reverts at 7 merges, 83 of them only in a registry | C10 |
| Revert path | the App opens the revert as a pull request graded by the lab, reopens the culprit's task, and holds merges while a revert conflicts | absent: no revert or culprit code on `dev` | C11 |

## Reversible first step

Add shadow culprit attribution to today's red dev pushes, with no change to the merge path. When a dev push grade is red, the lab reruns the failing units once, bisects the push's range from its cache, and records "would revert pull request N" beside the grade. The queue keeps gating merges, so turning the shadow off is one flag. After 14 days, compare each would be revert with what actually turned `dev` green: the fixer task's pull request or the next green grade. Take post merge testing up again only when that precision is at least 95 % and the green dev baseline and detection gates hold. Until then the queue stays. The phase's own work, the lab grading the merge group from its cache, reduces queue cost without accepting a red `dev`.

## Method

The commands below use plain `gh`; where they ran, on a swarm hive, each went through `python -m scripts.github_app_cli exec gh`, which only supplies a fresh token. Run them in an empty working folder from `agentihooks scratch new`, with `GIT_DIR` pointing at an agentihooks clone that has fetched `origin/dev`.

Downloads used by C4 to C10:

```bash
R=repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs
WINDOW="created=2026-11-04..2026-11-17&per_page=100"
gh api --paginate "$R?event=push&branch=dev&$WINDOW" \
  --jq '.workflow_runs[] | {id, head_sha, conclusion, created_at, run_started_at, updated_at}' > dev-push.jsonl
gh api --paginate "$R?event=merge_group&status=success&$WINDOW" --jq '.workflow_runs[] | {id, created_at, updated_at}' > mg-success.jsonl
gh api --paginate "$R?event=merge_group&status=cancelled&$WINDOW" --jq '.workflow_runs[] | {id, head_branch, created_at}' > mg-cancelled.jsonl
gh api "$R?event=merge_group&status=failure&$WINDOW" --jq '.workflow_runs[] | {id, head_branch, created_at}' > mg-failure.jsonl
```

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

C4, streaks, latency and landing:

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
print("graded span minutes %.0f" % (span / 60))
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
graded span minutes 20118
time weighted red 29.9%
streaks 76 one 39 two or three 22 four plus 15 max 19
streak minutes p50 40 p90 173 max 637
verdict minutes p50 33 p90 100
merges per hour 2.81, per active hour p50 3 p90 6 max 18
landed p50 2 p90 7 max 38
merges per graded push mean 1.58
```

C5, failed jobs of 25 red dev pushes spaced evenly by creation time:

```bash
python3 -c 'import json; r=sorted(map(json.loads, open("dev-push.jsonl")), key=lambda r: r["created_at"]); f=[x for x in r if x["conclusion"]=="failure"]; [print(f[int(i*len(f)/25)]["id"]) for i in range(25)]' > c5-ids.txt
while read -r id; do
  gh api --paginate "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/jobs?filter=latest&per_page=100" \
    --jq ".jobs[] | select(.conclusion==\"failure\") | \"$id \(.name)\""
done < c5-ids.txt > c5-jobs.txt
python3 -c 'import collections,re; runs=collections.defaultdict(set); [runs[re.sub(r" \(.*","",l.rstrip("\n").split(" ",1)[1])].add(l.split(" ",1)[0]) for l in open("c5-jobs.txt")]; [print(len(v), k) for k, v in sorted(runs.items(), key=lambda kv: (-len(kv[1]), kv[0]))]'
```

```text
22 Gate — Required
12 coverage-ratchet
4 feature-suite / features
4 worker-image / worker-image
4 worker-image / worker-image-compare
3 sonar
2 helm-kind / kind
2 shard-check
2 swarm-image / smoke
2 unit
1 Launch hotfix grade
1 brain-smoke / smoke
1 dep-manifest
1 hierarchy-cpu / grade
1 hierarchy-cpu / load
1 ledger-load / load
1 promote / promote
```

C6, green merge group duration:

```bash
python3 -c 'import json; from datetime import datetime as D; t=lambda s: D.fromisoformat(s.replace("Z","+00:00")); d=sorted((t(r["updated_at"])-t(r["created_at"])).total_seconds()/60 for r in map(json.loads, open("mg-success.jsonl"))); print("green merge groups %d, minutes p50 %.0f p90 %.0f" % (len(d), d[int(.5*len(d))], d[int(.9*len(d))]))'
```

```text
green merge groups 942, minutes p50 13 p90 21
```

C7, 36 of the 265 cancelled merge group runs spaced evenly by creation time, classified by their failed jobs:

```bash
python3 -c 'import json; r=sorted(map(json.loads, open("mg-cancelled.jsonl")), key=lambda r: r["created_at"]); [print(r[int(i*len(r)/36)]["id"]) for i in range(36)]' > c7-ids.txt
while read -r id; do
  gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/jobs?filter=latest&per_page=100" \
    --jq "{run: $id, failed: [.jobs[] | select(.conclusion==\"failure\") | .name]}"
done < c7-ids.txt > c7-jobs.jsonl
python3 -c 'import collections,json; k=collections.Counter(); [k.update(["unit shard failed" if any(n.startswith("unit (") for n in f) else "other job failed: "+", ".join(f) if f else "only the summary failed"]) for f in ([n for n in json.loads(l)["failed"] if n!="Queue gate summary"] for l in open("c7-jobs.jsonl"))]; [print(n, x) for x, n in k.most_common()]'
```

```text
21 only the summary failed
14 unit shard failed
1 other job failed: select
```

C7b, how each C7 run was cancelled. It reads the queue verifier job of each sampled run with its steps. A verifier that never started is listed under its unevaluated name expression, which contains `queue-fail-fast`, and has no steps:

```bash
while read -r id; do
  gh api "repos/the-cloud-clockwork/agentihooks/actions/runs/$id/jobs?filter=latest&per_page=100" \
    --jq "{run: $id, verifier: [.jobs[] | select(.name == \"Gate — Required\" or (.name | contains(\"queue-fail-fast\"))) | {name, steps: [.steps[] | {name, conclusion, started_at}]}]}"
done < c7-ids.txt > c7-verifier.jsonl
```

```python
import collections
import json

failed = {j["run"]: [n for n in j["failed"] if n != "Queue gate summary"] for j in map(json.loads, open("c7-jobs.jsonl"))}
kinds = collections.Counter()
for v in map(json.loads, open("c7-verifier.jsonl")):
    step = {s["name"]: s for j in v["verifier"] for s in j["steps"]}.get("Cancel a rejected queue run")
    if step and step["started_at"]:
        state = "own cancel step ran, ended " + step["conclusion"]
    elif any(j["name"] == "Gate — Required" for j in v["verifier"]):
        state = "verifier started, own cancel step did not run"
    else:
        state = "verifier never started"
    kinds[("a job failed" if failed[v["run"]] else "no other job failed") + ": " + state] += 1
for kind, n in sorted(kinds.items()):
    print(n, kind)
```

```text
5 a job failed: own cancel step ran, ended cancelled
4 a job failed: own cancel step ran, ended success
3 a job failed: verifier never started
20 no other job failed: verifier never started
1 no other job failed: verifier started, own cancel step did not run
```

A cancel step ran when it has a start time. At the first and the last window merge, the workflow file calls the cancel API in two steps, and its concurrency group cancels only `pull_request` runs. The verifier's `Cancel a rejected queue run` step cancels its own run. The `Cancel superseded and dead merge queue runs` step cancels only queue runs with a lower id than its own. C7b does not show what cancelled a run whose verifier did not run its cancel step:

```bash
for c in $(tail -1 dev-commits.txt | cut -d" " -f1) $(head -1 dev-commits.txt | cut -d" " -f1); do
  git show "$c:.github/workflows/test.yml" | grep -n -E 'cancel-in-progress|name: Cancel|gh run cancel|runs/\$id/cancel|-lt "\$RUN"'
done
```

```text
29:  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
391:      - name: Cancel superseded and dead merge queue runs
416:            [[ -n "$id" && "$id" -lt "$RUN" ]] || continue
424:            if gh api -X POST "repos/$GITHUB_REPOSITORY/actions/runs/$id/cancel" --silent; then
3359:      - name: Cancel a rejected queue run
3363:        run: gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"
29:  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
531:      - name: Cancel superseded and dead merge queue runs
556:            [[ -n "$id" && "$id" -lt "$RUN" ]] || continue
564:            if gh api -X POST "repos/$GITHUB_REPOSITORY/actions/runs/$id/cancel" --silent; then
3730:      - name: Cancel a rejected queue run
3734:        run: gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"
```

C7b ran on 19 November 2026 in its own working folder, so the C7 and C8 files of 18 November stayed as they were. GitHub no longer served the 18 earliest of the 265 cancelled merge group runs by then, so that folder's `c7-ids.txt` was rebuilt from a fresh `mg-cancelled.jsonl` with the C7 rule shifted by those 18 runs, and C7's jobs loop ran again over it there. That keeps 33 of the 36 runs. The first three runs in C8 were created before the earliest run still served, so they are the 3 missing, each with a failed job, and all 21 runs where no other job failed are included:

```bash
wc -l < mg-cancelled.jsonl
python3 -c 'import json; print(min(json.loads(l)["created_at"] for l in open("mg-cancelled.jsonl")))'
python3 -c 'import json; r=sorted(map(json.loads, open("mg-cancelled.jsonl")), key=lambda r: r["created_at"]); off=265-len(r); [print(r[int(i*265/36)-off]["id"]) for i in range(36) if int(i*265/36) >= off]' > c7-ids.txt
wc -l < c7-ids.txt
```

```text
247
2026-11-04T21:12:06Z
33
```

C8, for the 15 failed C7 runs and the 4 `failure` runs, the pull request named in the queue branch `gh-readonly-queue/dev/pr-<n>-<sha>`, its merge time and its commits after the red group:

```bash
python3 -c 'import json,re; c={str(r["id"]):r for r in map(json.loads, open("mg-cancelled.jsonl"))}; rows=[c[str(j["run"])] for j in map(json.loads, open("c7-jobs.jsonl")) if [n for n in j["failed"] if n!="Queue gate summary"]]+list(map(json.loads, open("mg-failure.jsonl"))); [print(r["id"], re.search(r"pr-(\d+)-", r["head_branch"]).group(1), r["created_at"]) for r in rows]' > c8-prs.txt
while read -r run n at; do
  echo "$run $n $(gh api "repos/the-cloud-clockwork/agentihooks/pulls/$n" --jq .merged_at)" \
    "$(gh api "repos/the-cloud-clockwork/agentihooks/pulls/$n/commits?per_page=100" --jq "[.[] | .commit.committer.date] | map(select(. > \"$at\")) | length")"
done < c8-prs.txt
```

```text
41755270692 9594 2026-11-04T02:53:48Z 1
41780593852 9630 2026-11-04T07:45:45Z 1
41838029431 9658 2026-11-04T14:10:44Z 2
42011238034 9794 2026-11-05T11:44:38Z 1
42151787606 9806 2026-11-06T04:55:55Z 3
42744830340 10399 2026-11-10T05:06:30Z 1
43008726721 10677 2026-11-11T13:02:06Z 1
43035202852 10671 2026-11-11T16:54:24Z 3
43047043330 10578 2026-11-11T15:49:19Z 0
43055746589 10671 2026-11-11T16:54:24Z 1
43126831451 10794 2026-11-12T01:58:46Z 1
43169558796 10845 2026-11-12T07:28:52Z 1
43757112784 11363 2026-11-15T22:50:51Z 0
43762102058 11355 2026-11-15T21:56:05Z 0
43876684668 11554 2026-11-16T17:17:16Z 2
42670371791 10318 2026-11-10T01:16:17Z 1
42404741735 10041 2026-11-07T19:28:45Z 0
42403236647 10035 2026-11-07T19:09:54Z 0
42396908025 9984 2026-11-07T17:57:24Z 0
```

All 19 rejects, across 18 pull requests, merged later: 13 after at least one new commit, 6 with none.

C9, the model, over the C4 data:

```python
transitions = [pos[b["head_sha"]] - pos[a["head_sha"]] for a, b in pairs if a["conclusion"] == "success" and b["conclusion"] == "failure"]
print("transitions", len(transitions), "one merge", transitions.count(1), "p90", pct(transitions, 0.9), "max", max(transitions), "merges in ranges", sum(transitions))
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
transitions 75 one merge 50 p90 3 max 6 merges in ranges 118
capped 14.5%
rejects 78 added 25.6% predicted 36.4%
rejects 114 added 37.4% predicted 46.5%
per graded push 45.7%
```

C10, the revert simulation. `git merge-tree` in git 2.39 has no `--merge-base`, so the trivial three tree form merges each revert, base the merge `c`, ours a later `dev` head and theirs `c^`. A section counts as a conflict when it holds conflict markers or deletes a file that the other side changed:

```python
import json
import re
import subprocess
from datetime import datetime


def at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


commits = [line.split(" ", 2) for line in open("dev-commits.txt")][::-1]
sha = [c[0] for c in commits]
ctime = [at(c[1]) for c in commits]
pos = {s: i for i, s in enumerate(sha)}
HEADS = "changed in both|removed in remote|removed in local|added in both|added in remote|added in local|merged"
REGISTRIES = {"feature-map.json", "tests/TYPE_BASELINE.json", "tests/registry/FROZEN_GATES.json"}


def conflicts(i, j):
    out = subprocess.run(["git", "merge-tree", sha[i], sha[j], sha[i] + "^"], capture_output=True, text=True, errors="replace").stdout
    files = []
    for sec in re.split(r"\n(?=(?:" + HEADS + r")\n)", "\n" + out):
        head = sec.strip().split("\n", 1)[0]
        if head in ("changed in both", "added in both") and "<<<<<<<" in sec:
            files.append(re.search(r"  (?:our|their)\s+\d+ \w+ (\S+)", sec).group(1))
        elif head in ("removed in remote", "removed in local"):
            ent = dict(re.findall(r"  (base|our|their)\s+\d+ (\w+) \S+", sec))
            side = "our" if head == "removed in remote" else "their"
            if "base" in ent and side in ent and ent["base"] != ent[side]:
                files.append(re.search(r"  base\s+\d+ \w+ (\S+)", sec).group(1))
    return files


hot = {}
for k in (1, 2, 7, 20):
    hits = [conflicts(i, i + k) for i in range(len(sha) - k)]
    bad = sum(1 for h in hits if h)
    print("%d later: %d/%d conflict (%.1f%%)" % (k, bad, len(hits), 100 * bad / len(hits)))
    if k == 7:
        at7 = [set(h) for h in hits if h]
        for h in hits:
            for f in h:
                hot[f] = hot.get(f, 0) + 1
print("at 7:", ", ".join("%s %d" % kv for kv in sorted(hot.items(), key=lambda kv: -kv[1])[:6]))
reg = [h for h in at7 if h & REGISTRIES]
print("at 7: %d of %d conflicting reverts hit a registry, %d hit only registries, %d hit two or more" % (len(reg), len(at7), sum(h <= REGISTRIES for h in reg), sum(len(h & REGISTRIES) >= 2 for h in reg)))
runs = [json.loads(line) for line in open("dev-push.jsonl")]
graded = sorted((r for r in runs if r["conclusion"] in ("success", "failure")), key=lambda r: pos[r["head_sha"]])
candidates = bad = 0
for a, b in zip(graded, graded[1:]):
    if a["conclusion"] == "success" and b["conclusion"] == "failure":
        verdict = max(i for i in range(len(sha)) if ctime[i] <= at(b["updated_at"]))
        for i in range(pos[a["head_sha"]] + 1, pos[b["head_sha"]] + 1):
            candidates += 1
            bad += bool(conflicts(i, verdict))
print("candidates %d, conflicting at verdict time %d (%.1f%%)" % (candidates, bad, 100 * bad / candidates))
```

```text
1 later: 15/942 conflict (1.6%)
2 later: 38/941 conflict (4.0%)
7 later: 171/936 conflict (18.3%)
20 later: 340/923 conflict (36.8%)
at 7: feature-map.json 98, tests/TYPE_BASELINE.json 18, tests/registry/FROZEN_GATES.json 7, tests/swarm/tick_scenarios/goldens.json 5, docs/reference/index.md 5, scripts/swarm/tick.py 5
at 7: 118 of 171 conflicting reverts hit a registry, 83 hit only registries, 5 hit two or more
candidates 118, conflicting at verdict time 11 (9.3%)
```

The per file line counts a revert once for each file it conflicts in. The three registry counts add up to 123, five more than the 118 reverts behind them, because each of the 5 reverts that hit two or more registries counts once per registry. The `hit a registry` line was added on 19 November 2026. That rerun printed the four distance lines and the per file line above unchanged. Its last line read 106 candidates from a fresh `dev-push.jsonl` of 842 runs, against the 903 of C1, so the line above keeps the 18 November value.

C11, revert or culprit code on `dev`:

```bash
git grep -l -i -E 'git revert|auto[ _-]?revert|culprit' origin/dev -- scripts hooks .github | wc -l
```

```text
0
```

## Rechecking in a later window

The commands under [Method](#method) carry this window's dates, sample sizes and derived numbers as literals. A recheck sets each literal in the order below, because every derived number comes from an earlier command's output.

Since this page was written, `python -m scripts.ci_metrics` has landed as an hourly job that writes runner minutes, cancelled runs and lead time to ClickHouse ([CI cost and lead time metrics](../reference/ci-metrics.md)). It has no `report` subcommand and records no failed run count, merge group reject or revert conflict, so a recheck still runs C1 to C11.

| Literal | Where | Set it to |
|---|---|---|
| `created=2026-11-04..2026-11-17` | `WINDOW` in the downloads, and again inside the C1 loop | the new first and last UTC day, both included |
| `--since=2026-11-04T00:00:00Z --until=2026-11-18T00:00:00Z` | C3 | midnight UTC at the start of the first day, and after the last day |
| `336` | C4, merges per hour | hours in the window, 24 × days |
| `25` | C5, twice | the red dev push sample, no larger than the C1 `push&branch=dev failure` count |
| `36` | C7, twice | the cancelled merge group sample, no larger than the C2 `merge_group cancelled` count |
| `66` | C9, twice | 2 × the C4 `verdict minutes p50` |
| `114` | C9, three times | `4 + 265 × 15 / 36` in this window: the C2 `merge_group failure` count, plus the C2 `merge_group cancelled` count times the C7 runs with a failed job other than the summary, divided by the C7 sample size |
| `13 / 19` | C9 | the C8 rejects followed by at least one new commit / all C8 rejects |
| `0.334` | C9 | the C1 red share: `push&branch=dev failure` / (`success` + `failure`) |
| `943` | C9 | the C3 merge count |
| `1.58` | C9 | the C4 `merges per graded push mean` |

The commands depend on each other:

- The downloads and C3 come first. C1 and C2 are counts that need nothing else.
- C4 and C9 run in one Python session, because C9 reads C4's `pos`, `ctime`, `pairs`, `span` and `pct`. Append C9 to the C4 file and run it with `python3`.
- C7b reads the `c7-ids.txt` and `c7-jobs.jsonl` that C7 writes, and `dev-commits.txt` from C3. Run it in the same window as C7, so it needs no rebuilt `c7-ids.txt` and skips the rebuild command under its output.
- C8 reads the `c7-jobs.jsonl` that C7 writes, and the `mg-failure.jsonl` and `mg-cancelled.jsonl` downloads.
- C10 runs on its own over `dev-commits.txt` and `dev-push.jsonl`. It calls `git merge-tree` once per window merge for each of the four distances and once per green to red candidate, 3,860 `git merge-tree` calls in this window, all local.
- C11 reads `origin/dev` and has no window.

The GitHub reads cost about 170 REST calls at this window's run counts: 24 download pages of 100 runs, 9 counts in C1, one jobs read per sampled run in C5, C7 and C7b, and two reads per reject in C8. Every agent on a swarm hive shares one GitHub API limit, so read the remaining budget first and, on a rate limit error, wait for the reset it reports instead of retrying:

```bash
gh api rate_limit --jq '.resources.core | {remaining, reset}'
```

Before C4, compare every download with its C1 or C2 count. `mg-success.jsonl`, `mg-cancelled.jsonl` and `mg-failure.jsonl` each hold exactly their count; `dev-push.jsonl` holds no fewer lines than the three `push&branch=dev` counts together, because it also keeps runs with other conclusions:

```bash
wc -l dev-push.jsonl mg-success.jsonl mg-cancelled.jsonl mg-failure.jsonl
```

| Symptom | Cause | Fix |
|---|---|---|
| A download is shorter than its count | GitHub documents a cap of 1,000 results for each filtered search in [List workflow runs for a repository](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository). The dev push and green merge group downloads held 903 and 942 runs in this window, so a busier window reaches the cap first | Split `created` into two ranges, download each, and concatenate the files |
| `mg-failure.jsonl` holds exactly 100 lines | The download has no `--paginate`, so it returns one page of 100 runs | Add `--paginate` to that download |
| C4 or C10 stops with a `KeyError` on a head sha | A run created inside the window graded a commit made before `--since`, so the sha is missing from `pos` | Keep only those runs before sorting `graded`: `runs = [r for r in runs if r["head_sha"] in pos]` |

A recheck changes the verdict only through [Gates that would make it safe](#gates-that-would-make-it-safe): compare each new value with its "Go when" column. [Reversible first step](#reversible-first-step) sets the rule for taking the question up again: after 14 days of shadow culprit attribution, at least 95 % of its would be reverts match what actually turned `dev` green, and the green dev baseline and the detection gates hold. C11 still finds no revert or culprit code on `dev`.
