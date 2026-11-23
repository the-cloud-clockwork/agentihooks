---
title: Doctor verdict history
parent: Reference
nav_order: 61
---

# Doctor verdict history

Each Doctor judgment appends a verdict event containing `finding`, `value`,
`note`, `by`, `at` and `measure`. Rejudging the same finding preserves both
events, including identical judgments written in the same millisecond.
The current finding record still holds the latest verdict and controls its
cooldown. Events belong to the Doctor that recorded them.

Before [pull request 6825](https://github.com/the-cloud-clockwork/agentihooks/pull/6825),
each verdict overwrote the previous value, note and evidence on the finding
record, keeping only earlier times and measures. A day's false positive count
could read only the latest verdict per finding, so it was a lower bound. The
event log makes every judgment written that day countable.

## What is recorded

`agentihooks doctor <slug> verdict FINDING VERDICT [--note TEXT]` judges a
Doctor finding. The judgment writes two things in one Redis transaction:

| Record | Redis key | Type | Holds |
|---|---|---|---|
| Current finding record | `agentihooks:swarm:<doctor>:doctor-findings`, field `<finding>` | hash | The latest verdict with its evidence, the earlier verdict times and measures from the last seven days, and the cooldown state |
| Verdict event | `agentihooks:swarm:<doctor>:doctor-verdicts` | sorted set | One member per judgment, scored by the judgment time in epoch milliseconds |

`<doctor>` is the Doctor ledger, the watched ledger with `-doctor` appended,
for example `rig-grade-swarm-doctor`. A watched ledger name longer than 41
characters is shortened and given a six character digest before `-doctor`;
`agentihooks doctor <slug> status` names the Doctor ledger. The key root `agentihooks` follows
`AGENTIHOOKS_SWARM_KEY_PREFIX` when that is set.

Each event holds these fields:

| Field | Meaning |
|---|---|
| `finding` | The finding id, `<class>/<subject>` |
| `value` | One of `false-positive`, `early-real`, `established`, `insufficient-evidence`, `resolved` |
| `note` | The `--note` text, empty without one |
| `by` | `AGENTIHOOKS_AGENT_NAME` of the judging session, else `operator` |
| `at` | Judgment time in epoch milliseconds |
| `measure` | The finding's number when it was judged |

The current record drives the finding's cooldown and return exactly as before:
a judged finding returns only after `AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES` and
only with grown evidence, and it shows the latest note. Events play no part in
that decision.

Only Doctor findings write events. Health findings of an ordinary swarm,
judged with `agentihooks swarm <slug> verdict`, keep their single record under
the swarm's `findings` key and write no event log.

## Retention

| Rule | Behavior |
|---|---|
| Event age | Each judgment removes events more than seven days older than itself |
| Key expiry | The log key expires seven days after its last write, so a Doctor that stops judging loses its log a week later |
| Read window | A read whose start is more than seven days before now is refused |
| Doctor removal | Removing the Doctor swarm deletes its log with its other keys |

Nothing configures the seven day retention; it is the store's fixed history
bound.

## Count a day's verdicts

`agentihooks doctor <slug> verdict-log --since TIME --until TIME` reads the
events in the half open window `[since, until)` and compares them with the
latest verdict per finding. `<slug>` is the watched ledger or its Doctor
ledger. Both bounds are required.

To compare a UTC day's events with the latest verdict per finding:

```bash
agentihooks doctor rig-grade-swarm verdict-log \
  --since 2026-10-23T00:00Z --until 2026-10-24T00:00Z
```

Times are ISO 8601; a time with no offset is read as UTC. Choose a completed
day within the last seven days. The command prints one JSON object:

```json
{
  "since": 1792713600000,
  "until": 1792800000000,
  "event_count": 3,
  "latest_count": 2,
  "events_by_value": {"false-positive": 2, "established": 1},
  "latest_by_value": {"false-positive": 1, "established": 1},
  "events": [
    {"value": "false-positive", "note": "first pass", "by": "master@d44a5a-1", "at": 1792720000000, "measure": 640, "finding": "context-overrun/master@323133-0175"},
    {"value": "false-positive", "note": "same cause", "by": "master@d44a5a-1", "at": 1792760000000, "measure": 640, "finding": "context-overrun/master@323133-0175"},
    {"value": "established", "note": "", "by": "operator", "at": 1792770000000, "measure": 5, "finding": "red-dev-workflow/hive-worker.yml"}
  ]
}
```

| Key | Meaning |
|---|---|
| `since`, `until` | The window bounds in epoch milliseconds |
| `event_count` | Every verdict event judged in the window |
| `latest_count` | Current finding records whose latest verdict was judged in the window |
| `events_by_value` | `event_count` split by verdict value |
| `latest_by_value` | `latest_count` split by verdict value |
| `events` | The events in judgment order |

The day's false positive count is `events_by_value["false-positive"]`.
`latest_by_value["false-positive"]` is the old lower bound, measured the same
way, so the two side by side show how many repeated judgments the latest
record hid. The window need not be a calendar day; this one is the first full
day after collection began on the rig-grade-swarm Doctor:

```bash
agentihooks doctor rig-grade-swarm verdict-log \
  --since 2026-10-25T00:11:55Z --until 2026-10-26T00:11:55Z \
  | jq '{event_count, latest_count, events_by_value, latest_by_value}'
```

List the findings judged more than once in the window:

```bash
agentihooks doctor rig-grade-swarm verdict-log \
  --since 2026-10-25T00:00Z --until 2026-10-26T00:00Z \
  | jq '[.events[].finding] | group_by(.) | map(select(length > 1) | {finding: .[0], judgments: length})'
```

## Inspect the stored log

Read the raw keys with the Redis the swarm connects to:
`AGENTIHOOKS_HIVE_REDIS_URL` when set outside local mode, else
`AGENTIHOOKS_SWARM_REDIS_URL`. The examples use the latter:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ZCARD agentihooks:swarm:rig-grade-swarm-doctor:doctor-verdicts
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" PTTL agentihooks:swarm:rig-grade-swarm-doctor:doctor-verdicts
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ZRANGE agentihooks:swarm:rig-grade-swarm-doctor:doctor-verdicts -3 -1 WITHSCORES
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGET agentihooks:swarm:rig-grade-swarm-doctor:doctor-findings context-overrun/master@323133-0175
```

Each member is a JSON pair `["<random id>", {event}]`. The random id keeps two
identical judgments in the same millisecond as separate members.

In Python, `VerdictStore.events(since, until, now_ms)` returns the same event
list the command prints, applying the same window checks:

```python
from scripts.doctor import loop
from scripts.swarm import cli as swarm

since, until = 1792713600000, 1792800000000
store = swarm.connect()
events = loop.verdicts(store, "rig-grade-swarm-doctor").events(since, until, swarm.now_ms())
```

## Limitations

- Collection begins when the repair reaches the Doctor writer. Earlier
  verdict values and notes were overwritten and cannot be backfilled. On the
  rig-grade-swarm Doctor that was 2026-10-25T00:11:55Z, the Doctor `pull-dev`
  time written in the APPLIED note of task t749 on the
  `rig-grade-swarm-doctor` ledger (`agentihooks ledger --slug
  rig-grade-swarm-doctor get tasks/t749`). Its `applied_at` field is unset, so
  that note is the only record of the time. A window spanning that transition has incomplete
  event counts.
- An empty log does not prove that no judgments occurred before collection
  began.
- `latest_count` includes only current verdicts whose judgment time falls in
  the window. It can fall when a finding is judged again after that window,
  because the later judgment replaces the record the count reads.
- A window can reach back seven days at most, and its end cannot be in the
  future.
- Only judgments written through the Doctor verdict store are logged; a
  finding that was never judged has no event and is not counted by either
  number.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `doctor: verdict log window exceeds seven day retention` | `--since` is more than seven days before now | Pick a window inside the last seven days |
| `doctor: verdict log needs --since before --until and --until no later than now` | The window is empty or reversed, or ends in the future | Put `--since` before `--until` and end the window at or before now; for the current day, end it at the present time |
| `doctor: --since takes an ISO time such as 2026-10-06T12:00Z, not ...` | A bound is not an ISO 8601 time | Write it as `2026-10-23T00:00Z` |
| `agentihooks doctor: error: the following arguments are required: --since, --until`, exit code 2 | A bound is missing | Pass both bounds |
| `doctor: no Doctor watches <slug>; start one with ...` | The slug names neither a Doctor nor a ledger it watches | Use the watched ledger or its `-doctor` ledger |
| `event_count` is 0 while `latest_count` is above 0 | The verdicts were written by Doctor code from before pull request 6825 | Check that the Doctor's checkout contains pull request 6825, and count only windows that start after collection began |
| `event_count` is below `latest_count` | The window starts before collection began | Start the window after the application time |

## Lab health findings

The Doctor judges the CI lab like any other finding. Two detectors read it on
every Doctor pass:

- `lab` reads the lab block the tick stores from the lab's `/capacity`
  answer, the same block `agentihooks swarm <slug> status --json` shows.
- `lab gate` compares the lab with GitHub Actions for each gate that the
  watched swarm checkout's `delivery.yaml` lists under `lab.authoritative`.
  That checkout is the swarm's primary `dev` checkout, which `wt.sh done`
  fast forwards after every merge, so the list is dev's as last pulled.
  It makes no GitHub call while that list is empty. With a gate listed, one
  pass reads the newest ten distinct heads of completed pull request Tests
  runs and the check runs of each of those heads once, whatever the number
  of listed gates: one page of runs and one or two pages of check runs a
  head, 11 to 21 reads on usual heads.

| Finding id | Raised when | Measure |
|---|---|---|
| `lab-unreachable/<swarm>` | The last capacity read failed. No other lab finding is raised from that block. | Minutes since the last good read, or since the failure when no read ever succeeded |
| `lab-behind/<swarm>` | A reading under ten minutes old predicts pull request grading more than `AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES` (20) minutes behind, the same limit that holds eng claims | Minutes behind |
| `lab-cache-hit-ratio/<class>` | A reading under ten minutes old gives the class a verdict cache hit ratio below `AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR` (0.6). A class with no lookups has no ratio and is skipped | Hit ratio in percent |
| `lab-gate-mismatch/<gate>` | An authoritative gate disagrees with GitHub Actions on any of the newest ten pull request heads that hold both verdicts | Mismatches |

The floor must be a fraction from 0 to 1; any other value falls back to 0.6.
The `lab gate` detector takes lab verdicts only from the GitHub App that
`AGENTIHOOKS_LAB_APP` names. While a gate is authoritative and that variable
is unset, the detector fails, so the mismatch count reads as unknown rather
than zero.

The lab verdict of a listed gate is the newest check run of that App named
`lab/<gate>`, the name the lab posts under authority, or `lab-shadow/<gate>`.
The Actions verdict is the gate's job on the same head. On a pull request the
Tests workflow skips the Actions job of a listed gate, and a skipped job is
no verdict, so such a head counts no mismatch: the detector reports the heads
that still hold both, which are dispatched runs, runs under the kill switch
and heads from before the gate was listed. The acceptance report command
`python -m scripts.ci_lab shadow report --gate` is a different read: it walks
back until its window of heads with an Actions verdict is full and reads only
`lab-shadow/<gate>`, so do not run it on a listed gate to reproduce this
finding.

Run the detectors once and print what they find now:

```bash
agentihooks doctor <slug> findings --detector lab --detector "lab gate" --json
```

`findings` runs on any swarm ledger, with or without a Doctor. Without
`--detector` it runs every detector except the trace read. Each finding
prints with its `id` and `measure`. A detector that fails prints a line on
stderr, and the command still exits 0.

| Symptom | Cause | Fix |
|---|---|---|
| `the lab gate detector failed: ValueError: AGENTIHOOKS_LAB_APP names no lab App, ...` | A gate is authoritative and the Doctor's environment does not name the lab App | Set `AGENTIHOOKS_LAB_APP` to the lab App slug in the Doctor's environment |
| `doctor: no detector <name>; the detectors are ...` | A `--detector` value is not a detector name | Use one of the names listed |
| No `lab` findings while the lab is down | The swarm has no `lab_url`, so the tick never reads capacity | Set the swarm's `lab_url`, then wait for a tick |
