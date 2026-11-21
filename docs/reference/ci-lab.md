---
title: CI lab service contract
parent: Reference
nav_order: 90
---

# CI lab service contract
{: .no_toc }

The CI lab runs as two services, the scheduler and the worker. This page lists what each one
exposes and reads: its entry point, environment, ports, metric names and capacity document.
Every name lives in `scripts.ci_lab.contract`; the services and the platform tests are to
import these constants by name, so a rename fails an import instead of dropping a series. The
worker's PriorityClass is a cluster object outside that module;
[Worker scheduling priority](#worker-scheduling-priority) covers it, and
[Worker Deployment](#worker-deployment) covers the chart objects that run the worker.

## Entry points

| Command | Module form | What it does today |
|---|---|---|
| `agentihooks ci-lab-scheduler --self-test` | `python -m scripts.ci_lab scheduler --self-test` | Checks the scheduler contract offline and prints it as JSON |
| `agentihooks ci-lab-scheduler [--interval S] [--passes N]` | `python -m scripts.ci_lab scheduler [--interval S] [--passes N]` | Runs the scheduler service: reads the runtime config, serves the lease API, `/capacity` and `/grades/<sha>` on port 8800 and `/metrics` on 9108, and runs one grading pass every interval (2 seconds), forever unless `--passes` is set |
| `agentihooks ci-lab-worker --self-test` | `python -m scripts.ci_lab worker --self-test` | Refuses any GitHub App variable, checks the worker contract offline and prints it as JSON |
| `agentihooks ci-lab-worker` | `python -m scripts.ci_lab worker` | Runs the worker service: builds the pod from the worker environment, leases units from the scheduler and runs each through the adapter of its kind (see [Worker executors](#worker-executors)) until SIGTERM, then lets running units drain; serves `/metrics` on 9108 from its own registry until the drain ends, and exits at start when it cannot bind 9108 |

The self tests make no network, Redis, object store or GitHub calls. They exit 0 when the
contract holds and 1 with the reason on standard error when it does not. A self test fails when
a metric name is not a valid Prometheus name starting with `ci_lab_`, a port repeats or falls
outside 1 to 65535, or the worker environment grants `GITHUB_APP_`. `-h` prints usage. The
scheduler and the worker without `--self-test` run their services (see [Scheduler loop](#scheduler-loop)
and [Worker executors](#worker-executors)).

Both services run from the installed wheel, which ships `hooks`, `scripts` and `profiles` and no
`tests` package, so lab code imports only from those packages. The dependency manifest lives in
`scripts.dep_manifest` and the shard sizing the lab shares with the test shards in
`scripts.shard_sizing`. Three checks hold it:

| Check | Where | Red when |
|---|---|---|
| `tests/ci_lab/test_wheel_imports.py` | Pull request tests | Any module of `scripts.ci_lab`, or `scripts.ci_test_plan`, imports the `tests` package |
| `shipped-code-imports-tests` | Semgrep incident rules | New code under `scripts` or `hooks` imports the `tests` package |
| Import the lab scheduler's modules inside the swarm image | Swarm container smoke | The built swarm image cannot import a module of `scripts.ci_lab`, the executors and mutation expansion the scheduler loads at call time included, or the CI test planner |

`docker/ci-lab/smoke.sh` also imports the worker's executors, test runner and pricing inside the
built CI lab image.

## Run the scheduler service

`agentihooks ci-lab-scheduler` starts both HTTP listeners and the grading loop. Before starting
it, provision the lab heads bus and its scheduler and relay consumers, Redis, the object store,
and a readable shared git mirror. The scheduler also needs a writable private mirror store and
access to the repository's `dev` branch to read its runner trust list and retry policy. Startup
binds the listeners only after these clients and the scheduler have been built.

The swarm image includes the locked `git` package. A different image or a host running the
installed wheel must provide `git` on `PATH`: the mirror expander invokes it during startup.
The offline `--self-test` checks the contract without starting either listener or checking these
dependencies. Run it separately from service options such as `--interval` and `--passes`.

### Runtime configuration

Mount a YAML file and set `CI_LAB_RUNTIME_CONFIG` to its path. This complete example allocates
600 core minutes per UTC day:

```yaml
classes:
  - hotfix
  - pull_request
  - queue
  - promotion
  - backfill
budget:
  coreMinutesPerDay: 600
  shed:
    backfill: 0.5
    promotion: 0.75
    queue: 0.9
backfill:
  auditShare: 0.1
  moduleSweeps: false
```

All three top level fields, `classes`, `budget` and `backfill`, are required; unknown fields
are refused. Classes must be unique names from the list above, and the daily budget must be
positive. Shed fractions and `auditShare` range from 0 to 1; `moduleSweeps` is a boolean. Only
`backfill`, `promotion` and `queue` may have shed thresholds. Each threshold is the fraction of
the daily budget spent; reaching it sheds that class and every less urgent class. In this
example, backfill is shed at half the daily budget remaining, promotion at a quarter and queue
at a tenth; each stays shed as the remaining budget falls further.
Hotfix and pull request work remain admitted. The budget resets on the next UTC day; work
already queued still runs. See [Scheduler loop](#scheduler-loop) for admission rules.

Validate the mounted file without connecting to the service dependencies:

```bash
export CI_LAB_RUNTIME_CONFIG=/etc/ci-lab/runtime.yaml
python -m scripts.ci_lab runtime "$CI_LAB_RUNTIME_CONFIG"
```

Valid input exits 0 without output. A schema error exits 1 and names the failing field on
standard error, for example `runtime budget: 'shed' is a required property`. An unreadable file
or invalid YAML also prevents startup. The service reads and validates the file once, before
creating its Redis, object store or bus clients; restart the service to apply a changed file.

### Environment and launch

Set the nonsecret addresses and mounted paths for your deployment, for example:

```bash
export CI_LAB_RUNTIME_CONFIG=/etc/ci-lab/runtime.yaml
export CI_LAB_REDIS_URL=redis://redis:6379/0
export CI_LAB_URL=http://lab:8800
export CI_LAB_MINIO_ENDPOINT=http://minio:9000
export CI_LAB_MINIO_BUCKET=ci-lab
export AGENTIHOOKS_BUS=nats
export AGENTIHOOKS_BUS_URL=nats://nats:4222
export CI_LAB_GIT_MIRROR=/data/git-mirror.git
export CI_LAB_MIRROR_STORE=/data/scheduler-mirrors
export CI_LAB_REPOSITORY=https://github.com/the-cloud-clockwork/agentihooks.git

agentihooks ci-lab-scheduler
```

`CI_LAB_URL` is the address consumers can reach; the relay uses it in grade links. It is
required at startup and does not change the listener's bind address.

The deployment must also inject `CI_LAB_MINIO_ACCESS_KEY`, `CI_LAB_MINIO_SECRET_KEY`, the
scheduler's `GITHUB_APP_*` identity and any bus or repository authentication. Keep their values
in the deployment's secret mechanism. Set `CI_LAB_CONTROL_TOKEN` there when the swarm needs the
[control routes](#control-routes); without it those routes refuse requests. The
[Environment](#environment) table lists the other service settings.

The default loop runs indefinitely with a two second sleep after each pass. To run three real
grading passes with a half second sleep, then exit:

```bash
agentihooks ci-lab-scheduler --interval 0.5 --passes 3
```

`--passes 0` also runs indefinitely. A finite run starts all three listeners, consumes heads, grades
work and ticks the relay just like the service; use `--self-test` for an offline check. Each
pass prints its scheduler and relay actions to standard output. The CLI has no host or port
options: it binds all interfaces on 8800, 8801 and 9108. The chart or container network controls
which clients can reach them.

### Verify the running service

Run these requests from a client that can reach the scheduler. Replace `lab` with its service
address and the sample sha with the full commit you want to inspect:

```bash
curl -fsS http://lab:8800/capacity \
  | jq '{slots, budget_fraction, shed, pull_request: .classes.pull_request}'
curl -fsS http://lab:8800/grades/0123456789abcdef0123456789abcdef01234567 \
  | jq '{head, grades}'
curl -fsS http://lab:9108/metrics \
  | grep --line-buffered -E '^ci_lab_(slots_total|slots_busy|queue_wait_seconds|budget_fraction|class_shed)'
```

`/capacity` returns the [capacity document](#capacity-document). Each capacity request and
metrics scrape builds a fresh load from queue, lease, live grade, recent worker offer and
daily budget state; separate requests can observe different instants. Without recent worker
offers and leased jobs, a running scheduler can report zero total slots. Offers expire after
60 seconds, and unit estimates remain fixed at 60 seconds per slot.

The grades reply is `{"head": "<sha>", "grades": [...]}`. An ungraded commit returns 200 with
`grades: []`; an unfinished grade has a null conclusion. A short or uppercase sha returns 400
with `{"error": "a grade lookup names one full commit sha"}`. See
[Grades of a commit](#grades-of-a-commit) for open and closed grade lookup and fields.

### Scheduler startup troubleshooting

| Symptom | Check and action |
|---|---|
| Startup fails before either listener appears | Check that `CI_LAB_RUNTIME_CONFIG` names a readable YAML file and run the runtime validator; missing required environment names also prevent startup |
| Startup fails while reading the repository | Check `git` on `PATH`, the shared mirror mount, private mirror store permissions, repository authentication and access to `dev` |
| Startup fails with a `jsonschema.ValidationError` | Dev's `delivery.yaml` breaks its schema; the retry policy read validates it. See the troubleshooting table of [Retry policy](#retry-policy) |
| Startup cannot reach a dependency | Check the Redis and object store settings and bus authentication; provision the lab heads stream and consumers before starting the scheduler |
| The scheduler port is reachable but metrics are not | Route port 9108 separately and request `/metrics`; port 8800 serves the scheduler API |
| Capacity shows zero slots or an estimate differs from observed duration | Check worker lease polls and their free slot offers; the load discards stale offers and uses fixed unit estimates |
| Grades returns an empty list | Confirm the full lowercase commit sha and that the lab received and graded that head |

The [node cache DaemonSet](ci-lab-node-cache.md) prepares each CI node's bare mirror and uv
cache. Its reference covers Helm values, placement, refresh stamps, lockfile warming and
per-node verification; a Ready cache pod alone does not prove either cache is warm.

## Environment

| Name | Scheduler | Worker | Meaning |
|---|---|---|---|
| `AGENTIHOOKS_BUS*` | yes | yes | Event bus connection settings |
| `CI_LAB_MINIO_*` | yes | yes | Object store endpoint, bucket and credentials, passed by reference |
| `CI_LAB_SECRET_*` | no | yes | A secret a static unit's step reads by name: `CI_LAB_SECRET_<NAME>` is `${{ secrets.<NAME> }}`, passed by reference and given only to a step whose env reads it (see [What a gate's steps read](#what-a-gates-steps-read)) |
| `GITHUB_APP_*` | yes | never | GitHub App identity used to post check runs and read dev push manifest artifacts; the App needs checks write and actions read |
| `CI_LAB_REDIS_URL` | yes | no | Queue, lease and verdict index store |
| `CI_LAB_REINDEX_ON_START` | yes | no | Only the literal string `true` rebuilds missing verdict index entries from report objects before the listeners start and prints `reindexed N verdicts`; unset or any other value skips it. See [Recovery](#recovery) |
| `CI_LAB_RUNTIME_CONFIG` | yes | no | Path of the mounted runtime config the scheduler reads at start, before it connects anywhere; its budget sets the budget fraction and shed classes of `/capacity` and `/metrics`; `python -m scripts.ci_lab runtime <path>` validates it |
| `CI_LAB_URL` | yes | no | The lab scheduler address the relay builds each relayed check run's and draft pull request's grade link from, `<CI_LAB_URL>/grades/<sha>` |
| `CI_LAB_SCHEDULER_URL` | no | yes | Base address of the scheduler lease API |
| `CI_LAB_SLOTS` | no | yes | Number of units the worker runs at once |
| `CI_LAB_GIT_MIRROR` | yes | yes | Path of the shared git mirror the worker cuts worktrees from and the scheduler borrows objects from |
| `CI_LAB_WORK_ROOT` | no | yes | Folder of the worker's private git store, its trees, venvs and unit runs and reports |
| `CI_LAB_MANIFEST` | no | yes | Legacy manifest path accepted by worker settings; test batches carry their selected dependency records in the lease and do not read this file |
| `CI_LAB_FLAKES` | no | yes | Path of the flake ledger a test unit appends to when its retry passes |
| `CI_LAB_MUTATION_CACHE` | no | yes | Folder of the mutation stats cache, keyed by tree content |
| `CI_LAB_KILLERS` | no | yes | Path of the kill memory that orders a mutant's tests, its remembered killer first |
| `CI_LAB_MUTATION_BUDGET` | no | yes | Seconds a mutant unit may take before its mutants run side by side |
| `CI_LAB_MUTATION_CAP` | no | yes | Most mutants a mutant unit grades at once |
| `CI_LAB_MINIO_ENDPOINT`, `CI_LAB_MINIO_BUCKET`, `CI_LAB_MINIO_ACCESS_KEY`, `CI_LAB_MINIO_SECRET_KEY` | yes | no | The object store names the scheduler reads |
| `CI_LAB_MIRROR_STORE` | yes | no | Folder of the scheduler's private git stores, one per repository, that borrow the mirror's objects |
| `CI_LAB_REPOSITORY` | yes | yes | Repository URL whose dev runner trust list admits head actors; on the worker, the repository its node mirror holds, which the lab remote URL must name |
| `AGENTIHOOKS_LAB_REMOTE`, `AGENTIHOOKS_LAB_REMOTE_TOKEN` | yes | yes | The lab remote URL a mirror fetches a sha GitHub or the node mirror lacks from, and its token, passed by reference; unset, the mirrors fetch from GitHub and the node mirror only. On a workstation or hive the same pair gives workspaces the `lab` remote; see [Workspaces that carry the lab remote](#workspaces-that-carry-the-lab-remote) |
| `CI_LAB_IMAGE` | yes | no | Worker image name keyed into static unit keys; empty when unset |
| `CI_LAB_CONTROL_TOKEN` | yes | no | Bearer token the [control routes](#control-routes) require, passed by reference; unset, they refuse every request. The swarm host that presses panic or merge priority holds the same variable |
| `UV_CACHE_DIR` | no | yes | Shared uv cache for virtual environments |

A worker started with any `GITHUB_APP_*` variable, or any variable with `REDIS` in its name, in its
environment refuses to start. It exits 1 and names each variable it found. Only the scheduler holds the App key.

## Ports

| Port | Service | Serves |
|---|---|---|
| `8800` | scheduler | The lease API workers call, [`/capacity`](#capacity-document) and [`/grades/<sha>`](#grades-of-a-commit); the control paths answer 404 here |
| `8801` | scheduler | The [control routes](#control-routes) a swarm calls, and nothing else |
| `9108` | scheduler and worker | Prometheus metrics at `/metrics` |

## Control routes

The control port, `8801`, serves three routes a swarm calls with `Authorization: Bearer <token>`, the token being the
scheduler's `CI_LAB_CONTROL_TOKEN`. Any other token, or a scheduler without one, answers 401 and changes nothing; any
other path answers 404. The lease port does not serve them, so a network policy can open the control port to hive
pods while the lease port stays closed. A head is a pull request head commit or its head branch. The state lives in
the lab Redis.

| Route | Body | Effect |
|---|---|---|
| `POST /priority` | `{"heads": [...], "class": "hotfix"}` | Marks the heads hotfix for six hours; a marked head is received in the hotfix class, with no quiet window |
| `POST /pause` | `{"class": "<class>", "except": [...]}` | Holds every waiting head of the class but the excepted ones until resume |
| `POST /resume` | none | Lifts every pause and answers the classes it held |

`agentihooks swarm <slug> panic on --fix <url>` marks the fix branches and the recorded fixer branches hotfix and
pauses `pull_request` except them, printing `lab paused pull request grading except <heads>`; `panic off`, and the
release of an automatic panic, resume the lab only when that swarm's panic paused it. `agentihooks swarm <slug> merge priority <url>` marks
the pull request head hotfix and records the lab's reply under `lab` in the queue journal. The swarm sends these
orders to port `8801` on the host of the lab address set with `agentihooks swarm <slug> set lab-url=<address>`, with
the token from `CI_LAB_CONTROL_TOKEN` on its host.

## Metrics

| Name | Labels | Constant |
|---|---|---|
| `ci_lab_units_queued` | `class` | `UNITS_QUEUED` |
| `ci_lab_units_running` | `class` | `UNITS_RUNNING` |
| `ci_lab_slots_total` | | `SLOTS_TOTAL` |
| `ci_lab_slots_busy` | | `SLOTS_BUSY` |
| `ci_lab_slots_wanted` | | `SLOTS_WANTED` |
| `ci_lab_queue_wait_seconds` | `class` | `QUEUE_WAIT_SECONDS` |
| `ci_lab_unit_duration_seconds` | `kind` | `UNIT_DURATION_SECONDS` |
| `ci_lab_cache_hits_total` | `cache` | `CACHE_HITS_TOTAL` |
| `ci_lab_cache_misses_total` | `cache` | `CACHE_MISSES_TOTAL` |
| `ci_lab_mirror_age_seconds` | | `MIRROR_AGE_SECONDS` |
| `ci_lab_core_seconds_total` | `class` | `CORE_SECONDS_TOTAL` |
| `ci_lab_heads_graded_total` | `class`, `conclusion` | `HEADS_GRADED_TOTAL` |
| `ci_lab_budget_fraction` | | `BUDGET_FRACTION` |
| `ci_lab_class_shed` | `class` | `CLASS_SHED` |
| `ci_lab_units_redelivered_total` | | `UNITS_REDELIVERED_TOTAL` |
| `ci_lab_units_lost_total` | | `UNITS_LOST_TOTAL` |
| `ci_lab_units_overdue` | | `UNITS_OVERDUE` |
| `ci_lab_workers_ready` | | `WORKERS_READY` |
| `ci_lab_github_rate_remaining` | | `GITHUB_RATE_REMAINING` |
| `ci_lab_verdict_mismatch_total` | `gate` | `VERDICT_MISMATCH_TOTAL` |

The scheduler sets every metric its queue, lease and worker state answers, the node mirror's age
and the GitHub calls left, on one `scripts.ci_lab.metrics.Registry` the `Scheduler` builds and
`listen` serves on 9108:

| Metric | Set from |
|---|---|
| `ci_lab_slots_wanted` | `scripts.ci_lab.budget.slots_wanted`: the running slots of every class but backfill plus the pending slots of every class but backfill that is not shed, so backfill never raises it |
| `ci_lab_workers_ready` | The workers that offered free slots on a lease poll within `READY_MS` |
| `ci_lab_units_overdue` | Leased units whose lease started, as `Queue.claim` stores it, more than `UNIT_ESTIMATE_S` ago; a lease claimed before the start was stored is not counted |
| `ci_lab_core_seconds_total` | Each finished attempt's slots times its report's `seconds`, counted once (`Queue.tally`) in the most urgent class of the live grades holding its job; an attempt whose worker uploaded no report adds nothing |
| `ci_lab_unit_duration_seconds` | The last finished attempt's report `seconds` per unit kind |
| `ci_lab_cache_hits_total`, `ci_lab_cache_misses_total` | `cache="verdict"`: each verdict store lookup a grade's placement makes; `cache="git"` and `cache="uv"`: the lookups a finished attempt's report carries under `caches` |
| `ci_lab_heads_graded_total` | Each grade a pass closes, by class and conclusion |
| `ci_lab_units_redelivered_total`, `ci_lab_units_lost_total` | Each expired lease a pass requeues or marks lost |
| `ci_lab_mirror_age_seconds` | `metrics.observe_mirror` at each GET of `/metrics` or `/capacity` on 9108: the seconds since the epoch second in the `fetched-at` stamp under `CI_LAB_GIT_MIRROR`, never below zero; no sample while the stamp is missing or holds no whole number. The node cache writes that stamp; the scheduler's own fetch writes none |
| `ci_lab_github_rate_remaining` | `metrics.observe_rate` on every check runs answer the scheduler's and the relay's `Checks` posters get, retries and refusals included: its `x-ratelimit-remaining` header when that holds a whole number; an answer without one keeps the last value |

A worker unit's report carries `caches` from `scripts.ci_lab.caches.report`: per revision it
leased, a git hit when the pod store already held the commit and a miss when `Mirror.fetch`
fetched it; per venv, a uv hit when it reused one and a miss when `venvs.venv_for` built it.
`ci_lab_verdict_mismatch_total` answers from no queue, lease or worker state and stays without
samples. The counters live in the
scheduler process and start from zero when it restarts, so KEDA triggers and alerts read them
through `rate()` or `increase()`.

`METRICS` maps each name to its label names. Environment names have constants too:
`BUS_PREFIX`, `MINIO_PREFIX`, `GITHUB_APP_PREFIX`, `REDIS_URL`, `RUNTIME_CONFIG`,
`SCHEDULER_URL`, `SLOTS`, `GIT_MIRROR` and `UV_CACHE_DIR`, with `SCHEDULER_PORT` and
`METRICS_PORT` for the ports.

[Alerts and dashboard](#alerts-and-dashboard) lists the series the scheduler sets today and the
alerts and panels that read each one.

## Capacity document

`GET /capacity` answers with one JSON document: the lab's queue and slots at one instant and a
predicted queue wait per scheduling class. `scripts.ci_lab.capacity.document` builds it from a
`Load` snapshot, and `scripts.ci_lab.metrics.respond` serves it; a `GET /metrics` on the same
snapshot sets the queue, slot and `ci_lab_queue_wait_seconds` gauges through
`scripts.ci_lab.metrics.observe_load`. The swarm tick reads the document to decide the
[lab behind claim hold](swarm-thresholds.md#lab-behind-claim-hold).

The scheduler service builds the snapshot on every request with
`scripts.ci_lab.scheduler.Scheduler.load` and serves `/capacity` on port 8800 and `/metrics` on
9108 from it, through `scripts.ci_lab.scheduler.listen`:

| Field | Built from |
|---|---|
| `pending` | Each pending queue job, in the most urgent class of the live grades holding it, estimated at `UNIT_ESTIMATE_S` (60 seconds) per slot it takes |
| `running` | Each leased queue job, counted in the same class |
| `slots_busy` | The slots every leased job takes, whether or not a live grade holds it |
| `slots_total` | `slots_busy` plus the free slots each worker offered on its latest lease poll, when that poll is at most `READY_MS` (60 seconds, twice the longest lease wait) old |
| `cache_hits`, `cache_misses` | The live grades' units the verdict store answered, and those it sent to the queue, per class |
| `frees_in` | One `UNIT_ESTIMATE_S` entry per busy slot |
| `running_slots`, `pending_slots` | The slots each leased and each pending job takes, in the class `running` and `pending` count it |
| `workers_ready` | The workers whose latest lease poll is within `READY_MS` |
| `overdue` | The leased jobs whose lease started more than `UNIT_ESTIMATE_S` ago |
| `budget_fraction`, `shed` | The day's core budget from the runtime config, through `scripts.ci_lab.budget.observe` |

A worker's lease poll records its free slots when the poll starts and what its grants leave when
it returns (`Queue.offer`); each load forgets the offers older than the ready window with
`Queue.prune`, one script that reads and deletes, so an offer a worker renews meanwhile stays.
A queue job no live grade holds counts toward the busy slots only. The load reads only the slots
and lease start of each pending and leased job, with `Queue.slots`: HMGET in one pipeline that
opens no transaction, so it builds no job record however long the queue grows.
Until the lab records measured unit durations, every estimate is the fixed `UNIT_ESTIMATE_S`.

### Load snapshot

`scripts.ci_lab.capacity.Load` is a frozen dataclass.

| Field | Default | Meaning |
|---|---|---|
| `pending` | required | Estimated seconds of each queued unit, keyed by class |
| `running` | required | Units running now, keyed by class |
| `slots_total` | required | Slots in the pool |
| `slots_busy` | required | Slots running a unit; `slots_free` is `slots_total - slots_busy`, never below zero |
| `budget_fraction` | `1.0` | Fraction of the day's core budget left; `scripts.ci_lab.budget.observe` sets it |
| `shed` | `()` | Classes the budget sheds; `scripts.ci_lab.budget.observe` sets them |
| `cache_hits`, `cache_misses` | `{}` | Verdict cache lookups, keyed by class |
| `frees_in` | `()` | Seconds until each busy slot frees, one entry per busy slot; an entry at or below zero counts as a slot free now |
| `running_slots`, `pending_slots` | `{}` | Slots running and pending units take, keyed by class; `scripts.ci_lab.budget.slots_wanted` reads them for `ci_lab_slots_wanted` |
| `workers_ready` | `0` | Workers that offered slots within the ready window |
| `overdue` | `0` | Leased units past their estimate |

### Document fields

A pool of two slots, both busy with merge queue units that free in 300 and 60 seconds, with two
pull request units of 120 and 60 seconds queued:

```json
{
  "classes": {
    "hotfix": {"pending": 0, "running": 0, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": null},
    "pull_request": {"pending": 2, "running": 0, "predicted_wait_s": 240.0, "verdict_cache_hit_ratio": null},
    "queue": {"pending": 0, "running": 2, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": null},
    "promotion": {"pending": 0, "running": 0, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": null},
    "backfill": {"pending": 0, "running": 0, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": null}
  },
  "slots": {"total": 2, "busy": 2, "free": 0},
  "utilisation": 1.0,
  "budget_fraction": 1.0,
  "shed": []
}
```

| Field | Meaning |
|---|---|
| `classes` | One entry for every class in `scripts.ci_lab.delivery.CLASSES`, in the order `hotfix`, `pull_request`, `queue`, `promotion`, `backfill` |
| `classes.<class>.pending` | Queued units of the class |
| `classes.<class>.running` | Running units of the class |
| `classes.<class>.predicted_wait_s` | Seconds until the class's queued work drains; `0.0` with nothing queued, `null` when no slot is free and `frees_in` is empty |
| `classes.<class>.verdict_cache_hit_ratio` | Verdict cache hits over lookups; `null` with no lookups |
| `slots` | `total`, `busy` and `free` slots |
| `utilisation` | Busy slots over total slots, at most `1.0`; `0.0` for a pool with no slots |
| `budget_fraction` | As in the snapshot |
| `shed` | Shed classes, in class order |

### Predicted wait

`scripts.ci_lab.capacity.predicted_wait(estimates, free_slots, frees_in)` computes each class's
`predicted_wait_s`:

1. `work` is the sum of the class's pending estimates. With no work the wait is `0.0`.
2. Each free slot starts at 0 seconds and each busy slot at its `frees_in` entry, raised to 0
   when negative. The start times are sorted, earliest first.
3. With the `k` earliest slots working, the work drains at `(work + their k start times) / k`.
   The wait is the least of these over every `k` from one to the number of slots.
4. With no start time at all, no free slot and an empty `frees_in`, the wait is `null`.

The rule shares a class's queued seconds across the slots as one body of work, so it predicts
when that queue drains, not when a given unit starts: one 45 second unit on two slots freeing
in 10 and 20 seconds predicts 37.5 seconds. Each class is predicted against the whole pool on
its own; a queued hotfix does not add to the pull request wait.

| Pending seconds | Free slots | `frees_in` | Wait | How |
|---|---|---|---|---|
| 120, 60 | 0 | 300, 60 | 240 | The slot free at 60 takes all 180 seconds; adding the slot at 300 gives (180 + 360) / 2 = 270, a longer wait |
| 60 | 0 | 300, 100 | 160 | The slot free at 100 alone: 100 + 60 |
| 600 | 0 | 300, 100 | 500 | Both slots share the work: (600 + 100 + 300) / 2 |
| 90 | 1 | 30 | 60 | The free slot and the slot free at 30: (90 + 0 + 30) / 2 |
| 50, 50 | 0 | 40, -30 | 70 | The overdue unit's slot counts as free now: (100 + 0 + 40) / 2 |
| none | 0 | 100 | 0 | Nothing queued |
| 10 | 0 | none | `null` | A full pool with no free time known |

### A wait the lab cannot predict

| Surface | A `null` wait shows as |
|---|---|
| `GET /capacity` | `"predicted_wait_s": null` |
| `ci_lab_queue_wait_seconds{class="<class>"}` | `+Inf` |
| The swarm's `lab_capacity` ClickHouse row | `-1.0` |
| The ledger page Lab stat | `no free slot` beside the class |
| The lab behind claim hold and the `lab_wait` stage finding | 0 minutes, so it never holds a claim |

### How the swarm reads it

Point a swarm at the scheduler with its `lab-url` setting, an `http` or `https` address with a
host and no credentials:

```bash
agentihooks swarm example-swarm set lab-url=http://lab:8800
```

Each tick pass, `scripts.swarm.lab_capacity.capture` reads `<lab-url>/capacity` with a three
second timeout. It stores the swarm's lab block in Redis as `url`, `at`, `capacity`, `error` and
`error_at`, and ships one `lab_capacity` row per class. A failed read keeps the last capacity
and records the error; a changed `lab-url` drops the capacity read from the old address. The
claim hold reads `capacity.classes.pull_request.predicted_wait_s` from that block while it is
at most ten minutes old.

### Verify

The capacity document needs no service to check: build a snapshot and print the pull request
class.

```bash
python -c 'import json; from scripts.ci_lab.capacity import Load, document; print(json.dumps(document(Load(pending={"pull_request": (120.0, 60.0)}, running={"queue": 2}, slots_total=2, slots_busy=2, frees_in=(300.0, 60.0)))["classes"]["pull_request"]))'
```

```json
{"pending": 2, "running": 0, "predicted_wait_s": 240.0, "verdict_cache_hit_ratio": null}
```

Read what a swarm last captured:

```bash
agentihooks swarm example-swarm status --json \
  | jq '.lab | {at, error, slots: .capacity.slots, pull_request: .capacity.classes.pull_request}'
```

The feature map entry `lab-full-pool-wait` lists the regression tests: the drain rule, the full
pool document, the queue wait gauge and the swarm reading the claim hold reads.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| A full lab reads `null`, `+Inf` or `no free slot` and never holds claims | The snapshot carries no `frees_in` | Fill one entry per busy slot with the seconds until the unit on it ends |
| The predicted wait is shorter than the longest queued unit | The rule shares queued seconds across slots | Expected; read it as when the class's queue drains |
| `predicted_wait_s` is `0.0` while every slot is busy | Nothing of that class is queued | Expected; only queued work of the class adds wait |
| A long hotfix queue leaves the pull request wait unchanged | Each class is predicted on its own | Expected; read each class's wait |
| The lab block shows an `error` and an old `at` | The last read failed and the last capacity was kept | Check `lab-url` and that the scheduler answers `/capacity`; a reading older than ten minutes never holds a claim |

## Alerts and dashboard

Nine Prometheus alerts and the CI Lab Grafana dashboard watch the lab. They read the lab
[metrics](#metrics) from the scheduler's metrics port, `9108`, and the kube-state-metrics and
cAdvisor series of the `ci-lab` namespace. Both live in the deployment repository, where they
landed in its pull request 1825 on 18 November 2026. Every alert is a `warning` and reaches the
ntfy warning topic through the cluster's existing Alertmanager.

The alert `CILabVerdictMismatch` shares its name with the [reuse audit](#reuse-audit)'s exception.
The alert reads `ci_lab_verdict_mismatch_total`, which the [shadow comparison](#shadow-comparison)
counts. The exception is raised in Python by `scripts.ci_lab.audit` and never reaches Prometheus.

### Where they live and how they deploy

| File in the deployment repository | What it holds | How it reaches its target |
|---|---|---|
| `k8s/manifests/ops/ci-lab-prometheusrule.yaml` | The PrometheusRule `ci-lab`: one group, `ci-lab`, evaluated every 60 seconds, labelled `release: kube-prometheus-stack` | The `ops-config` Argo CD Application syncs `k8s/manifests/ops` recursively from `dev`, with automated prune and self heal. The `kube-prometheus-stack` Prometheus sets no rule selector, so the chart default loads every PrometheusRule labelled with its release name, as it does the `ci-delivery` rule file beside this one |
| `stacks/observability/provisioning/dashboards/CI/ci-lab.json` | The CI Lab dashboard, uid `ci-lab` | A push to `dev` under `stacks/` runs the `build-and-deploy.yml` workflow, which deploys the observability compose stack. Grafana mounts `provisioning/` read only. Its file provider sets `foldersFromFilesStructure: true`, so the dashboard lands in the `CI` folder |
| `stacks/observability/provisioning/dashboards/CI/ci-delivery.json` | The CI Delivery dashboard, uid `ci-delivery`, with two lab panels added at its foot | As above |
| `tests/prometheus/ci-lab.test.yml` | promtool cases: each alert fires on one input and stays silent on at least one other | The `Prometheus rule tests (promtool)` job of the CI Validate workflow |
| `tests/unit/ci_lab/test_observability.py` | Pins the nine alert names, their two labels, a non-empty summary and description on each, each promtool case's labels and description against its rule, the ntfy route of each, the `absent()` guard, a firing and a silent promtool case per alert, the metric names against a copy of the lab contract, the dashboard uid and datasource, the panel titles, the node cap and the node price | The `Python tests (pytest)` job of the CI Validate workflow |

Both reach their targets on merge into `dev`, with no promotion step. Self heal reverts a hand
edit of the live PrometheusRule, so change the file by pull request.

### Routing

Every rule carries the labels `severity: warning` and `component: ci-lab`. The Alertmanager
configuration sits in the values of the `kube-prometheus-stack` Argo CD Application,
`k8s/argocd/apps/kube-prometheus-stack.yaml`. The parts a lab alert passes through:

```yaml
route:
  receiver: ntfy-alerts
  group_by: [namespace, alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 6h
  routes:
    - receiver: ntfy-critical
      matchers: ['severity = critical']
      continue: false
    - receiver: ntfy-alerts
      matchers: ['severity =~ "warning|info"']
      repeat_interval: 6h
inhibit_rules:
  - source_matchers: ['severity = critical']
    target_matchers: ['severity = warning']
    equal: [namespace, alertname]
```

- `ntfy-alerts` is a webhook receiver that posts to the ntfy warning topic with its alerts
  template. `ntfy-critical` posts to the critical topic at urgent priority.
- The first notice of a group leaves 30 seconds after its first alert, a change to the group
  leaves after 5 minutes, and a group that keeps firing repeats every 6 hours.
- No lab expression keeps a `namespace` label, so the firing series of one alert, such as two
  classes of `CILabQueueWaitHigh`, share one notification.
- No lab alert is `critical`, so the inhibit rule silences none of them. Moving one to the
  critical topic changes its `severity` label, and the unit test that pins `warning` with it.

### The alerts

| Alert | Fires when | Holds for | One alert per | First check |
|---|---|---|---|---|
| `CILabQueueWaitHigh` | The predicted wait of the hotfix, pull request or queue class is over 1200 seconds, the 20 minute default past which the [lab behind claim hold](swarm-thresholds.md#lab-behind-claim-hold) holds new eng claims. A [wait the lab cannot predict](#a-wait-the-lab-cannot-predict) reads `+Inf` and fires it too, while the claim hold reads that wait as 0. Promotion and backfill never fire it | 10 minutes | class | Slots busy against slots total, and whether the worker pool sits at its maximum |
| `CILabWorkersPending` | At least one `ci-lab-worker-*` pod in `ci-lab` is Pending | 15 minutes | the pool | The `cluster-autoscaler-oci-status` ConfigMap for the CI pool, and whether no CI node can make room by preempting pods below a worker's priority, see [Worker scheduling priority](#worker-scheduling-priority) |
| `CILabBudgetShedding` | `ci_lab_class_shed` is 1 for a class: the share of the day's core budget spent reached that class's shed fraction or a more urgent class's, so its units wait for the budget to reset. Only backfill, promotion and queue shed | 5 minutes | class | Budget left on the dashboard and the shed fractions in the runtime config at `CI_LAB_RUNTIME_CONFIG` |
| `CILabPullRequestBlockedByCap` | Pull request units are queued, every slot is busy, and any HorizontalPodAutoscaler in `ci-lab`, in the deployed chart the one KEDA builds from the [worker ScaledObject](#packing), wants at least its maximum replicas. An autoscaler at its maximum in another namespace does not count | 10 minutes | the pool; its value is the queued pull request units | The worker maximum and the CI node cap; raising either is the operator's elasticity budget |
| `CILabSchedulerBlind` | Prometheus holds no `ci_lab_slots_wanted` series while the `ci-lab-scheduler` Deployment asks for at least one replica. A scheduler scaled to zero, or no Deployment, keeps it silent | 10 minutes | the scheduler | The scheduler's scrape target and port `9108`; the worker ScaledObject sizes the worker pool from this gauge and holds `elasticity.floorWorkers` while it is missing |
| `CILabCacheMissRatioHigh` | The git or uv node cache missed more than half its lookups over 30 minutes, with at least 10 misses. A cache with misses and no hit series counts its hits as 0. The verdict cache never fires it | 15 minutes | cache | The node cache DaemonSet on the CI nodes and the age of its git mirror |
| `CILabScaleFlapping` | Over the last hour, sampled every 5 minutes, the worker Deployment's replicas rose against 5 minutes earlier in at least 4 samples and fell in at least 4. One ramp up and down again does not fire it | no hold | the pool; its value is the rising samples | Lengthen the scale down stabilisation window of the worker ScaledObject, `elasticity.scaleDownWindowSeconds`, since each swing churns CI nodes through the autoscaler |
| `CILabUnitsOverdue` | At least one leased unit has run past its estimate | 15 minutes | the scheduler; its value is the overdue units | The worker holding the lease; an expired lease requeues the unit once and loses it on the second expiry, see [Retry policy](#retry-policy) |
| `CILabVerdictMismatch` | A gate's shadow mismatch counter rose in the last hour, or its series first appeared in that hour above 0 | no hold | gate | The mismatch rows of the [shadow report](#report), before the lab grades that gate for merges. It clears an hour after the gate's last new mismatch |

The rule expressions, without labels and annotations:

```yaml
- alert: CILabQueueWaitHigh
  expr: max by (class) (ci_lab_queue_wait_seconds{class=~"hotfix|pull_request|queue"}) > 1200
  for: 10m
- alert: CILabWorkersPending
  expr: sum(kube_pod_status_phase{namespace="ci-lab", pod=~"ci-lab-worker-.+", phase="Pending"}) > 0
  for: 15m
- alert: CILabBudgetShedding
  expr: max by (class) (ci_lab_class_shed) > 0
  for: 5m
- alert: CILabPullRequestBlockedByCap
  expr: >-
    sum(ci_lab_units_queued{class="pull_request"}) > 0
    and on () sum(ci_lab_slots_busy) >= sum(ci_lab_slots_total)
    and on () max(kube_horizontalpodautoscaler_status_desired_replicas{namespace="ci-lab"})
    >= max(kube_horizontalpodautoscaler_spec_max_replicas{namespace="ci-lab"})
  for: 10m
- alert: CILabSchedulerBlind
  expr: >-
    absent(ci_lab_slots_wanted)
    and on () sum(kube_deployment_spec_replicas{namespace="ci-lab", deployment="ci-lab-scheduler"}) > 0
  for: 10m
- alert: CILabCacheMissRatioHigh
  expr: >-
    sum by (cache) (increase(ci_lab_cache_misses_total{cache=~"git|uv"}[30m]))
    / (
    sum by (cache) (increase(ci_lab_cache_misses_total{cache=~"git|uv"}[30m]))
    + (
    sum by (cache) (increase(ci_lab_cache_hits_total{cache=~"git|uv"}[30m]))
    or 0 * sum by (cache) (increase(ci_lab_cache_misses_total{cache=~"git|uv"}[30m]))
    )
    ) > 0.5
    and on (cache) sum by (cache) (increase(ci_lab_cache_misses_total{cache=~"git|uv"}[30m])) >= 10
  for: 15m
- alert: CILabScaleFlapping
  expr: >-
    count_over_time((max(kube_deployment_spec_replicas{namespace="ci-lab", deployment="ci-lab-worker"})
    - max(kube_deployment_spec_replicas{namespace="ci-lab", deployment="ci-lab-worker"} offset 5m) > 0)[1h:5m]) >= 4
    and on ()
    count_over_time((max(kube_deployment_spec_replicas{namespace="ci-lab", deployment="ci-lab-worker"})
    - max(kube_deployment_spec_replicas{namespace="ci-lab", deployment="ci-lab-worker"} offset 5m) < 0)[1h:5m]) >= 4
- alert: CILabUnitsOverdue
  expr: max(ci_lab_units_overdue) > 0
  for: 15m
- alert: CILabVerdictMismatch
  expr: >-
    sum by (gate) (increase(ci_lab_verdict_mismatch_total[1h])) > 0
    or sum by (gate) (ci_lab_verdict_mismatch_total unless ci_lab_verdict_mismatch_total offset 1h) > 0
```

Each rule also carries a `summary` and a `description` annotation, and the promtool cases compare
both exactly. A firing `CILabPullRequestBlockedByCap` with four queued pull request units reads:

```text
summary: 4 pull request units wait while every lab slot is busy and the worker pool is at its cap
description: The lab cannot add workers for pull request heads because the KEDA worker pool sits
  at its maximum replicas. The node cap and the floor are the operator's elasticity budget.
```

### Lab series the rules read

Only the scheduler sets lab series. The worker serves `/metrics` on 9108 through
`metrics.serve_registry` from a registry of its own that observes nothing, so its exposition holds
the `HELP` and `TYPE` lines and no sample: a worker scrape target reads up without doubling the
scheduler's series, which already count every unit from its report. `scripts.ci_lab.scheduler.listen`
binds the metrics port with a fresh `metrics.Registry`, and every `GET /metrics` sets these from
the scheduler's load snapshot through `metrics.observe_load`:

- `ci_lab_units_queued`, `ci_lab_units_running`, `ci_lab_class_shed` and
  `ci_lab_queue_wait_seconds`, once for each of the five classes
- `ci_lab_slots_total`, `ci_lab_slots_busy` and `ci_lab_budget_fraction`

On agentihooks `dev` of 18 November 2026, no service code sets the other contract metrics. The
registry writes their `HELP` and `TYPE` lines with no sample, so Prometheus stores no series for
them:

| Series | Read by | Until a service sets it |
|---|---|---|
| `ci_lab_slots_wanted` | `CILabSchedulerBlind`, the slots panels, the worker ScaledObject | `CILabSchedulerBlind` fires 10 minutes after a `ci-lab-scheduler` Deployment with a replica appears, and the `wanted` lines stay empty. The scheduler sets it from `scripts.ci_lab.budget.slots_wanted` on every scrape |
| `ci_lab_units_overdue` | `CILabUnitsOverdue` | The alert cannot fire |
| `ci_lab_workers_ready` | Workers ready against desired | The `heartbeat` line stays empty |
| `ci_lab_core_seconds_total`, `ci_lab_heads_graded_total` | Core minutes per merged pull request | The stat reads No data |
| `ci_lab_cache_hits_total`, `ci_lab_cache_misses_total` | `CILabCacheMissRatioHigh`, Cache hit ratio | `Scheduler._place` counts the `verdict` cache and `metrics.observe_unit` counts the `git` and `uv` lookups a unit report carries, so the alert and panel wait only on a running scheduler |
| `ci_lab_verdict_mismatch_total` | `CILabVerdictMismatch`, Shadow mismatches per gate | `shadow.Shadow` counts it in a registry of its own and has no live caller, so the alert cannot fire |
| `ci_lab_unit_duration_seconds`, `ci_lab_mirror_age_seconds`, `ci_lab_github_rate_remaining` | No alert or panel | Nothing, since no alert or panel reads them. `metrics.observe_unit` sets the unit duration for each finished attempt whose report carries `seconds`, `metrics.observe_mirror` the mirror age at each read of the metrics port and `metrics.observe_rate` the GitHub calls left from each check runs answer |

`Scheduler._reap` counts `ci_lab_units_redelivered_total` and `ci_lab_units_lost_total` on the
registry the metrics port serves, one for each expired lease it requeues or marks lost. No alert or
panel reads them; the [Worker loss drill](#worker-loss-drill) reads them.

The kube-state-metrics series need the lab chart's objects in the `ci-lab` namespace: the
`ci-lab-scheduler` and `ci-lab-worker` Deployments, the worker pods, and the KEDA ScaledObject
with the HorizontalPodAutoscaler KEDA builds from it. Prometheus reaches port `9108` only through
a ServiceMonitor for the scheduler. The deployment repository's chart ships all of them: the
scheduler Deployment since its pull request 1818, the worker Deployment since 1826, the
ScaledObject since 1834 and the scheduler's ServiceMonitor since 1841.

### CI Lab dashboard

The dashboard has the uid `ci-lab`, the title `CI Lab`, the tags `ci` and `lab`, UTC time, a one
minute refresh and the last six hours as its default range. Every panel queries the Prometheus
datasource with the uid `prometheus`. Open it at `<Grafana root URL>/d/ci-lab`, where the root URL
is the `GRAFANA_ROOT_URL` of the observability stack. A hidden constant variable, `ci_node_cap`,
holds `7`, the CI pool maximum.

| Panel | Type | Shows |
|---|---|---|
| Spend estimate per day | stat, EUR | CI nodes now times EUR 0.308 per node hour times 24. A CI node carries both the `workload=ci` taint and the `remote=oci` taint; both keys sit under the cluster's own label domain, written `<label domain>` below because this page does not name it. At the cap of 7 nodes it reads EUR 51.74 |
| Core minutes per merged pull request | stat, minutes | Lab core minutes of every class over the dashboard range, divided by the queue heads graded green in that range. The panel's description gives the reason: GitHub builds one merge group head per queued pull request, so each green queue head counts as one merged pull request |
| Budget left | stat, percent | `max(ci_lab_budget_fraction)` |
| Queue wait by class | time series, seconds | Predicted wait per class, capped at 3600 so a `+Inf` wait plots at one hour, with a red threshold line at 1200, the `CILabQueueWaitHigh` level |
| Slots busy, total and wanted | time series | `ci_lab_slots_busy`, `ci_lab_slots_total` and `ci_lab_slots_wanted` |
| Workers ready against desired | time series | The worker Deployment's spec replicas as `desired`, its ready replicas as `ready`, and `ci_lab_workers_ready` as `heartbeat` |
| CI nodes against the cap | time series | The CI node count against `vector($ci_node_cap)` |
| Cache hit ratio (git, uv, verdict) | time series, percent | Hits over lookups in the last hour per cache; a cache missing one side counts that side as 0 |
| Worker CPU use over requests | time series, percent | The 5 minute CPU rate of the worker containers over their CPU requests |
| Budget fraction and shed classes | time series | Budget left, and a `<class> shed` line per class that reads 1 while the class sheds |
| Shadow mismatches per gate | time series | The rise of `ci_lab_verdict_mismatch_total` over the last hour per gate |

The four queries that carry more than one series:

```promql
count(kube_node_spec_taint{key="<label domain>/workload", value="ci"} and on (node) kube_node_spec_taint{key="<label domain>/remote", value="oci"}) * 0.308 * 24

sum(increase(ci_lab_core_seconds_total[$__range])) / 60 / sum(increase(ci_lab_heads_graded_total{class="queue", conclusion="success"}[$__range]))

(sum by (cache) (increase(ci_lab_cache_hits_total[1h])) or 0 * sum by (cache) (increase(ci_lab_cache_misses_total[1h]))) / ((sum by (cache) (increase(ci_lab_cache_hits_total[1h])) or 0 * sum by (cache) (increase(ci_lab_cache_misses_total[1h]))) + (sum by (cache) (increase(ci_lab_cache_misses_total[1h])) or 0 * sum by (cache) (increase(ci_lab_cache_hits_total[1h]))))

sum(rate(container_cpu_usage_seconds_total{namespace="ci-lab", pod=~"ci-lab-worker-.+", container!="", container!="POD"}[5m])) / sum(kube_pod_container_resource_requests{namespace="ci-lab", pod=~"ci-lab-worker-.+", resource="cpu"})
```

The CI Delivery dashboard gained two panels at its foot, `Lab slots busy, total and wanted` and
`Lab queue wait by class`. They run the same queries as the CI Lab panels of nearly the same name,
without the threshold line.

### Change a threshold, the cap or the price

Every change is a pull request into the deployment repository's `dev`.

- **A threshold, hold or class list.** Edit the rule and the promtool cases that pin it in the
  same pull request. A case whose `exp_alerts` no longer match fails the promtool job, annotations
  included.
- **The CI node cap.** `ci_node_cap` must equal `<max>` in the
  `--nodes=0:<max>:$(OCI_INSTANCE_POOL_ID)` flag of
  `k8s/manifests/cluster-autoscaler-oci/deployment.yaml`.
  `test_the_node_cap_is_the_oci_ci_pool_maximum_in_the_autoscaler` fails when they differ, so a
  cap change edits both.
- **The node price.** The spend expression must end in `* <price> * 24`, where `<price>` comes
  from the `| price | EUR <price>/h per node` row of `docs/2H-OCI-AUTOSCALING.md`.
  `test_the_spend_estimate_prices_ci_nodes_for_a_day_at_the_documented_euro_rate` reads that row.
  That row reads EUR 0.308 per node hour, an E6 node with a 600 GB boot volume.
- **A panel.** Edit the dashboard JSON. The provider leaves `allowUiUpdates` unset, so Grafana
  refuses to save a change made in its editor over a provisioned dashboard; export the JSON from
  the editor and commit it instead.

### Use it

Run the promtool cases as the CI job does, from a deployment repository checkout with Docker:

```bash
work="$(mktemp -d)"
cp tests/prometheus/ci-lab.test.yml "$work/"
python3 - "$work" <<'PY'
import pathlib, sys, yaml
out = pathlib.Path(sys.argv[1])
rule = yaml.safe_load(pathlib.Path("k8s/manifests/ops/ci-lab-prometheusrule.yaml").read_text())
(out / "ci-lab.rules.yml").write_text(yaml.safe_dump({"groups": rule["spec"]["groups"]}))
PY
docker run --rm -v "$work:/w" -w /w --entrypoint promtool prom/prometheus:v3.4.1 test rules ci-lab.test.yml
```

The test file names `ci-lab.rules.yml`, so the rule groups are written there first. promtool
prints `SUCCESS` for the file, or `FAILED:` with each case that missed and the alerts it got.

List the series the scheduler's metrics port sets, with no service, from an agentihooks checkout:

```bash
python -c 'from scripts.ci_lab import metrics; from scripts.ci_lab.capacity import Load; r = metrics.Registry(); metrics.observe_load(r, Load(pending={"pull_request": (120.0,)}, running={"queue": 1}, slots_total=4, slots_busy=1, frees_in=(60.0,))); print(sorted({line.split("{")[0].split(" ")[0] for line in r.render().splitlines() if not line.startswith("#")}))'
```

```text
['ci_lab_budget_fraction', 'ci_lab_class_shed', 'ci_lab_queue_wait_seconds', 'ci_lab_slots_busy', 'ci_lab_slots_total', 'ci_lab_units_queued', 'ci_lab_units_running']
```

### Verify

The rule object, then the group as Prometheus loaded it:

```bash
kubectl get prometheusrule -A --field-selector metadata.name=ci-lab
curl -fsS "$PROMETHEUS_URL/api/v1/rules?type=alert" \
  | jq -r '.data.groups[] | select(.name == "ci-lab") | .rules[] | "\(.name) \(.health) \(.state)"'
```

Nine lines answer, one per alert, each `ok` with its state: `inactive`, `pending` or `firing`.
A missing group means the rule did not load.

The lab alerts Alertmanager holds now:

```bash
curl -fsS "$ALERTMANAGER_URL/api/v2/alerts?filter=component%3D%22ci-lab%22" \
  | jq -r '.[] | "\(.labels.alertname) \(.labels.class // .labels.cache // .labels.gate // "-") \(.status.state)"'
```

The dashboard, with a Grafana service account token:

```bash
curl -fsS -H "Authorization: Bearer $GRAFANA_TOKEN" "$GRAFANA_ROOT_URL/api/dashboards/uid/ci-lab" \
  | jq '{title: .dashboard.title, folder: .meta.folderTitle, provisioned: .meta.provisioned, panels: (.dashboard.panels | length)}'
```

```json
{"title": "CI Lab", "folder": "CI", "provisioned": true, "panels": 11}
```

For pull request 1825, the deployment repository's Actions run 44258703320 passed the promtool job
on both rule test files, and its `build-and-deploy.yml` run 44263157474 on the merge commit
deployed the observability stack.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `kubectl get prometheusrule` lists no `ci-lab` | `ops-config` has not synced the merge, or its sync failed | `kubectl -n argocd get application ops-config -o jsonpath='{.status.sync.status} {.status.sync.revision}{"\n"}'`; the revision must be a `dev` commit holding the rule file |
| The object exists but the rules API has no `ci-lab` group | The `release: kube-prometheus-stack` label is missing or changed, so the Prometheus rule selector skips the object | Restore the label; `test_the_rule_file_holds_exactly_the_nine_lab_alerts` pins it |
| Every lab alert stays inactive and the lab panels read No data | Prometheus holds no lab series: the lab chart's objects and the scheduler's ServiceMonitor are not in the cluster | `count by (__name__) ({__name__=~"ci_lab_.+"})` in Prometheus lists the lab series it holds |
| `CILabSchedulerBlind` fires 10 minutes after a `ci-lab-scheduler` replica exists | No service code sets `ci_lab_slots_wanted`, or the scrape target is down | `up` for the scheduler target. With the target up, `curl -fsS http://<scheduler>:9108/metrics \| grep '^ci_lab_slots_wanted'` prints nothing until a service sets the gauge |
| Spend estimate reads No data | No node carries both CI taints, and `count` over no series is empty rather than 0 | Expected while the CI pool has no node |
| Core minutes per merged pull request reads No data or Infinity | The core and graded head counters are not exported, or no queue head was graded green in the range | See [Lab series the rules read](#lab-series-the-rules-read); widen the range |
| `CILabVerdictMismatch` keeps firing after the gate agrees again | It looks back one hour | Expected; it clears an hour after the last new mismatch of that gate |
| The promtool job fails after an annotation edit | promtool compares `summary` and `description` exactly | Update the case's `exp_annotations` in the same pull request |
| A firing lab alert sends no second ntfy message | A group that keeps firing repeats only every 6 hours | Read the alert in the Alertmanager API above; a change to the group sends after 5 minutes |

## Worker Deployment

The deployment repository's `ci-lab` chart runs the [worker service](#worker-executors) as the
`ci-lab-worker` Deployment in the `ci-lab` namespace, beside a NetworkPolicy of the same name.
Both landed in that repository's pull request 1826 on 19 November 2026, in
`k8s/charts/ci-lab/templates/worker-deployment.yaml` and `templates/worker-networkpolicy.yaml`,
with the `worker` block of the chart's `values.yaml` and the `workersPerNode`, `nodeMax` and
`nodeReserve` keys of its `elasticity` block. Pull request 1834 of the same day added the KEDA
ScaledObject in `templates/scaledobject.yaml`, the other `elasticity` keys and the condition that
drops `replicas` from the Deployment while elasticity is on. The `ci-lab` Argo CD Application
syncs `k8s/charts/ci-lab` from that repository's `dev` branch with automated prune and self heal,
so a chart change reaches the cluster when it merges into `dev`.

A worker pod takes 5 of a CI node's 16 vCPUs and 8 GiB of its memory, runs only on the CI node
pool, holds no credential, writes only to its own `emptyDir` folders and reaches only cluster
DNS, the scheduler and the object store.

### Placement

| Pod spec field | Value | Effect |
|---|---|---|
| `schedulerName` | `runner-binpack` | The `runner-binpack-scheduler` Deployment in `kube-system` schedules the pod instead of the default scheduler. Its one profile scores `NodeResourcesFit` with the `MostAllocated` strategy over CPU and memory, weight 1 each, at plugin weight 10, and disables `NodeResourcesBalancedAllocation` and `PodTopologySpread` scoring, so a worker goes to the fullest CI node it fits and workers fill one node before the next |
| `priorityClassName` | `ci-lab-worker-preempt` | Priority 250, ahead of every CI runner, and a pending worker may preempt lower priority pods such as runners; see [Worker scheduling priority](#worker-scheduling-priority) |
| `nodeSelector` | `<domain>/node-pool: oci-ci` and `<domain>/workload: ci` | Only nodes of the OCI CI pool; both labels must match |
| `tolerations` | `<domain>/remote=oci` and `<domain>/workload=ci`, each with `operator: Equal` and `effect: NoSchedule` | Lets the pod onto the tainted CI pool nodes |
| `affinity.podAntiAffinity` | `preferredDuringSchedulingIgnoredDuringExecution`, weight 100, `topologyKey: kubernetes.io/hostname`, `namespaceSelector: {}`, label key `actions.github.com/scale-set-name` with `operator: Exists` | Prefers a node with no Actions Runner Controller runner pod from any namespace. It is one score among the scheduler's, so a worker still lands beside runners when that node scores higher or no other node fits |

`<domain>` stands for the deployment's label domain. The selector and the tolerations equal the
[node cache DaemonSet](ci-lab-node-cache.md#placement-and-security)'s, so every node that can take
a worker also runs a node cache pod. They are template settings, not Helm values; read the exact
keys from the running Deployment:

```bash
kubectl -n ci-lab get deploy ci-lab-worker \
  -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}{.spec.template.spec.tolerations}{"\n"}'
```

### Packing

The pod's one container requests what it is limited to:

| Resource | Request | Limit |
|---|---|---|
| `cpu` | `"5"` | `"5"` |
| `memory` | `8Gi` | `8Gi` |
| `ephemeral-storage` | `20Gi` | `20Gi` |

Equal CPU and memory requests and limits give the pod the `Guaranteed` QoS class. The worker
runs as many slots as its CPU quota has whole cores, at most `CI_LAB_SLOTS`; both are 5, one slot
per core, so a worker runs at most five units at once.

The CI node shape comes from the `ci` entry of the OCI runner pools in the deployment
repository's Terraform, `stacks/terraform/modules/oci-arc-runners/variables.tf`: `ocpus = 8`,
which is 16 vCPUs, and `memory_gbs = 28`, which the packing test counts as 28 GiB.
`elasticity.workersPerNode` workers plus `elasticity.nodeReserve` fill that node, and one more
worker would not fit:

| On one CI node | CPU | Memory |
|---|---|---|
| Three workers | 15 | 24 GiB |
| `elasticity.nodeReserve` | 1 | 2 GiB |
| Total | 16 of 16 | 26 of 28 GiB |
| Four workers and the reserve | 21, over 16 | 34 GiB, over 28 |

The reserve holds the node's DaemonSets. Each node cache container requests 50m CPU and 128 MiB.
Measured on a CI node in pull request 1826, the DaemonSet pods there requested 41m CPU and
120 MiB; with the node cache's 50m and 128 MiB added, about 91m and 248 MiB. The node's
allocatable equalled its capacity of 16 CPU and 27.4 GiB.

The worker count:

| `elasticity.enabled` | Replicas |
|---|---|
| `true`, the shipped value | The Deployment carries no `replicas`. The `ci-lab-worker` KEDA ScaledObject sizes it from `max_over_time(sum(ci_lab_slots_wanted)[2m:15s])` at a target of `worker.slots` per worker, between `elasticity.floorWorkers` (1) and `workersPerNode × nodeMax` (3 × 7 = 21), and holds `floorWorkers` after `elasticity.fallbackFailures` (3) failed reads of that query |
| `false` | `worker.replicas` (3), one node of workers |

`elasticity.nodeMax` (7) is the CI pool maximum of the OCI cluster autoscaler, the middle field
of its `--nodes=0:7:$(OCI_INSTANCE_POOL_ID)` flag in
`k8s/manifests/cluster-autoscaler-oci/deployment.yaml`, so the worker ceiling is three workers on
every node the autoscaler may run. Each scale up step adds at most `workersPerNode` workers, one
node of them, per `elasticity.scaleUpPeriodSeconds`. The other `elasticity` values tune the
ScaledObject's polling and scale down.

### Isolation

The pod holds no credential:

| Setting | Value | Keeps out |
|---|---|---|
| `automountServiceAccountToken` | `false` | A Kubernetes API token |
| `enableServiceLinks` | `false` | The `*_SERVICE_HOST` and `*_PORT` variables of the namespace's Services |
| `serviceAccountName`, `imagePullSecrets`, `initContainers` and `envFrom` | absent | A named account, a pull secret, init steps and bulk environment; the worker image pulls from GHCR without a secret |
| `env` | literal `name` and `value` pairs only | Any `valueFrom` reference |
| `volumes` | `emptyDir` and `hostPath` only | Secret, ConfigMap and projected volumes |

On top of that, the worker refuses at start any variable with the `GITHUB_APP_` prefix or `REDIS`
in its name: it raises `Refused` with `ci-lab worker refuses <names>` and exits 1. Its environment
is exactly the worker contract:

| Variable | Value |
|---|---|
| `CI_LAB_SCHEDULER_URL` | `http://ci-lab-scheduler:8800` |
| `CI_LAB_SLOTS` | `worker.slots`, `5` |
| `CI_LAB_GIT_MIRROR` | `/cache/git/agentihooks.git` |
| `UV_CACHE_DIR` | `/cache/uv` |
| `CI_LAB_WORK_ROOT` | `/work` |
| `CI_LAB_MANIFEST` | `/work/manifest.json` |
| `CI_LAB_FLAKES` | `/work/flakes.jsonl` |
| `CI_LAB_MUTATION_CACHE` | `/work/mutation-cache` |
| `CI_LAB_KILLERS` | `/work/killers` |
| `CI_LAB_MUTATION_BUDGET` | `worker.mutation.budgetSeconds`, `1800` |
| `CI_LAB_MUTATION_CAP` | `worker.mutation.cap`, `1` |

`scripts.ci_lab.executors.settings` reads `CI_LAB_GIT_MIRROR` and the last seven without a
default, and `scripts.ci_lab.worker.build` reads `CI_LAB_SCHEDULER_URL` the same way, so a pod
missing one fails at start with a `KeyError`. A mutant unit leases one slot, and the cap of 1
keeps its mutants inside it.
[Environment](#environment) gives the meaning of each variable.

| Level | Security setting |
|---|---|
| Pod | `runAsNonRoot: true`; `runAsUser` and `runAsGroup` from `worker.uid`, `1001`, the node cache's `nodeCache.uid` and the owner of the cache folders; `seccompProfile.type: Unconfined` |
| Container | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]` |
| Pod, unset | `hostNetwork`, `hostPID` and `hostIPC` |

Seccomp stays `Unconfined` for the [unit sandbox](#unit-sandbox). containerd's default profile
allows `unshare` and `setns` only to a process holding `CAP_SYS_ADMIN`, and `clone` only without
namespace flags, while bubblewrap needs those calls to give each unit its own namespaces, so
`RuntimeDefault` would refuse every sandboxed unit. The node cache DaemonSet keeps
`RuntimeDefault`.

The root filesystem is read only, so the worker writes only to its two `emptyDir` mounts:

| Mount | Volume | Access | Holds |
|---|---|---|---|
| `/work` | `work`, `emptyDir` | read and write | The worker's private git store, trees, venvs, unit runs and reports, and the manifest, flake ledger, mutation cache and kill memory paths above |
| `/tmp` | `temporary`, `emptyDir` | read and write | Temporary files |
| `/cache/git` | `git`, hostPath `<nodeCache.path>/git` of type `Directory` | read only | The node's bare mirror; the private store under `/work` borrows its objects through `objects/info/alternates` |
| `/cache/uv` | `uv`, hostPath `<nodeCache.path>/uv` of type `Directory` | read only | The node's uv cache |

Neither `emptyDir` sets a `sizeLimit`, so their use counts against the pod's 20 GiB
`ephemeral-storage` limit together with the container's logs, and the kubelet evicts the pod past
it. Both start empty in every new pod. The `Directory` type makes the kubelet refuse a host folder
that does not exist yet: on a node where the node cache `prepare` container has not created `git`
and `uv`, the worker waits in `ContainerCreating` while the kubelet retries the mount.

### Stop and rollout

| Setting | Value | Effect |
|---|---|---|
| `cluster-autoscaler.kubernetes.io/safe-to-evict` | `"false"`, the pod template's only annotation | The cluster autoscaler never removes a node that runs a worker; a CI node goes only after its workers have. A scale down of the Deployment removes idle workers first, see [Worker pod deletion cost](#worker-pod-deletion-cost) |
| `terminationGracePeriodSeconds` | `worker.terminationGracePeriodSeconds`, `2100` | How long the kubelet waits after SIGTERM before it kills the container. The worker's drain window is fixed in code, `worker.GRACE_SECONDS`, 2100 seconds: a stopping worker stops leasing and drains its running units for up to that long, see [Worker executors](#worker-executors). A Helm value below 2100 kills a worker mid drain |
| `strategy` | `RollingUpdate` with `maxSurge: 0` and `maxUnavailable: 1` | A rollout adds no surge pod, so it asks the autoscaler for no extra node, and takes one worker down at a time; a busy worker holds its step for up to the grace |

### Network

The `ci-lab-worker` NetworkPolicy selects the worker pods by `app.kubernetes.io/name: ci-lab`,
`app.kubernetes.io/instance: <release>` and `app.kubernetes.io/component: worker`, and lists both
`Ingress` and `Egress` in `policyTypes`, so only these rules pass:

| Direction | Peer | Port | For |
|---|---|---|---|
| Egress | `k8s-app: kube-dns` pods in `kube-system` | UDP and TCP 53 | Name lookups, such as `ci-lab-scheduler` |
| Egress | The chart's scheduler pods in the release namespace | TCP 8800 | The lease API: lease polls, heartbeats, upload URLs and completions |
| Egress | Pods matching `minio.pods`, `app: minio`, in the namespace `minio.namespace` | TCP `minio.port`, 9000 | The PUTs of each unit's report and log to the presigned URLs the scheduler hands out; see [Worker uploads](#worker-uploads) |
| Ingress | `app.kubernetes.io/name: prometheus` pods in `monitoring` | TCP 9108 | The worker's `/metrics` |

The policy has no `ipBlock`. GitHub, package indexes, the lab remote, the bus, Redis, the
Kubernetes API server and every other private range get no route from a worker. Pull request 1826
probed this shape live with a temporary pod on a CI node: under a policy allowing only DNS and the
MinIO pods on 9000, MinIO's health endpoint answered 200 through its load balancer address,
because that address is translated to the MinIO pod before the policy matches, while MinIO's
console port and another LAN service were refused.

### Configuration

| Helm value | Shipped | Effect |
|---|---|---|
| `worker.image.repository` | `ghcr.io/the-cloud-clockwork/agentihooks-ci-lab-worker` | Worker image, pulled with `imagePullPolicy: Always`; the scheduler's `CI_LAB_IMAGE` names the same image |
| `worker.image.tag` | `dev` | Image tag |
| `worker.replicas` | `3` | Replicas when `elasticity.enabled` is `false` |
| `worker.slots` | `5` | `CI_LAB_SLOTS` and the ScaledObject's target per worker |
| `worker.uid` | `1001` | Pod UID and GID; keep it equal to `nodeCache.uid` |
| `worker.terminationGracePeriodSeconds` | `2100` | The kubelet's stop grace; keep it at or above the worker's 2100 second drain |
| `worker.mutation.budgetSeconds` | `1800` | `CI_LAB_MUTATION_BUDGET` |
| `worker.mutation.cap` | `1` | `CI_LAB_MUTATION_CAP` |
| `worker.resources` | 5 CPU, `8Gi` and `20Gi` ephemeral, requests equal to limits | Container resources, copied as written |
| `elasticity.enabled` | `true` | Renders the ScaledObject and drops `replicas` from the Deployment |
| `elasticity.floorWorkers` | `1` | ScaledObject minimum and fallback replicas |
| `elasticity.workersPerNode` | `3` | Workers per CI node: a factor of the ScaledObject maximum and its scale up step |
| `elasticity.nodeMax` | `7` | CI pool node maximum: the other factor of the ScaledObject maximum |
| `elasticity.pollingInterval` | `15` | Seconds between the ScaledObject's trigger reads |
| `elasticity.fallbackFailures` | `3` | Failed trigger reads before the ScaledObject holds `floorWorkers` |
| `elasticity.scaleUpPeriodSeconds` | `60` | Period of the scale up step of `workersPerNode` workers, with no stabilisation window |
| `elasticity.scaleDownWindowSeconds` | `300` | Scale down stabilisation window |
| `elasticity.scaleDownPodsPerMinute` | `2` | Most workers removed per 60 seconds |
| `elasticity.prometheusAddress` | `http://kube-prometheus-stack-prometheus.monitoring.svc:9090` | Prometheus the trigger queries |
| `elasticity.nodeReserve.cpu`, `elasticity.nodeReserve.memory` | `"1"`, `2Gi` | Room left on a CI node for its DaemonSets; only the packing test reads it |
| `nodeCache.path` | The deployment's host cache root | Root of the two hostPath mounts |
| `minio.namespace`, `minio.pods`, `minio.port` | The object store's namespace, `app: minio`, `9000` | The MinIO egress rule |

Change these through a pull request into the deployment repository's `dev`. Its tests hold these
relations, so a change that breaks one fails there:

- `worker.slots` equals the whole cores of `worker.resources.limits.cpu`, and requests equal limits.
- `workersPerNode` is 3; `workersPerNode` workers plus the reserve fit the CI node's vCPUs and
  memory, and one more worker does not fit its vCPUs.
- `nodeMax` equals the autoscaler flag's maximum; change both in one pull request.
- `worker.uid` equals `nodeCache.uid`.
- `worker.terminationGracePeriodSeconds` is at least 1800 seconds of unit time plus 300 seconds
  for the five delivery calls.

For example, save this override as `ci-lab-workers.example.yaml` to render a fixed pool of three
workers:

```yaml
elasticity:
  enabled: false
worker:
  replicas: 3
  slots: 5
  uid: 1001
  terminationGracePeriodSeconds: 2100
  resources:
    requests:
      cpu: "5"
      memory: 8Gi
      ephemeral-storage: 20Gi
    limits:
      cpu: "5"
      memory: 8Gi
      ephemeral-storage: 20Gi
```

Render the worker objects from the deployment checkout:

```bash
helm template ci-lab k8s/charts/ci-lab --namespace ci-lab \
  -f ci-lab-workers.example.yaml \
  --show-only templates/worker-deployment.yaml \
  --show-only templates/worker-networkpolicy.yaml
```

The output holds the Deployment, with `replicas: 3` because the override turns elasticity off,
and the NetworkPolicy.

### Guard tests

The deployment repository's unit tests render the chart with Helm and read the manifests the
values must match:

| Test | Holds |
|---|---|
| `tests/unit/ci_lab/test_packing.py` | Requests equal limits at 5 CPU, `8Gi` and `20Gi`, whole cores and `CI_LAB_SLOTS` equal to them; three workers plus the reserve fit the Terraform `ci` node and a fourth does not, and the node cache requests fit inside the reserve; `nodeMax` equals the autoscaler's CI pool maximum, and with elasticity off the replicas equal `workersPerNode`; the scheduler name is a `runner-binpack` profile, the priority class is defined, the node selector equals the node cache's and the Terraform pool's labels, the tolerations equal the node cache's, and the anti affinity is exactly the one above |
| `tests/unit/ci_lab/test_scaledobject.py` | Among its ScaledObject checks, `maxReplicaCount` equals `workersPerNode × nodeMax` |
| `tests/unit/ci_lab/test_worker_isolation.py` | No token, service links, account, pull secret, init container or `envFrom`, only `emptyDir` and hostPath volumes; the environment equals the contract above; the image equals the scheduler's `CI_LAB_IMAGE`; the security contexts above with the node cache's UID and no host namespaces; the writable and read only mounts above; `safe-to-evict` as the only annotation, a grace of at least 1800 plus 300 seconds and the rollout strategy |
| `tests/unit/ci_lab/test_network.py` | The policy selects the worker pod labels in both directions; exactly three egress rules, DNS, the scheduler on 8800 and MinIO on 9000, with no `ipBlock`; one ingress rule, the monitoring Prometheus on 9108, matching the container's `metrics` port |

### Verify

List the workers and their nodes:

```bash
kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker -o wide
```

The binpack score sends each worker to the fullest CI node it fits. The floor,
`elasticity.floorWorkers`, is one worker. With three workers and one CI node that has room, expect
all three on the same `NODE`. The chart does not force it: a worker scheduled while that node was
full goes elsewhere. Read the node's requests:

```bash
node=$(kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker \
  -o jsonpath='{.items[0].spec.nodeName}')
kubectl describe node "$node" | sed -n '/^Allocated resources:/,/^Events:/p'
```

Computed from the requests, three workers put the `cpu` requests line at about 15.1 of the
16 CPU, 94 percent: 15 from the workers and about 91m from the DaemonSets that pull request
1826 measured. [Worker delivery evidence](#worker-delivery-evidence) lists what has been recorded live.

Check each worker's scheduler, priority and QoS class:

```bash
kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker \
  -o custom-columns='NAME:.metadata.name,SCHEDULER:.spec.schedulerName,CLASS:.spec.priorityClassName,PRIORITY:.spec.priority,QOS:.status.qosClass'
```

Every row reads `runner-binpack`, `ci-lab-worker-preempt`, `250` and `Guaranteed`. Read the policy
as the cluster holds it:

```bash
kubectl -n ci-lab describe networkpolicy ci-lab-worker
```

Each finished unit's report carries one git lookup per tree it leased, and the scheduler adds
them to its counters. A lookup is a hit when the worker needed no fetch: the head's tree was
already live in the pod, or the pod's store found the commit, its own or the node mirror's
through `objects/info/alternates`. A miss is a fetch the worker made. From a client that reaches
the scheduler, as in [Verify the running service](#verify-the-running-service):

```bash
curl -fsS http://lab:9108/metrics | grep -E '^ci_lab_cache_(hits|misses)_total\{cache="git"\}'
```

Hits that rise as units finish, with misses flat, show workers cutting trees from the node mirror
or from trees they already hold.

### Limitations

- The worker's `/metrics` on 9108 serves its own empty registry, so a scrape holds no samples, and
  the chart defines no PodMonitor or ServiceMonitor for the worker pods. The policy admits the
  monitoring Prometheus to 9108 for when one exists.
- The scheduler sets `ci_lab_mirror_age_seconds` from the `fetched-at` stamp under its own
  `CI_LAB_GIT_MIRROR`, which its fetch never writes, so the series has a sample only when that
  path is a mirror the node cache refreshes, and it shows no other node's mirror.
- The worker fetches a head it lacks from the node mirror only. The chart sets neither
  `AGENTIHOOKS_LAB_REMOTE` nor `CI_LAB_REPOSITORY`, and the policy reaches neither GitHub nor the
  lab remote, so the executor of a head the node mirror does not hold yet raises at its fetch and
  leaves the unit's lease for the scheduler to reap.
- A head whose `.github/test-requirements.lock` digest differs from the image's baked venv builds
  its venv with `uv venv` and `uv pip install` under `env -i`, which drops `UV_CACHE_DIR`, and the
  build needs a package index this policy does not reach. See [Head venv builds](#head-venv-builds).
- Nothing writes `/work/manifest.json`, so the test executor raises `FileNotFoundError` when it
  reads its dependency manifest. The worker delivers nothing for the unit and the scheduler
  reaps the lease; the [retry policy](#retry-policy) requeues it
  once and marks it lost on the second expiry.
- Seccomp is `Unconfined` for the whole pod; a `Localhost` profile that adds only the namespace
  calls bubblewrap needs is not written.

### Worker delivery evidence

Pull request 1826 recorded its tests and its network probe; the live placement and cache results
its done criteria name are not recorded:

| Criterion | Evidence |
|---|---|
| The packing, isolation and network seams | Red on run 44496821882 of the deployment repository, 2 failed and 2588 passed, on the tests alone; green on run 44502133418 at the final head, 2599 passed |
| Egress only to DNS, the scheduler and MinIO | The live probe on a CI node described under [Network](#network) |
| Three workers on one node, with `kubectl describe node` near 15 of 16 CPU requested | Not recorded; the figure in the verify steps above is computed |
| A worker git hit from the node mirror | Not recorded |
| The scheduler exporting `ci_lab_mirror_age_seconds` | `metrics.observe_mirror` sets it at each read of the metrics port; a live sample on a CI node is not recorded |

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| Worker pods stay `Pending` with no scheduling events | The `runner-binpack` scheduler is not running, and the default scheduler ignores pods that name another | `kubectl -n kube-system get deploy runner-binpack-scheduler` and its pod logs |
| `Pending` with `didn't match Pod's node affinity/selector` or `untolerated taint` | No CI pool node exists or none carries both labels | `kubectl get nodes -L <domain>/node-pool,<domain>/workload`; the autoscaler adds CI nodes up to its pool maximum |
| `Pending` with `Insufficient cpu` or `Insufficient memory` | Every CI node is full of pods a worker cannot preempt, or the pool is at `nodeMax` | The autoscaler's pool maximum and the `Preempted` events that name the worker, as in [Worker scheduling priority](#worker-scheduling-priority) |
| `ContainerCreating` with `hostPath type check failed` for `git` or `uv` | The node cache has not prepared the node | `kubectl -n ci-lab get pods -l app.kubernetes.io/component=node-cache -o wide` and its `prepare` log; the kubelet mounts once the folders exist |
| The worker exits at start with `ci-lab worker refuses` | Its environment holds a `GITHUB_APP_` or `REDIS` variable | Remove the variable from the chart |
| The worker exits at start with a `KeyError` naming a `CI_LAB_` variable | A required work setting is missing from the pod environment | Compare the pod's `env` with the contract table above |
| The worker log repeats `ci-lab worker: lease call failed, retrying in <n>s` | The scheduler is not answering on 8800, or answers with a server error: it is rolling, restarting or down. The worker keeps running, retries after a backoff that doubles from 1 up to 10 seconds, and leases again on the first answer | `kubectl -n ci-lab get deploy ci-lab-scheduler` and `kubectl -n ci-lab logs deploy/ci-lab-scheduler --since 10m` |
| The worker exits with an `HTTPError` 4xx raised from `Client.lease` | The scheduler answered the lease call with a client error, such as a 404 when `CI_LAB_SCHEDULER_URL` names a wrong path; a retry cannot change that answer, so the process ends with the traceback without draining its running units, which are left to finish or reap | Compare the pod's `CI_LAB_SCHEDULER_URL` with the contract table above |
| A head's units are reaped undelivered and the worker log shows a failed `git fetch` | The node mirror lacks the head and the worker has no other source | The node cache's `fetched-at` stamp and warm log, see [Verify every node](ci-lab-node-cache.md#verify-every-node) |
| A venv build fails with `uv venv for <tree> ended with code` or `uv pip install for <tree> ended with code` | The build needs the package index, which the policy refuses | The kept build log the error names; see Limitations |
| The worker log reads `ci-lab worker: unit <job> crashed: FileNotFoundError(2, 'No such file or directory')` and the traceback's last line names `<tree>/.github/test-requirements.lock` | The head does not track the test requirements lock, so `executors.lockfile_digest` cannot key the unit's venv. The unit never reports, its lease expires, and the [retry policy](#retry-policy) requeues it once and marks it lost on the second expiry | `git ls-tree <head> .github/test-requirements.lock` prints nothing; see [Which venv a unit gets](#which-venv-a-unit-gets) |
| The same crash, with the traceback naming `<tree>/uv.lock` | The worker runs an image older than pull request 12536, which hashed the untracked `uv.lock`; every unit crashes this way | `kubectl -n ci-lab get deploy ci-lab-worker -o jsonpath='{.spec.template.spec.containers[0].image}'`, then roll the workers onto a release that carries the fix |
| `ci_lab_cache_misses_total{cache="uv"}` rises once per pod for each lock digest the worker image did not bake | Those heads' `.github/test-requirements.lock` differs from the lock the image baked, so each pod builds `/work/venvs/<digest>` once for it; a pod whose baked marker matches a head's lock records no uv miss for it | Compare the two digests as in [Verify the venv keying](#verify-the-venv-keying); an image built from a commit with the same lock runs those units on the baked venv |
| A venv build fails with `uv venv for <tree> ended with code None` or `uv pip install for <tree> ended with code None` | The step ran past the unit time limit, `Pod.timeout`, 1800 seconds, and the sandbox killed it | The kept build log the error names; no marker is written, so the next unit with that digest builds again |
| Uploads time out or are refused | The MinIO egress rule does not match the object store's pods | `minio.namespace`, `minio.pods` and `minio.port` against `kubectl get pods -n <namespace> --show-labels` |
| A sandboxed unit fails at bubblewrap with `Operation not permitted` | The pod runs under a seccomp profile that refuses namespace calls, or the node kernel restricts user namespaces | The pod's `seccompProfile`, then the namespace probe in the [sandbox verify](#unit-sandbox) steps on the node |
| A worker pod is evicted for ephemeral storage | `/work`, `/tmp` and the logs passed 20 GiB | `kubectl -n ci-lab get events --field-selector reason=Evicted` |
| A worker container is `OOMKilled` | Its units passed the 8 GiB limit | `kubectl -n ci-lab describe pod <worker pod>` for the last state |
| A rollout takes long | Each step waits for one busy worker to drain, up to 2100 seconds | `kubectl -n ci-lab rollout status deploy/ci-lab-worker` |
| An idle CI node is not scaled down | A worker still runs on it, and `safe-to-evict: "false"` keeps the node | `kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker -o wide` |

## Worker scheduling priority

The PriorityClass `ci-lab-worker-preempt` ranks lab worker pods at 250, above every CI runner
(`arc-runners-oss-checks`, 200) and below `qitp-trading` (1000), and lets a pending worker preempt
pods of lower priority. When the CI nodes are full, a pending worker does not wait for a node to
free by itself. The scheduler picks a node where evicting lower priority pods makes room for the
worker, deletes those pods, and schedules the worker there once they have terminated. On the CI
pool the pods below a worker are the Actions Runner Controller runners at 200 and pods that name no
class at 0, such as the node cache pods; in the rollout of 19 November 2026 the scheduler evicted
only runner pods. When a node frees on its own, the scheduler also tries a pending worker before
every pending runner. A pod gets this rank only when its spec names the class;
[Use it from a worker pod](#use-it-from-a-worker-pod) shows how.

| PriorityClass | Value | `preemptionPolicy` | Pods that name it |
|---|---|---|---|
| `qitp-trading` | 1000 | `PreemptLowerPriority` | Trading workers |
| `ci-lab-worker-preempt` | 250 | `PreemptLowerPriority` | Lab workers |
| `arc-runners-oss-checks` | 200 | `Never` | Every runner set of the deployment repository's `arc-runners-oss` chart: the pull request check and image build runners, the light runners, the merge queue runners, which inherit the pull request runner pod and its class, and the batch mutation runners |
| `arc-runners-oss-batch-priority` | 100 | `Never` | None; it stays defined in `runner-priority-classes.yaml` until no pod names it |
| none | 0 | `PreemptLowerPriority`, the Kubernetes default | Every pod that names no class, such as the runners in the `arc-runners` namespace and the [node cache](ci-lab-node-cache.md) pods |

The class is defined as:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: ci-lab-worker-preempt
value: 250
preemptionPolicy: PreemptLowerPriority
globalDefault: false
description: ci-lab workers schedule ahead of, and preempt, pull request, merge queue and batch runners in arc-runners-oss-checks (200)
```

- `value: 250`: above all four `arc-runners-oss` runner sets, the merge queue runners included,
  and below `qitp-trading`.
- `preemptionPolicy: PreemptLowerPriority`: a pending worker that fits no node may evict running
  pods of lower priority to get one. Preemption only evicts pods of lower priority, so a runner can
  never evict a running worker, and a pending worker never evicts another worker. `qitp-trading`
  also preempts and can still evict a running worker.
- `globalDefault: false`: the class applies only to pods that name it. A pod that names no class
  keeps priority 0.

### Where it lives and how it reaches the cluster

The class sits alone in `k8s/manifests/cluster/ci-lab-priority-classes.yaml` in the deployment
repository, apart from the runner classes in `runner-priority-classes.yaml` beside it. The
`cluster-rbac` Argo CD Application syncs `k8s/manifests/cluster` recursively from that
repository's `dev` branch with automated prune and self heal, so a change to the class reaches the
cluster when it merges into `dev`, with no promotion step.

The class reached its current form in three of that repository's pull requests:

| Pull request | Date | Class | Value | `preemptionPolicy` | Why |
|---|---|---|---|---|---|
| 1740 | 15 November 2026 | `ci-lab-worker` | 150 | `Never` | First class: workers behind every CI runner, ahead of the unused batch class |
| 1913 | 19 November 2026, 14:18 UTC | `ci-lab-worker-priority` | 250 | `Never` | The worker Deployment had been 0 of 3 available since 10:57 UTC: every worker pod lost `FailedScheduling` on `Insufficient cpu` while pending runner pods at 200 took the freed room |
| 1917 | 19 November 2026, 15:56 UTC | `ci-lab-worker-preempt` | 250 | `PreemptLowerPriority` | At 250 with `Never` the workers still waited: at 14:33 UTC `ci_lab_workers_ready` read 0 and three worker pods had been Pending for 10 minutes while pending runner pods took every freed slot |

Pull requests 1913 and 1917 each replaced the class document under a new name and pointed the
worker template at it in the same pull request. `cluster-rbac` pruned the old class, and
`kubectl get priorityclass` on 19 November at 16:07 UTC listed `ci-lab-worker-preempt` and no
older lab class.

Two of the deployment repository's tests guard the class:

| Test | Holds |
|---|---|
| `tests/unit/ci_lab/test_priority.py` | The lab file defines `ci-lab-worker-preempt` and no other class; its value is 250, above the batch class (100) and the checks class (200) read from the runner file and below `qitp-trading` (1000, pinned as a constant); `scheduling.k8s.io/v1`, `PreemptLowerPriority` and `globalDefault: false` |
| `tests/unit/ci_lab/test_packing.py` | The rendered worker Deployment names `schedulerName: runner-binpack` and `priorityClassName: ci-lab-worker-preempt`, and that name is a class the lab file defines |

A re-rank or a policy change updates both in the same pull request. The runner classes keep their
own test, `tests/unit/arc_runners_oss/test_priority.py`, which this class leaves untouched.

### Use it from a worker pod

A pod gets the rank only by naming the class in its spec. The lab chart's
[worker Deployment](#worker-deployment) carries this in its pod template:

```yaml
spec:
  template:
    spec:
      schedulerName: runner-binpack
      priorityClassName: ci-lab-worker-preempt
```

Admission resolves the name to `spec.priority` when the pod is created, so a running pod keeps the
priority it was created with. Admission refuses a pod that names a class the cluster does not hold.
The `runner-binpack` scheduler runs the preemption; its `Preempted` events name it as the reporting
controller. The worker Deployment named `ci-lab-worker` from the deployment
repository's pull request 1826, `ci-lab-worker-priority` from 1913, and `ci-lab-worker-preempt`
from 1917.

### Verify

The class as the cluster holds it:

```bash
kubectl get priorityclass ci-lab-worker-preempt
```

```text
NAME                    VALUE   GLOBAL-DEFAULT   AGE   PREEMPTIONPOLICY
ci-lab-worker-preempt   250     false            10m   PreemptLowerPriority
```

The full order, system classes included:

```bash
kubectl get priorityclass --sort-by=.value
```

The class, priority and uid each worker pod resolved to:

```bash
kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker \
  -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,NODE:.spec.nodeName,CLASS:.spec.priorityClassName,PRIORITY:.spec.priority'
```

Every row reads `ci-lab-worker-preempt` and `250`.

The scheduler records preemption as `events.k8s.io/v1` events with `eventTime` set and
`lastTimestamp` empty, so read them through that API. Each `Preempted` event names the evicted
runner pod and, in its note, the uid of the worker that evicted it:

```bash
kubectl get events.events.k8s.io -A --field-selector reason=Preempted \
  -o custom-columns='TIME:.eventTime,NAMESPACE:.metadata.namespace,POD:.regarding.name,NOTE:.note'
```

One worker's own timeline, from its first `FailedScheduling` to `Scheduled`:

```bash
kubectl get events.events.k8s.io -n ci-lab --field-selector regarding.name=<worker pod> \
  -o custom-columns='TIME:.eventTime,REASON:.reason,NOTE:.note'
```

The API server drops events after its event TTL, one hour by default, so read them soon after a
rollout or scale up.

This excerpt is the worker rollout that followed pull request 1917 on 19 November 2026, for worker
`ci-lab-worker-8678fc4dbf-xh7zp` (uid `3d3ca3ba-1909-44a3-b734-ed8de53f5408`), with the
`FailedScheduling` notes shortened:

```text
15:57:12.04  ci-lab           FailedScheduling  ci-lab-worker-8678fc4dbf-xh7zp      0/9 nodes are available: ... 3 Insufficient memory, ... 5 Insufficient cpu.
15:57:12.16  arc-runners-oss  Preempted         <pull request runner pod 1>         Preempted by pod 3d3ca3ba-1909-44a3-b734-ed8de53f5408 on node <ci node>
15:57:12.18  arc-runners-oss  Preempted         <merge queue runner pod>            Preempted by pod 3d3ca3ba-1909-44a3-b734-ed8de53f5408 on node <ci node>
15:57:12.28  arc-runners-oss  Preempted         <pull request runner pod 2>         Preempted by pod 3d3ca3ba-1909-44a3-b734-ed8de53f5408 on node <ci node>
15:57:14.71  ci-lab           FailedScheduling  ci-lab-worker-8678fc4dbf-xh7zp      0/9 nodes are available: ... preemption: not eligible due to a terminating pod on the nominated node.
15:57:20.65  ci-lab           Scheduled         ci-lab-worker-8678fc4dbf-xh7zp      Successfully assigned ci-lab/ci-lab-worker-8678fc4dbf-xh7zp to <ci node>
```

`<ci node>` is one node of the CI pool, the same in every line, and each runner pod is shown as a
placeholder that names its runner set. The pending worker preempted a merge queue runner and two
pull request runners, then waited for them to terminate and was scheduled 8.6 seconds after its
first `FailedScheduling`. The other two workers of that rollout were scheduled 5.5 and 4.5 seconds
after theirs. The three workers evicted ten runner pods in all: four merge queue runners, three pull
request runners, one batch runner and two runners from the `arc-runners` namespace. No node cache
pod was evicted.

### What happens to a preempted runner's job

Kubernetes deletes the runner pod, and GitHub ends the job it was running with conclusion
`failure`. Of the four preempted merge queue jobs of 19 November, three carry the annotation
`The operation was canceled.` and one `Process completed with exit code 130.`

- In the merge queue, a preempted `unit` shard is recovered inside the same Tests run by the
  [lost unit shard rerun](delivery-tiers.md#lost-unit-shard-rerun). In the 19 November rollout
  the four preempted queue runners held `unit (3.12, 2)`, `(3.12, 4)`, `(3.12, 8)` and `(3.12, 10)`
  of one merge group run. `unit-lost` classified all four as runner loss, each
  `unit rerun (3.12, <shard>)` passed on a new runner, and `Gate — Required` passed.
- On other events nothing reruns the job by itself. `unit-lost` runs only on `merge_group`, and
  the Lost runner workflow's `scripts.ci_lost_runner` reads only jobs still `in_progress`, so it
  skips a job that already ended `failure`. Re-run the failed jobs.

### Re-rank or change the policy

`value` and `preemptionPolicy` are both immutable on an existing PriorityClass, so an edit of
either in place fails the `cluster-rbac` sync. To move the lab workers or change what they may
evict, do what pull requests 1913 and 1917 did, in one pull request in the deployment repository:

1. Replace the class document in `ci-lab-priority-classes.yaml` with one under a new name that
   carries the new value or policy.
2. Point the worker template's `priorityClassName` at the new name.
3. Update `WORKER`, the value bounds and the policy in `tests/unit/ci_lab/test_priority.py`, and
   the `priorityClassName` assertion in `tests/unit/ci_lab/test_packing.py`.

After the merge into `dev`, `cluster-rbac` creates the new class and prunes the old one, and the
`ci-lab` Application rolls the worker Deployment onto the new name. A running pod that still names
the pruned class keeps running until the rollout replaces it. If `ci-lab` syncs before
`cluster-rbac`, the new ReplicaSet reports `FailedCreate` until the class exists, and retries by
itself.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `kubectl get priorityclass ci-lab-worker-preempt` answers `NotFound` | `cluster-rbac` has not synced the commit that adds it, or its sync failed | `kubectl -n argocd get application cluster-rbac -o jsonpath='{.status.sync.status} {.status.sync.revision}{"\n"}'`; the revision must be a `dev` commit whose tree holds the class |
| The worker ReplicaSet reports `FailedCreate` with `no PriorityClass with name ci-lab-worker-preempt was found` | The pod template names the class before the cluster holds it | Sync `cluster-rbac` first, as above; the ReplicaSet retries pod creation by itself |
| A pending worker's `FailedScheduling` note ends `preemption: not eligible due to a terminating pod on the nominated node` | The worker has already preempted pods on that node and waits for them to terminate | Nothing to do; the worker's timeline above shows `Scheduled` once the victims are gone |
| Worker pods stay Pending and no `Preempted` event names their uid | No CI node can make room for a worker by evicting pods below 250: other workers, which a worker never evicts, or pods above 250 hold it. The worker waits for a node to free or for a new CI node | `kubectl get pods -A --field-selector=status.phase=Pending -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.priorityClassName,PRIORITY:.spec.priority --sort-by=.spec.priority`, then the worker's `FailedScheduling` notes |
| A check job failed with `The operation was canceled.` or exit code 130 while workers rolled out or scaled up | A worker preempted its runner pod | `kubectl get events.events.k8s.io -A --field-selector reason=Preempted` and match the evicted pod against the job's runner name; outside the merge queue, re-run the failed jobs |
| `kubectl get events` shows no time for a `Preempted` or `Scheduled` event | The scheduler sets `eventTime` and leaves `lastTimestamp` empty | Read them through `events.events.k8s.io` with `.eventTime`, as the Verify commands above do |
| A worker pod shows priority 0 | Its pod template names no class | The worker listing above prints an empty class |
| A running worker pod was evicted | A higher class that preempts, such as `qitp-trading`, took its node | `kubectl get events.events.k8s.io -n ci-lab --field-selector reason=Preempted` |
| `cluster-rbac` sync fails after the value or the policy was edited | Both are immutable | Re-rank or change the policy by name, as above |

## Worker pod deletion cost

A scale down of the worker Deployment removes the ReplicaSet's pods with the lowest
`controller.kubernetes.io/pod-deletion-cost` first. The scheduler keeps that annotation in step
with the leases, so idle workers go before busy ones:

| Transition | Patch on the worker's pod |
|---|---|
| A lease poll grants the worker at least one job | annotation set to `1000` |
| The completion or reap of the worker's last live lease | annotation removed |
| The end of one lease while the worker holds another | none |
| A grant that lands while the last lease's clear is in flight | annotation set to `1000` again |

A worker names itself by its pod hostname, so the worker name on a lease is its pod name; a pod on
the host network, or one whose name runs past 63 characters, has another hostname and its patch is
refused. `scripts.ci_lab.pods.KubePods` sends one JSON merge patch on
`/api/v1/namespaces/<namespace>/pods/<worker>`, with the namespace, token and CA of the scheduler's
mounted service account. Outside a cluster (`KUBERNETES_SERVICE_HOST` unset) the scheduler patches
nothing. A refused or unreachable API server never fails a grant, a completion or a pass, but each
patch waits up to five seconds for it; the annotation then lags until the worker's next transition.

The scheduler pod needs `patch` on `pods` in the lab namespace, a mounted service account token,
and network egress to the API server.

### Verify

```bash
kubectl get pods -n <namespace> -l app.kubernetes.io/component=worker \
  -o custom-columns='NAME:.metadata.name,COST:.metadata.annotations.controller\.kubernetes\.io/pod-deletion-cost'
```

A worker running a unit shows `1000`; an idle one shows `<none>`.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| Every worker shows `<none>` while units run | The scheduler's patches are refused or never reach the API server | `kubectl auth can-i patch pods -n <namespace> --as system:serviceaccount:<namespace>:ci-lab-scheduler`, then the scheduler's network policy egress to the API server |
| Grants and completions slow by about five seconds each | The API server does not answer the patches | The check above; the patch timeout bounds each patch at five seconds, and a completion that raced a grant sends two |
| A busy worker shows `<none>` after a grant raced a clear | The busy patch of the re-check was refused or timed out | The `kubectl auth can-i` check above; the cost stays cleared until the raced lease ends, and the worker's next poll that grants sets it again |
| An idle worker shows `1000` | A grant marked it busy again during a clear, and that lease ended before the re-check's patch landed | Its next lease poll that grants and the end of that lease reset it |

## Worker loss drill

The deployment repository's `ci-lab` chart carries a drill that kills one lab worker in the middle
of its units, so the lease expiry, the redelivery and the single verdict post can be read on the
deployed lab. It is off by default under `chaos.enabled`. Lowering the worker floor needs no drill
objects: a values pull request that lowers `elasticity.floorWorkers` shrinks the worker pool, and
the CI node count falls once a node holds no worker. An ordinary pod delete or node drain
redelivers nothing, as [Drain a node without a redelivery](#drain-a-node-without-a-redelivery)
describes.

### Drill objects

With `chaos.enabled: true` the chart renders six objects, each named `ci-lab-chaos` and labelled
`app.kubernetes.io/component: chaos`. With it false, the default, it renders none of them.

| Kind | What it holds |
|---|---|
| ServiceAccount | The account the Job runs as |
| Role and RoleBinding | `list` and `delete` on `pods` in the lab namespace, bound to that account alone; no other workload runs as it |
| ValidatingAdmissionPolicy | Matches a `DELETE` of a pod in the lab namespace sent by `system:serviceaccount:<namespace>:ci-lab-chaos`, and admits it only when the pod carries every worker selector label with its value: `app.kubernetes.io/name: ci-lab`, `app.kubernetes.io/instance: <release>` and `app.kubernetes.io/component: worker`. `failurePolicy` is `Fail` |
| ValidatingAdmissionPolicyBinding | Binds the policy with `validationActions: [Deny]`, so a refused delete answers `Forbidden` with `the chaos account deletes only lab worker pods` |
| Job | Sync wave `1`, so Argo CD applies it after the account, the Role and the policy; `backoffLimit: 0`, `activeDeadlineSeconds: 120` and no `ttlSecondsAfterFinished` |

The Role allows any pod; the admission policy keeps the drill from deleting the scheduler, the lab
remote or a node cache pod. The policy needs Kubernetes 1.30 or newer, which serves
ValidatingAdmissionPolicy under `admissionregistration.k8s.io/v1`. `<release>` is the Helm release
name, `ci-lab` for the deployed Argo CD Application.

The Job's pod runs `alpine/k8s:1.33.5` (`chaos.image`) as user and group 65532 on a read only root,
with every capability dropped, privilege escalation off, the `RuntimeDefault` seccomp profile and
an emptyDir at `/tmp`, which is also its `HOME`. It requests 50m CPU and 128Mi memory with a 128Mi
memory limit (`chaos.resources`). It runs this script under `/bin/sh -ec`:

```sh
pods=$(kubectl get pods --selector "$WORKERS" --field-selector status.phase=Running --output json)
pod=$(printf '%s' "$pods" | jq -r '[.items[] | select(.metadata.annotations["controller.kubernetes.io/pod-deletion-cost"])][0].metadata.name // empty')
test -n "$pod" || { echo "no lab worker holds a lease" >&2; exit 1; }
kubectl delete pod "$pod" --grace-period=1 --wait=false
```

`WORKERS` holds the worker selector labels joined by commas. The script deletes the first Running
worker in the list that carries the deletion cost annotation, which the scheduler sets while a
worker holds a lease (see [Worker pod deletion cost](#worker-pod-deletion-cost)). The one second
grace ends the pod before the worker's SIGTERM drain (see [Worker executors](#worker-executors))
can finish its units, so their leases expire instead of completing. The Job fails when no worker
carries the annotation, since an idle worker has no lease to expire, and when the pod list fails.

The drill runs once per enable. `backoffLimit: 0` starts no second pod after a failure. The Job has
no TTL because the `ci-lab` Application runs with self heal: a Job the TTL controller deleted would
be created again and would kill another worker.

### Run the drill

1. Pick a time when the lab grades a head with several units, such as a merge group or promotion
   grade, and check that a worker holds a lease:

   ```bash
   kubectl get pods -n ci-lab -l app.kubernetes.io/component=worker \
     -o custom-columns='NAME:.metadata.name,COST:.metadata.annotations.controller\.kubernetes\.io/pod-deletion-cost'
   ```

   A worker running a unit shows `1000`.
2. Open a pull request into the deployment repository's `dev` that sets, in
   `k8s/charts/ci-lab/values.yaml`:

   ```yaml
   chaos:
     enabled: true
   ```

3. Merge it while units still run. The `ci-lab` Application syncs `k8s/charts/ci-lab` from `dev`
   with automated prune and self heal, so the sync applies the account, the Role and the policy,
   then the Job, which lists the workers at once.
4. Read what the Job did and follow the units as
   [Verify a redelivery](#verify-a-redelivery) shows.
5. Open a second pull request that sets `chaos.enabled: false`. Prune deletes the Job, the
   account, the Role, the RoleBinding and the policy. Running the drill again takes another
   pull request that sets it back to `true`.

Leave the finished Job in place until that second pull request syncs. Deleting it by hand makes
self heal create it again, and the new Job kills another worker.

### What happens to a killed worker's units

Each unit the killed worker held goes through these steps:

1. Its lease expires. The worker heartbeats each lease every 10 seconds
   (`worker.HEARTBEAT_SECONDS`), and each heartbeat moves the lease's expiry 120 seconds ahead
   (`scheduler.LEASE_TTL_MS`), so the lease expires between 110 and 120 seconds after the kill.
2. The first scheduler pass after the expiry, at most 2 seconds later, reaps it in
   `Scheduler._reap`. `queue.Queue.reap` moves the job back to pending with one more attempt while
   its attempts are below `lab.retry.lost`, one by default (see
   [Configure the counts](#configure-the-counts)). The pass counts the job in
   `ci_lab_units_redelivered_total` and the service prints `reaped <job> pending`. A job that
   already spent its lost retry, after an earlier crash or loss, becomes `lost` instead: the pass
   counts `ci_lab_units_lost_total`, prints `reaped <job> lost`, and the grade turns red.
3. The dead worker's lease token is refused. A heartbeat or completion that carries it answers
   `403` with `lease token refused`, so a late call from the old pod changes nothing.
4. Another worker leases the requeued job, such as the pod the Deployment started in place of the
   killed one, runs it and completes it. Its report becomes the unit's verdict in the store.
5. The gate's check run, `lab/<gate>`, and the grade's `lab/required` each move `queued`,
   `in_progress` and `completed` once. The redelivery opens no second check run, and
   `Scheduler._post` never moves a completed check run again (step 8 of the
   [Scheduler loop](#scheduler-loop)).

`ci_lab_units_redelivered_total` therefore rises by the number of units the killed worker held, up
to its `worker.slots` (5), and `ci_lab_units_lost_total` stays where it was.

`tests/ci_lab/test_redelivery.py`, under the feature `lab-worker-loss-redelivery`, runs these steps
against the scheduler over fakeredis and the local object store with a 100 millisecond lease and two
workers, and reads both counters through `GET /metrics`. The deployment repository's
`tests/unit/ci_lab/test_chaos.py` pins the rendered drill: nothing by default, the Role rules and
binding, the policy's match and label checks, and the Job's single run, script, selector and
security context.

### Verify a redelivery

The Job:

```bash
kubectl -n ci-lab get job ci-lab-chaos
kubectl -n ci-lab logs job/ci-lab-chaos
```

A Job that killed a worker completes and logs the deletion of one worker pod. A failed Job's log
names its cause, as the [troubleshooting table](#worker-loss-troubleshooting) lists.

The scheduler's reaps, two minutes after the kill:

```bash
kubectl -n ci-lab logs deploy/ci-lab-scheduler --since 10m | grep --line-buffered -E '^reaped '
```

The counters, read once before the merge and again after the reap, from the scheduler or from
Prometheus once the ServiceMonitor's 30 second scrape follows it:

```bash
curl -fsS http://<scheduler>:9108/metrics | grep -E '^ci_lab_units_(redelivered|lost)_total '
```

```promql
ci_lab_units_redelivered_total
ci_lab_units_lost_total
```

`ci_lab_units_redelivered_total` rises by at least one. A counter writes no sample until its first
count, so when the scheduler has reaped nothing since it started, the series is absent before the
kill and its first sample after the reap is the count itself. `increase()` over a window that
holds the series' first sample misses that count, so compare the raw values.
`ci_lab_units_lost_total` keeps its value, or stays absent. Both counters live in the scheduler
process and restart from zero with it, so a scheduler restart between the two reads voids the
comparison.

The check runs of the graded head, one call:

```bash
gh api "repos/<owner>/<repo>/commits/<sha>/check-runs" --paginate \
  --jq '.check_runs[] | select(.name | startswith("lab/")) | "\(.name) \(.status) \(.conclusion)"'
```

Each `lab/<gate>` name and `lab/required` appears once, `completed`, with the grade's conclusion.

### Drain a node without a redelivery

Every other way a worker pod ends drains its units: a rollout, a KEDA scale down, a
`kubectl delete pod` without `--grace-period`, or a node drain. Each sends SIGTERM and waits for the
pod's termination grace, 2100 seconds (`worker.terminationGracePeriodSeconds`). The worker stops
leasing, keeps heartbeating its running units, delivers each one and exits, so no lease expires and
`ci_lab_units_redelivered_total` stays flat. A unit still running when the grace ends is abandoned;
its lease expires and it is redelivered as the drill's units are.

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
```

`--ignore-daemonsets` leaves the node cache pod, and `--delete-emptydir-data` lets the drain evict
workers, whose `work` and `temporary` volumes are emptyDir. The chart defines no
PodDisruptionBudget, so the drain evicts every worker on the node at once and returns when the last
one has delivered its units. The Deployment starts replacements on another CI node, or on a node
the autoscaler adds. `kubectl uncordon <node>` makes the node schedulable again.

### Lower the worker floor

The KEDA ScaledObject `ci-lab-worker` sizes the worker Deployment as [Packing](#packing) describes,
and [Configuration](#configuration) lists every `elasticity` value. `elasticity.floorWorkers`, 1 by
default, is both its `minReplicaCount` and the `fallback` replicas it holds after
`elasticity.fallbackFailures` failed trigger reads. The trigger sets `ignoreNullValues` to
`"false"`, so a missing `ci_lab_slots_wanted` series counts as a failed read.

A values pull request into the deployment repository's `dev` changes the floor:

```yaml
elasticity:
  floorWorkers: 0
```

The node count follows the floor through four steps:

1. Argo CD syncs the ScaledObject. While the wanted slots ask for fewer workers than the old
   floor, the HorizontalPodAutoscaler KEDA builds from it, `keda-hpa-ci-lab-worker`, waits out the
   300 second window, then removes two workers a minute down to the new floor. A floor of `0` lets
   KEDA remove the last worker once nothing has been wanted for its cooldown period, the KEDA
   default of 300 seconds, since the ScaledObject sets no `cooldownPeriod`.
2. The ReplicaSet picks the workers to remove. It takes any worker not yet scheduled, pending or
   not ready first, then the workers with the lowest deletion cost, so idle workers go before
   busy ones. Each removed worker drains as above.
3. Workers run only on the `oci-ci` node pool, placed by the `runner-binpack` scheduler. The
   deployment repository's OCI cluster autoscaler removes a CI node that stays below half its
   requested capacity for one minute.
4. Workers carry `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`, so the autoscaler
   never drains a node out from under a worker. A CI node goes only once KEDA has removed every
   worker on it.

Read the floor, the autoscaler and the CI nodes:

```bash
kubectl -n ci-lab get scaledobject ci-lab-worker -o jsonpath='{.spec.minReplicaCount}{"\n"}'
kubectl -n ci-lab get hpa keda-hpa-ci-lab-worker
kubectl -n kube-system get configmap cluster-autoscaler-oci-status -o jsonpath='{.data.status}'
```

The `CI nodes against the cap` panel of the [CI Lab dashboard](#ci-lab-dashboard) plots the node
count. The same count in Prometheus:

```promql
count(kube_node_spec_taint{key="<label domain>/workload", value="ci"} and on (node) kube_node_spec_taint{key="<label domain>/remote", value="oci"})
```

A floor of `0` also makes the fallback `0`, so a Prometheus outage of three polls scales the pool to
no workers and the lab grades nothing until the series returns.

### Worker loss limitations

- The drill deletes the first leased worker the API server lists, never a chosen one, and every
  unit that worker holds expires with it.
- The Job lists the workers when Argo CD syncs the enabling merge. A merge that lands while no
  worker holds a lease fails it, and a second run needs the disable and enable pull requests.
- The drill deletes a pod. It never cordons or drains a node; the drain path redelivers nothing.
- Both counters reset when the scheduler restarts, so a restart between the kill and the read
  loses the count. The scheduler log keeps the `reaped` lines.
- On a tie in deletion cost the ReplicaSet removes workers from the nodes running the most of them
  first, so a scale down can leave one idle worker on each of several nodes. Those nodes stay until
  the pool falls far enough to empty one.

### Worker loss delivery evidence

The deployment repository's pull request 1907 delivered the drill and `test_chaos.py`. Pull request
[#12445](https://github.com/the-cloud-clockwork/agentihooks/pull/12445) added
`test_redelivery.py`. A live run on the deployed lab, with a counter rise after the drill and a node
count fall after a floor change, has no recorded evidence on this page.

### Worker loss troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| The Job fails and logs `no lab worker holds a lease` | No Running worker carried the deletion cost annotation when the Job ran | The custom columns query in [Run the drill](#run-the-drill); disable the drill, then enable it again while units run |
| The Job fails with `Forbidden` and `the chaos account deletes only lab worker pods` | The pod the Job chose lacks a worker selector label, or one differs | `kubectl -n ci-lab get pod <pod> --show-labels` against the three worker labels |
| The Job fails with `cannot list resource "pods"` or `cannot delete resource "pods"` | The Role or RoleBinding is missing, or the Job ran before its sync wave applied them | `kubectl -n ci-lab get role,rolebinding ci-lab-chaos`; the Application's sync status |
| Argo CD cannot apply the ValidatingAdmissionPolicy | The cluster serves no `admissionregistration.k8s.io/v1` ValidatingAdmissionPolicy | `kubectl api-resources --api-group=admissionregistration.k8s.io` lists `validatingadmissionpolicies` on Kubernetes 1.30 or newer |
| `ci_lab_units_redelivered_total` stays flat after the kill | The lease has not expired yet, the scrape has not run since, or the worker held no lease | Wait two and a half minutes; then the `reaped` lines in the scheduler log |
| `ci_lab_units_lost_total` rises and the grade is red | The killed unit had already spent its lost retry on an earlier crash or loss | `lab.retry.lost` in `delivery.yaml`; rerequest a check run of the head on GitHub to grade it again, as the [Scheduler loop](#scheduler-loop) describes |
| A second worker dies after the drill | The finished Job was deleted by hand, and self heal created it again | Disable the drill by pull request; never delete the Job |
| The CI node count stays after a lower floor | Wanted slots still ask for more workers, the 300 second window has not passed, or each node still runs a worker | `kubectl -n ci-lab get hpa keda-hpa-ci-lab-worker`; the worker pods with `-o wide`; the autoscaler status ConfigMap |

## Lab head intake

The lab learns which heads to grade from `agentihooks github-webhook`, the GitHub webhook receiver
in `scripts.hub.github_webhooks`. `scripts.ci_lab.webhooks` turns each head it hears of into an
envelope typed `ci.lab.head.<event>`, and the receiver publishes it on the bus stream
`AGENTIHOOKS-CI-LAB-HEADS`, which holds `ci.lab.head.>`. The receiver records lab heads only when
both of these hold:

1. **The stream exists.** A bus opened with provisioning on creates it after the events stream; the
   hub's root bus does so when it starts. The receiver opens its bus with provisioning off whatever
   `AGENTIHOOKS_BUS_BIND` says, so it never creates the stream and publishes to it by name.
   [Subjects and streams per swarm](swarm-bus-scope.md#subjects-and-streams-per-swarm) describes
   the stream itself.
2. **The receiver holds `AGENTIHOOKS_CI_LAB_NOTICE_SECRET`.** Without it the receiver still
   verifies GitHub deliveries and records their pull request observations, but records no lab head
   from any route and answers `404` on `/lab/head`.

### Heads it records

| Source | Recorded when | Subject | `head` and `base` | `actor` |
|---|---|---|---|---|
| `push` delivery on `/github` | A branch under `refs/heads/` outside the plant prefix moved; a deleted branch, a new branch whose `before` is all zeros, or a tag records nothing | `ci.lab.head.push` | `after` and `before` | The delivery's sender |
| `push` delivery on `/github` to a plant branch | A branch under `refs/heads/diffcheck/lab-plant-` (`scripts.ci_lab.webhooks.PLANT_PREFIX`) moved; a deleted branch records nothing | `ci.lab.head.plant` | `after`, and an empty base whatever `before` says | The delivery's sender |
| `pull_request` delivery on `/github` | The action is `opened`, `synchronize` or `reopened` | `ci.lab.head.pull_request` | The pull request's head and base SHA | The pull request's author |
| `merge_group` delivery on `/github` | The action is `checks_requested` | `ci.lab.head.merge_group` | The group's `head_sha` and `base_sha` | The delivery's sender |
| `check_run` delivery on `/github` | The action is `rerequested` and the check's name starts with `lab` | `ci.lab.head.check_run` | The run's `head_sha`, and the base of its first pull request, empty when it has none | The delivery's sender |
| Signed notice on `/lab/head` | Every valid notice | `ci.lab.head.notice` | As the notice names them | As the notice names it |

The receiver records each head the table names whoever its actor is, and every other GitHub
event records no head. `scripts.ci_lab.intake.admit` keeps only heads whose actor is on the
runner trust list (`.github/workflows/trusted-actors.json`) as grade requests, but no service
calls it yet. A pull request head's actor is its author rather than the sender, so once that filter
is wired in, a trusted user reopening an untrusted author's pull request yields no grade request.

A delivery the receiver cannot read answers `400`. A missing field that the pull request
observation of a `pull_request` or `check_suite` delivery reads answers before anything is
recorded. With lab intake on, a missing field that only a head row reads answers after the pull
request observations were recorded.

Each head is one envelope:

| Field | Value |
|---|---|
| `id` | SHA-256 of the head's fields, so one head event always has one id |
| `type` | Its subject, `ci.lab.head.<event>` |
| `swarm` | `ci-lab` |
| `entity` | `head` |
| `key` | `<repo>@<head>`; GitHub deliveries give `repo` as the repository's `html_url` |
| `version` | `0` |
| `time` | Milliseconds since the epoch when the receiver built it |
| `author` | The actor |
| `data` | `event`, `repo`, `head`, `base`, `actor` and `action` (`pushed` for a push or a notice, else the delivery's action) |

The bus sends each envelope with its id as `Nats-Msg-Id`, and the stream drops a repeated id inside
its duplicate window, the bus dedupe window of two minutes. A GitHub redelivery of the same push,
or a notice sent twice, inside that window is stored once.

### Receiver environment

`agentihooks github-webhook [--host 127.0.0.1] [--port 8790]` reads:

| Name | Required | Meaning |
|---|---|---|
| `AGENTIHOOKS_GITHUB_WEBHOOK_SECRET` | yes | Verifies `X-Hub-Signature-256` on `/github`. Unset, the receiver prints `agentihooks github-webhook: AGENTIHOOKS_GITHUB_WEBHOOK_SECRET must be set to verify GitHub webhooks` and exits 2 |
| `AGENTIHOOKS_CI_LAB_NOTICE_SECRET` | no | Turns lab intake on and verifies `X-Hub-Signature-256` on `/lab/head`. Unset or empty, lab intake is off as above |
| `AGENTIHOOKS_BUS`, `AGENTIHOOKS_BUS_URL`, `AGENTIHOOKS_BUS_CREDENTIALS` | in a cluster | The bus backend, its address and the receiver's NATS credential |

The receiver reads both secrets once at start, so a changed value reaches it only through a pod
restart.

### Send a head notice

A head notice tells the lab about a head without waiting for GitHub. It is `POST /lab/head` with a
JSON body, signed the way GitHub signs a delivery but with the lab notice secret: the
`X-Hub-Signature-256` header carries `sha256=` and the hex HMAC-SHA256 of the raw body.

| Body field | Meaning |
|---|---|
| `repo` | The repository URL. Use its `https://github.com/<owner>/<repo>` form so the key matches the one GitHub deliveries give the same head |
| `head` | The head commit SHA |
| `base` | The base commit SHA |
| `actor` | The GitHub login that pushed |

All four must be non empty strings; other fields are ignored. From a pod inside the cluster that has
`python3` and `curl`:

```bash
body='{"repo":"https://github.com/<owner>/<repo>","head":"<head sha>","base":"<base sha>","actor":"<login>"}'
digest="$(printf '%s' "$body" | python3 -c 'import hashlib, hmac, os, sys; print(hmac.new(os.environb[b"AGENTIHOOKS_CI_LAB_NOTICE_SECRET"], sys.stdin.buffer.read(), hashlib.sha256).hexdigest())')"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "X-Hub-Signature-256: sha256=$digest" -H 'Content-Type: application/json' \
  --data-binary "$body" \
  http://agentihooks-swarm-github-webhook.agentihooks-swarm.svc:8790/lab/head
```

Sign the exact bytes you send; JSON serialized again after signing no longer matches. The signer
reads the secret from its environment, so the value never appears in a process argument list.

| Answer | Meaning |
|---|---|
| `200` | The head was published, or the stream already held it inside the duplicate window |
| `400` | The body is not JSON, or one of the four fields is missing, empty or not a string |
| `401` | The signature header is missing or does not match the lab notice secret |
| `404` | Lab intake is off, or the path is neither `/github` nor `/lab/head` |
| `411` | `Content-Length` is missing or not a number |
| `413` | The body is over 25 MiB |
| `503` | The publish failed; the receiver logs `hub: GitHub webhook could not record the event` with the traceback |

Each request also logs `github-webhook: delivery <X-GitHub-Delivery> event <X-GitHub-Event> answered <code>`
on standard error; a notice carries neither header, so both print `None`.

The deployment's public ingress routes only `/github`. A notice reaches the receiver's Service on
port 8790 from inside the cluster, where the receiver's network policy admits the pod network.

`agentihooks swarm <slug> publish` sends one notice after each push to origin. Its body names the
origin in the `https://github.com/<owner>/<repo>` form, the pushed head, the head's merge base with
`origin/dev` and the actor below, signed as above. The publish result records the receiver's status
as `lab_notice`, or `0` when the send failed. Without all three settings nothing is sent and
`lab_notice` is absent. A send the receiver refuses, or that cannot reach it, prints
`lab head notice for <head> was not accepted: <error>; the push stands` on standard error, records
`lab_notice` `0` and leaves the push in place.

| Name | Meaning |
|---|---|
| `AGENTIHOOKS_CI_LAB_NOTICE_URL` | The receiver's `/lab/head` address, such as `http://agentihooks-swarm-github-webhook.agentihooks-swarm.svc:8790/lab/head` |
| `AGENTIHOOKS_CI_LAB_NOTICE_SECRET` | The lab notice secret the receiver verifies with |
| `AGENTIHOOKS_CI_LAB_NOTICE_ACTOR` | The GitHub login the notice names. It must be on the trust list, or the scheduler records the head without grading it |

### Push a head to the lab remote

The lab remote is a git endpoint beside the scheduler; [Lab remote service](#lab-remote-service)
covers where it runs, how it authenticates, its token and the client settings. Its post receive hook,
`python -m scripts.ci_lab.remote.post_receive`, turns each pushed `refs/grade/*` head into a
`ci.lab.head.notice` envelope on the same stream, and prints the head's grade url to the pusher:

```text
$ git push -o branch=<branch> lab HEAD:refs/grade/<task>
remote: lab grade: <CI_LAB_URL>/grades/<sha>
```

A swarm agent makes this push with `agentihooks swarm <slug> publish --via lab`, which then waits on
the url before it pushes origin; see [Publishing through the lab](../pillars/swarm.md#publishing-through-the-lab).
[Workspaces that carry the lab remote](#workspaces-that-carry-the-lab-remote) covers where the `lab`
remote comes from.

The url is the scheduler's [`/grades/<sha>`](#grades-of-a-commit) route. The envelope carries the
pushed `ref` and the `branch` push option, which becomes the head's scheduler lane. Its id covers
both, so the same head pushed again with another branch or to another grade ref is a new head
event. The base is the `-o base=<sha>` push option, else the head's first parent. The hook runs
after git writes the refs, so it cannot turn a push away. The remote's pre receive hook refuses refs
outside `refs/grade/*`. A deleted grade ref, or a root commit pushed without `-o base`, prints
`lab remote: <ref> is not graded` and publishes nothing.

GitHub does not hold a head pushed only to the lab remote, so the scheduler, the relay and the
worker fetch it from there. The scheduler and relay mirrors fetch a sha from the GitHub repository
first and, when that fetch fails, from the lab remote `AGENTIHOOKS_LAB_REMOTE` names, but only
when that URL names the same repository. The node cache fills the worker's node mirror from GitHub,
so a worker whose node mirror lacks the sha fetches it from the lab remote into its pod store
before it cuts the tree, but only when `CI_LAB_REPOSITORY` names the repository the lab URL names.
Git gets `AGENTIHOOKS_LAB_REMOTE_TOKEN` as the basic password of the user `lab` for that URL alone,
through its environment config, and no store or log keeps it. Worker units run with the sandbox
whitelist alone, so no unit sees the token. A grade of a head pushed
to a `refs/grade/*` ref keeps the ref and records each check's status and conclusion. It posts no
GitHub check run; the relay posts them once it has pushed the head. The relay's push token asks
for contents write and workflows write, so a relayed branch that changes workflow files is
accepted.

| Name | Meaning |
|---|---|
| `CI_LAB_REPOSITORY` | The repository URL the head belongs to, in the `https://github.com/<owner>/<repo>` form GitHub deliveries use |
| `CI_LAB_REMOTE_ACTOR` | The actor every pushed head is recorded under. It must be on the trust list, or the scheduler records the head without grading it |
| `CI_LAB_URL` | The lab scheduler address a hive and the workstation can reach, used as the base of the grade url |
| `AGENTIHOOKS_BUS`, `AGENTIHOOKS_BUS_URL`, `AGENTIHOOKS_BUS_CREDENTIALS` | The bus the hook publishes on, opened with provisioning off |

### Workspaces that carry the lab remote

An agent pushes to the lab through a git remote named `lab`. The workstation, hives and swarm v2
workspaces add that remote by themselves when two variables are set where the workspace is made:

| Name | Meaning |
|---|---|
| `AGENTIHOOKS_LAB_REMOTE` | The lab remote URL, such as `http://<lab host>/agentihooks.git`. Unset or empty, no workspace gets the remote |
| `AGENTIHOOKS_LAB_REMOTE_TOKEN` | The token git sends as the basic password of the user `lab`. Only the name is written to git config; git reads the value from its own environment each time the lab asks |

`scripts.ci_lab.remote.checkout.carry` does the work, and `wt.sh` repeats the same steps in shell.
For a repository the URL matches, it writes two git config entries:

1. `remote.lab.url`: `git remote add lab <url>`, or `git remote set-url lab <url>` when a `lab`
   remote already exists, so a moved lab URL replaces the old one on the next pass.
2. `credential.<url>.helper`: a shell function for that URL alone. On git's `get` request it prints
   the user `lab` and the value of `AGENTIHOOKS_LAB_REMOTE_TOKEN` as the password; it answers no
   other request and stores nothing. A shell without the variable sends an empty password, which
   the lab refuses.

**Which repositories match.** The lab serves one repository per URL, so the remote goes only to the
repository the URL names. Each URL is reduced to its repository name: the last part after a `/` or
`:`, without a trailing `/` or `.git`, in lowercase. The remote is added only when the lab URL
yields a name and that name equals the name of the checkout's `origin`. Host and owner are not
compared.

| `AGENTIHOOKS_LAB_REMOTE` | `origin` | Gets `lab` |
|---|---|---|
| `http://<lab host>/agentihooks.git` | `git@github.com:The-Cloud-Clockwork/agentihooks.git` | Yes, both name `agentihooks` |
| `http://<lab host>/agentihooks.git` | `https://github.com/the-cloud-clockwork/agentihooks-bundle` | No, the origin names `agentihooks-bundle` |
| unset or empty | any | No |

**Where it runs.**

| Workspace | When | Repository that gets the remote | When git fails |
|---|---|---|---|
| `wt.sh new` and `wt.sh tmp` (the `worktree` skill) | After the worktree is created, before it is leased | The primary checkout, so every worktree of that repository shares the remote and its helper | Prints `wt: the lab remote was not set in <repo>` on standard error; the worktree is still created and its path printed |
| `agentihooks swarm <slug> publish --via lab` | Before the lab push | The task worktree's repository | Refuses the publish; see [Publishing through the lab](../pillars/swarm.md#publishing-through-the-lab) |
| Swarm v2 workspace prepare (`scripts.swarm_v2.workspaces.prepare`) | When a prepare builds a new workspace, the first for a task or for a newer generation, after the bare mirror is cloned or verified and fetched. A prepare that replays the workspace already recorded for the same generation skips it | The bare mirror, so the workspace worktree cut from it shows the remote | A git that cannot start or does not finish fails the prepare with `git <command> did not finish`. A `git remote` or `git config` that exits with an error is not reported, and the mirror can lack the remote or its helper |
| The hive daemon, `agentihooks hive run` | Once at start, after it loads the operator's env files and before its first quota refresh | Every checkout under `HIVE_REPO_ROOT` (default `~/dev`): its direct children and one level into a child that is not a checkout itself | A git that cannot start or runs past 60 seconds in one checkout prints `hive: the lab remote was not set in <path>: <error>`; the daemon goes on with the next checkout. A `git remote` or `git config` that exits with an error is not reported, and the checkout can lack the remote or its helper |

The daemon carries its checkouts only at start. A checkout cloned while it runs gets the remote at
the next daemon start, the next `wt.sh new` or `wt.sh tmp` from it, or the next
`publish --via lab` in it. The daemon reads the two variables from its own environment once the
operator env files are loaded; a hive whose daemon lacks `AGENTIHOOKS_LAB_REMOTE` carries nothing.

#### Verify the lab remote

```bash
git -C <checkout> remote -v
git -C <checkout> config --get-regexp '^credential\..*\.helper$'
test -n "${AGENTIHOOKS_LAB_REMOTE_TOKEN:-}" && echo set || echo unset
```

A fresh worktree of a matching repository lists the lab beside origin:

```text
lab	http://<lab host>/agentihooks.git (fetch)
lab	http://<lab host>/agentihooks.git (push)
origin	git@github.com:The-Cloud-Clockwork/agentihooks.git (fetch)
origin	git@github.com:The-Cloud-Clockwork/agentihooks.git (push)
```

The second command prints one `credential.<lab url>.helper` line whose function names
`$AGENTIHOOKS_LAB_REMOTE_TOKEN` and holds no token. The third tells whether the shell that will push
holds the token, without printing it.

#### Troubleshooting the lab remote

| Symptom | Cause | Check |
|---|---|---|
| `git remote -v` lists no `lab` | The process that made the workspace had no `AGENTIHOOKS_LAB_REMOTE`, or the URL names another repository | `test -n "${AGENTIHOOKS_LAB_REMOTE:-}" && echo set \|\| echo unset` in that process's environment, then compare the last part of the lab URL with `git remote get-url origin` |
| A hive checkout lacks `lab` while a fresh worktree has it | The checkout was cloned after the daemon started, or the daemon started without the variable | Restart the daemon, or run `wt.sh new` against that checkout |
| `lab` points at an old URL | The workspace was carried before the URL changed | The next `wt.sh new`, `publish --via lab` or daemon start sets the new URL; the old URL's helper entry stays and is unused |
| A push to `lab` fails authentication | The pushing shell has no `AGENTIHOOKS_LAB_REMOTE_TOKEN`, so the helper sends an empty password, or it holds another value than the lab expects | The third verify command; set the variable in the environment the agent runs in |
| A push to `lab` fails with `Failed to connect` | The lab host is not reachable from this machine | `git ls-remote lab` from the same host lists the lab's grade refs once the host reaches the lab; until then, publish without `--via lab` |
| `wt: the lab remote was not set in <repo>` | `git remote` or `git config` failed in the primary checkout, for example on a locked config file | Run `git -C <repo> remote -v`; the next `wt.sh new` tries again |

### Guards on a lab push

A `git push` to `lab` is a push for every guard, and the guards read its `branch=` push options as
the branches it writes, because the relay writes them on GitHub once the grade is green.

| Guard | A push to `lab` |
|---|---|
| Prepush guard | Needs the committed `HEAD` to pass `python -m scripts.ci_prepush`, as a push to origin does, in a repository that has that module. The guard skips the value of `-o` and `--push-option` when it reads the remote |
| [One push gate](../pillars/swarm.md#pushing-while-checks-run) | Held under the same conditions as a push to origin when one of its `branch=` options names the task's recorded branch, or any `branch=` option when the task records none. A lab push with no `branch=` option relays nothing and is never held |
| [Panic](../pillars/swarm.md#publishing-under-panic-mode) | Spared only when every branch its `branch=` options name is a registered fix, or, with no such option, when the checked out branch it grades is one; git config must not route the push elsewhere, as for any spared push |
| `push-stop` gate | Counts only `origin`: a commit is unpushed until an `origin` remote branch holds it, so a head pushed only to the lab still holds the session's stop |

```bash
git push -o branch=engineer-a lab HEAD:refs/grade/t1   # writes engineer-a once green: judged like a push of engineer-a
git push lab HEAD:refs/grade/t1                         # relays nothing: one push gate passes it
```

### Provisioning in the deployment

The deployment repository wires the secret in `k8s/manifests/agentihooks-swarm/github-webhook.yaml`,
added by pull request 1775 of the deployment repository, merged on 16 November 2026. The stream
needs no manifest: it came with agentihooks pull request #11427 and the hub creates it as above.

1. A `Password` generator, `agentihooks-ci-lab-notice`, makes a 48 character value with 8 digits
   and no symbols.
2. A `PushSecret`, `agentihooks-ci-lab-notice`, writes that value to the OpenBao key
   `k8s/agentihooks-ci-lab-notice`, property `secret`, through the `openbao` ClusterSecretStore.
   `updatePolicy: IfNotExists` writes only while the key is absent, so the value is generated once
   and its hourly resync leaves it alone. `deletionPolicy: None` keeps the key when the PushSecret
   is deleted.
3. The receiver's `ExternalSecret`, `agentihooks-swarm-github-webhook`, reads the key every five
   minutes into the Secret of the same name as `AGENTIHOOKS_CI_LAB_NOTICE_SECRET`, beside the
   webhook secret, the bus address and the bus credential.

```yaml
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
  name: agentihooks-ci-lab-notice
spec:
  refreshInterval: 1h
  updatePolicy: IfNotExists
  deletionPolicy: None
  secretStoreRefs:
  - name: openbao
    kind: ClusterSecretStore
  selector:
    generatorRef:
      apiVersion: generators.external-secrets.io/v1alpha1
      kind: Password
      name: agentihooks-ci-lab-notice
  data:
  - match:
      secretKey: password
      remoteRef:
        remoteKey: k8s/agentihooks-ci-lab-notice
        property: secret
```

The receiver's ExternalSecret carries the value under its template data and reads it from the same
key; only the lab notice lines are shown:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: agentihooks-swarm-github-webhook
spec:
  target:
    template:
      data:
        AGENTIHOOKS_CI_LAB_NOTICE_SECRET: "{{ .lab_notice_secret }}"
  data:
  - secretKey: lab_notice_secret
    remoteRef:
      key: k8s/agentihooks-ci-lab-notice
      property: secret
```

The swarm chart's `githubWebhook.secretName` names that Secret. The receiver Deployment loads the
whole Secret with `envFrom` and carries a `secret.reloader.stakater.com/reload` annotation naming
it, so a change to the Secret restarts the receiver with the new value:

```yaml
githubWebhook:
  enabled: true
  secretName: agentihooks-swarm-github-webhook
```

The receiver's NATS user, `swarm-github-webhook` in the deployment repository's
`k8s/values-overlays/agentihooks-swarm-nats/users.yaml`, may publish `ci.lab.head.*`. That
wildcard matches one token after `ci.lab.head`, which covers every subject in
[Heads it records](#heads-it-records).

The deployment repository's `tests/unit/swarm_nats/test_lab_notice.py` holds the ExternalSecret key
and its OpenBao reference, the PushSecret's `IfNotExists` and `None` policies, its store and
generator, and a generated length of at least 48.

To replace the value, write the new one to the same OpenBao key and property. The PushSecret leaves
an existing key alone, the ExternalSecret copies the new value within five minutes, and the reload
annotation restarts the receiver. Every notice sender needs the same value.

### Verify

```bash
kubectl -n agentihooks-swarm get pushsecret agentihooks-ci-lab-notice
kubectl -n agentihooks-swarm get externalsecret agentihooks-swarm-github-webhook
kubectl -n agentihooks-swarm get secret agentihooks-swarm-github-webhook --template='{{range $k,$v:=.data}}{{$k}} {{end}}'
```

The PushSecret reports `Synced`, the ExternalSecret reports ready, and the key listing names
`AGENTIHOOKS_CI_LAB_NOTICE_SECRET`. It prints key names only, never values.

```bash
kubectl -n agentihooks-swarm logs deploy/agentihooks-swarm-github-webhook --since=1h | grep answered
```

A push to a repository whose webhook reaches the receiver logs `event push answered 200`. With a
NATS credential that may read the stream, such as the hub's, list the stream and read its latest
pushed head:

```bash
nats --server "$AGENTIHOOKS_BUS_URL" --creds <credential file> stream info AGENTIHOOKS-CI-LAB-HEADS
nats --server "$AGENTIHOOKS_BUS_URL" --creds <credential file> stream get AGENTIHOOKS-CI-LAB-HEADS --last-for ci.lab.head.push
```

The receiver's own credential publishes only and cannot run these. The acceptance recorded on the
deployment pull request on 16 November 2026, after its promotion run 43811275374, is this check:
the PushSecret `Synced`, a 48 byte key in the receiver Secret, two push deliveries answered `200`,
and `ci.lab.head.push` at stream sequence 9 for an agentihooks head pushed by
`github-merge-queue[bot]`.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `/lab/head` answers `404` | The receiver started without `AGENTIHOOKS_CI_LAB_NOTICE_SECRET` | The Secret's key listing as above. A key in the Secret that the pod lacks means the pod predates it: `kubectl -n agentihooks-swarm rollout restart deploy/agentihooks-swarm-github-webhook` |
| Pushes answer `200` but the stream gains no head | Lab intake is off for the same reason, or the delivery names no head: a tag, a deleted branch, a closed pull request or a check whose name does not start with `lab` | [Heads it records](#heads-it-records) |
| The receiver Secret lacks the key | The PushSecret is not `Synced`, or the ExternalSecret has not refreshed | `kubectl -n agentihooks-swarm describe pushsecret agentihooks-ci-lab-notice` and the ExternalSecret's events |
| A notice answers `401` | It was signed with another value, sent without the header, or its body changed after signing | Sign the bytes sent, with the value the Secret holds |
| A notice answers `400` | A field is missing, empty or not a string, or the body is not JSON | [Send a head notice](#send-a-head-notice) |
| A GitHub delivery answers `400` while its signature is valid | The body is not JSON, or the delivery lacks a field the receiver reads; [Heads it records](#heads-it-records) says what was recorded before the answer | `kubectl -n agentihooks-swarm logs deploy/agentihooks-swarm-github-webhook` for the delivery id, then the delivery's payload in the GitHub webhook settings |
| A request answers `503` and the log shows `could not record the event` | The publish failed: the stream does not exist yet, or the NATS credential lacks `ci.lab.head.*` | `stream info` as above; the hub creates the stream on its next start. On `/github` the pull request observations were recorded before the failure, and a GitHub redelivery records the head |
| A `pull_request` delivery, merge queue actions included, or a completed `check_suite` delivery answers `503` with `nats: no response from stream` in its traceback | The receiver's NATS credential allows the logical `swarm.*.pull.*`, `swarm.*.merge.*` and `swarm.*.checks.completed` subjects but not their `legacy.swarm.*` form, so its unscoped events log keeps the logical subject, which no stream holds ([Existing swarms through the subject change](swarm-bus-scope.md#existing-swarms-through-the-subject-change)). The pull request observation fails first, so the lab head of the same delivery is lost too. This was the cause on 19 November 2026 (#12458) | `/connz?auth=true` on each bus node's monitoring endpoint lists the receiver connection's publish allow list, which must name the `legacy.swarm.*` form of its eight pull, merge and checks subjects; `/subsz?test=swarm.<swarm>.pull.pushed` matches nothing. Grant them to `swarm-github-webhook` in the deployment repository's `users.yaml`, as its pull request 1911 did, then redeliver from GitHub |

## Lab remote service

The lab remote is the git endpoint that [Push a head to the lab remote](#push-a-head-to-the-lab-remote)
pushes to. It serves one bare repository, `agentihooks.git`, over git's smart HTTP protocol to
callers that hold the lab remote token. The deployment repository's `ci-lab` chart runs it beside
the scheduler in the `ci-lab` namespace, and the `ci-lab` ArgoCD Application syncs that chart from
the deployment repository's `dev` branch. Pull request 1829 of the deployment repository added it
on 19 November 2026.
That repository's `tests/unit/ci_lab/test_remote.py` renders every object below by name and runs
`serve.py` against a real bare repository to check its authentication, push and error answers.

### Where it runs

| Object | What it does |
|---|---|
| StatefulSet `ci-lab-remote` | One replica on the scheduler's image, `ghcr.io/the-cloud-clockwork/agentihooks-swarm`, so the image updater that moves the scheduler moves the remote too. It runs as user 10001 with a read only root filesystem and no service account token, and takes the scheduler's node affinity, which keeps it off the pools in the chart value `scheduler.excludedPools`. It requests 100m of CPU and 256Mi of memory and may use up to 1Gi of memory. Its `reloader.stakater.com/auto` annotation makes Reloader restart the pod when the Secret `ci-lab-remote` changes |
| Init container `init` | Runs `git init --bare /srv/git/agentihooks.git` on every start. On a repository that already exists git reinitializes it and keeps every ref and object |
| Container `remote` | Runs `python /etc/ci-lab-remote/serve.py`, listening on port 8790, named `http`. A TCP readiness probe on that port runs every 10 seconds |
| Claim `repo` | `local-path`, `ReadWriteOnce`, 20Gi, mounted at `/srv/git`. It holds the repository and the request bodies spooled during a push |
| Volume `scratch` | An `emptyDir` of at most 2Gi mounted at `/tmp` |
| ConfigMap `ci-lab-remote` | `serve.py`, `gitconfig`, `pre-receive` and `post-receive`, mounted read only at `/etc/ci-lab-remote`. The pod template carries a checksum of these files, so a change to one rolls the pod |
| Service `ci-lab-remote` | Headless, port 8790. Pods reach the remote at `http://ci-lab-remote.ci-lab.svc:8790/agentihooks.git` |
| Service `ci-lab-remote-lan` | `LoadBalancer` on the MetalLB address in the chart value `remote.lan.address`, port 80. LAN hosts reach the remote at `http://<lab remote address>/agentihooks.git`, and tailnet hosts reach the same URL through the tailnet's route to the LAN |
| NetworkPolicy `ci-lab-remote` | Admits TCP 8790 only from the ranges in the chart values `lan.sources` (the LAN and the pod network) and `remote.tailnet` (the tailnet). Egress goes only to cluster DNS, the `kube-dns` pods in `kube-system` on port 53 over UDP and TCP, and to the bus, the NATS pods in the namespace in the chart value `bus.namespace` on `bus.port` (4222). The bus route is held for the grade hook; see [Grade hook environment](#grade-hook-environment) |

The remote speaks plain HTTP on both addresses. On the LAN the token crosses the network in clear
text; the tailnet encrypts its own traffic.

### How it answers git

`serve.py` runs `git http-backend` once per request, because the image has git but no web server.
It answers one request per connection and ends a request whose client sends nothing for 300
seconds.

- **Authentication.** A request must carry the token as `Authorization: Bearer <token>`, or as
  basic credentials with the user `lab` and the token as the password. The server compares the
  header in constant time. Every other request gets `401` with an empty body and two challenges,
  `WWW-Authenticate: Bearer realm="ci-lab"` and `WWW-Authenticate: Basic realm="ci-lab"`. Git then
  asks its credential helpers, and with none that answers it stops with `could not read Username`.
- **The token stays in the server.** The server takes `CI_LAB_REMOTE_TOKEN` out of its own
  environment at start, so `git http-backend` and the hooks never receive it. An empty token stops
  the server at start with `the lab remote needs a non empty token`.
- **Reads.** A token holder lists and fetches every ref, over git protocol version 0 or 2.
- **Pushes.** The `gitconfig` the container names in `GIT_CONFIG_GLOBAL` turns on
  `http.receivepack`, advertises push options and points `core.hooksPath` at `/etc/ci-lab-remote`.
  The `pre-receive` hook refuses the whole push when any ref it updates lies outside
  `refs/grade/*`. It prints `lab remote: only refs/grade/* takes pushes, refused <ref>` for each
  such ref; no ref of that push lands and `post-receive` does not run. Git's defaults hold for the
  rest: a grade ref may be force updated or deleted, so an agent can push a rebased head to the same
  ref.
- **Push options.** Because push options are advertised, `git push -o branch=<name>` reaches the
  hooks as `GIT_PUSH_OPTION_COUNT` and `GIT_PUSH_OPTION_0`, and hook output reaches the pusher as
  `remote:` lines.
- **Pack bodies.** Git sends a pack larger than its `http.postBuffer` in chunked encoding. The
  server spools every request body to an unnamed file on the `repo` claim before it starts the
  backend, so the backend reads the whole body with its length. A push needs room on the claim for
  its spooled body as well as for the objects git keeps.
- **Errors.** A URL that names a repository other than `agentihooks.git` gets the backend's `404`.
  A backend that ends without writing its headers gets `500`.
- **Logs.** The server logs each request line and its answer code on standard error, so the pod log
  shows every `401`, `404` and `500`. Request lines carry no credentials.

The first push into the empty repository carries the whole history of the pushed head. Later pushes
leave out the objects reachable from the advertised refs the pushing clone already holds. A clone
that holds none of the lab's grade heads sends its whole history again; `git config push.negotiate
true` makes git find the commits it shares with the lab before it builds the pack.

### Grade hook environment

The `post-receive` hook runs `python -m scripts.ci_lab.remote.post_receive` from the image with the
container's environment; [Push a head to the lab remote](#push-a-head-to-the-lab-remote) describes
what it publishes and prints. It reads `CI_LAB_URL`, `CI_LAB_REPOSITORY`, `CI_LAB_REMOTE_ACTOR` and
the `AGENTIHOOKS_BUS` settings from that environment, and needs a bus login allowed to publish
`ci.lab.head.notice`. The chart gives the remote container only `CI_LAB_REMOTE_PORT`,
`CI_LAB_REMOTE_ROOT`, `GIT_CONFIG_GLOBAL` and `CI_LAB_REMOTE_TOKEN`. The chart's only bus login,
the Secret `ci-lab-nats` read from the OpenBao key in the chart value `openbao.nats`, belongs to the
scheduler, so the remote does not share it; a follow up gives the remote its own login and the
hook's settings. The
NetworkPolicy already lets the pod reach the bus, and nothing on the remote uses that route yet.

Until then the hook fails on every grade push after git has written the ref: the push lands, the
pusher reads the hook's error in place of a grade url, and the lab records no head. Reads never run
a hook, so `git ls-remote` and fetches answer as described above.

### Token and client settings

The chart generates the token and keeps it in OpenBao, as the deployment does for the
[lab notice secret](#provisioning-in-the-deployment):

1. A `Password` generator, `ci-lab-remote-token`, makes a 48 character value with 8 digits and no
   symbols.
2. A `PushSecret`, `ci-lab-remote-token`, writes that value to the OpenBao key `k8s/ci-lab-remote`
   (`secret/k8s/ci-lab-remote`), property `token`, through the `openbao` ClusterSecretStore.
   `updatePolicy: IfNotExists` writes only while the key is absent, so the value is generated once
   and its hourly resync and every redeploy leave it alone. `deletionPolicy: None` keeps the key when
   the PushSecret is deleted.
3. The `ExternalSecret` `ci-lab-remote` reads the property every five minutes into the Secret
   `ci-lab-remote` as `CI_LAB_REMOTE_TOKEN`, and the remote container takes that key as an
   environment variable when it starts. When the Secret changes, Reloader restarts the pod, so the
   server serves the new value without a hand restart.

Every client uses two settings and passes the token by reference:

| Name | Value |
|---|---|
| `AGENTIHOOKS_LAB_REMOTE` | The lab remote URL: `http://<lab remote address>/agentihooks.git` on a host on the LAN or the tailnet, or `http://ci-lab-remote.ci-lab.svc:8790/agentihooks.git` in a pod |
| `AGENTIHOOKS_LAB_REMOTE_TOKEN` | The value of the `token` property of `k8s/ci-lab-remote` |

The chart writes neither setting outside the `ci-lab` namespace. A host takes both from an
agentihooks env file under `~/.agentihooks/`, which agentihooks loads into the sessions it
launches. The deployment repository's hive template maps neither setting; a hive gets the pair only
once both are mapped into its hive settings from the same OpenBao property, after the PushSecret has
written it. The lab mirrors in the
[Environment](#environment) table read the same pair.

### Use it

`wt.sh`, swarm v2 workspaces and the hive daemon add the `lab` remote and its credential helper
themselves when both settings are set; [Workspaces that carry the lab remote](#workspaces-that-carry-the-lab-remote)
says where and when. By hand, in an agentihooks checkout:

```bash
git remote add lab "$AGENTIHOOKS_LAB_REMOTE"
git config credential."$AGENTIHOOKS_LAB_REMOTE".helper \
  '!f() { if test "$1" = get; then printf "username=lab\npassword=%s\n" "$AGENTIHOOKS_LAB_REMOTE_TOKEN"; fi; }; f'
git config push.negotiate true
git ls-remote lab
git push -o branch="$(git branch --show-current)" lab HEAD:refs/grade/<task>
```

The helper answers git's `get` request for the lab URL alone, with the user `lab` and the token
read from the environment when git asks. Git config holds the variable's name, never its value, and
the helper stores nothing. A shell without `AGENTIHOOKS_LAB_REMOTE_TOKEN` sends an empty password,
which the lab answers `401`.

### Verify

```bash
kubectl -n ci-lab get statefulset ci-lab-remote
kubectl -n ci-lab get pushsecret ci-lab-remote-token
kubectl -n ci-lab get externalsecret ci-lab-remote
kubectl -n ci-lab get secret ci-lab-remote --template='{{range $k,$v:=.data}}{{$k}} {{end}}'
kubectl -n ci-lab get service ci-lab-remote ci-lab-remote-lan
```

The StatefulSet shows `1/1` ready, the PushSecret `Synced`, the ExternalSecret ready, the key
listing `CI_LAB_REMOTE_TOKEN` and the LAN Service its external address. The key listing prints key
names only, never values.

Without kubectl or the token, a request that carries no token shows whether the server is reachable
and answering:

```bash
curl -s -D - -o /dev/null "$AGENTIHOOKS_LAB_REMOTE/info/refs?service=git-upload-pack"
```

The answer is `401` with both challenges. On 19 November 2026, with the `ci-lab` Application on the
deployment repository's merge commit `c26d2c0c`, a hive pod asking
`http://ci-lab-remote.ci-lab.svc:8790/agentihooks.git` this way got:

```text
HTTP/1.1 401 Unauthorized
Server: BaseHTTP/0.6 Python/3.12.12
WWW-Authenticate: Bearer realm="ci-lab"
WWW-Authenticate: Basic realm="ci-lab"
Content-Length: 0
Connection: close
```

and `git ls-remote` of the same URL with no credential helper exited 128 with `could not read
Username`. A timeout or a refused connection in place of the `401` points at the `Failed to connect`
row of the troubleshooting table below. That hive carried neither client setting, and the hive
template maps neither, so on that day the authenticated listing below had not yet run from a hive or
from the workstation.

From a hive and from the workstation, with the helper above set:

```bash
test -n "${AGENTIHOOKS_LAB_REMOTE_TOKEN:-}" && echo set || echo unset
git ls-remote "$AGENTIHOOKS_LAB_REMOTE"; echo "exit $?"
```

An empty repository lists nothing and exits 0. Once heads are pushed, each line is a commit and its
`refs/grade/<task>` ref. A missing or wrong token fails with `could not read Username` or
`Authentication failed`, and the pod log shows the `401`:

```bash
kubectl -n ci-lab logs ci-lab-remote-0 -c remote --since=10m
```

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `git ls-remote` fails with `could not read Username` | The request carried no token, so the lab answered `401` and no credential helper answered git | The `test -n` line above, then the helper with `git config --get-regexp '^credential\..*\.helper$'` |
| `Authentication failed` while the token is set | The value differs from the one the pod read at start: another copy, or a value replaced in OpenBao that the ExternalSecret has not copied yet, which takes up to five minutes before Reloader restarts the pod | Map the value again from `k8s/ci-lab-remote`; after a replacement, compare the ExternalSecret's last refresh with the pod's start time in `kubectl -n ci-lab get externalsecret ci-lab-remote` and `kubectl -n ci-lab get pod ci-lab-remote-0` |
| `Failed to connect` or a timeout | The host is outside the NetworkPolicy's ranges, the LAN Service has no external address, or the pod is not ready | `kubectl -n ci-lab get service ci-lab-remote-lan` and the StatefulSet's ready count |
| A push is rejected with `lab remote: only refs/grade/* takes pushes, refused <ref>` | One of its refs lies outside `refs/grade/*`, which rejects the whole push | Push `HEAD:refs/grade/<task>` alone |
| A grade push lands but prints a hook error in place of `lab grade:` | The remote container lacks the hook's settings | [Grade hook environment](#grade-hook-environment) |
| A request answers `404` | The URL names a repository other than `agentihooks.git` | `AGENTIHOOKS_LAB_REMOTE` ends in `/agentihooks.git` |
| The pod restarts with `the lab remote needs a non empty token` | The Secret holds an empty `CI_LAB_REMOTE_TOKEN` | The `token` property in OpenBao and the ExternalSecret's events |
| The pod stays in `CreateContainerConfigError` | The Secret `ci-lab-remote` does not exist yet because the ExternalSecret has not synced | `kubectl -n ci-lab describe pushsecret ci-lab-remote-token` and `kubectl -n ci-lab describe externalsecret ci-lab-remote` |

To replace the token, write the new value to the same OpenBao key and property. The PushSecret
leaves an existing key alone, the ExternalSecret copies the value within five minutes, and Reloader
restarts the pod once the Secret changes. Then give every host and hive the new value; until a client
has it, the lab answers its requests `401`.

## Digest specification

A verdict is stored under the key of the inputs it judged. `scripts.ci_lab.digests` builds every
key; the builders are pure and read no file, clock or network.

- `digest(record)` is the sha256 hex digest of the record's canonical JSON: keys sorted, compact
  separators `,` and `:`, non ASCII escaped. This is the record form of `scripts/ci_input_key.py`.
- `blob_id(data)` is the git blob id of a file's bytes. Every file input is named by its blob id,
  so a key reads the same whether the lab took it from `git ls-tree` or hashed the file itself.
- A key reads `<kind>-v<schema>-<digest>`. The digest covers the kind, the schema version and the
  inputs, so a schema bump in `SCHEMAS` is a cold cache for that kind, never a reinterpretation.
- Mappings and sets are order free: the same entries in any order give one key. Sequences keep
  their order where order changes the run, such as a pytest invocation.
- The day, the run id, the branch name and the author are never inputs.

| Kind | Builder | Schema | Unit |
|---|---|---|---|
| static | `static_key` | 1 | one static gate over the files it grades |
| test | `test_key` | 1 | one test batch |
| stats | `stats_key` | 4, the stats cache schema | one test file's mutation stats |
| mutant | `mutant_key` | 1 | the mutants of one changed function |
| grade | `grade_key` | 1 | one grade of a head |

### Inputs per kind

| Kind | Input | What it is |
|---|---|---|
| `toolchain` | `interpreter` | Python version the unit runs on |
| `toolchain` | `tools` | Tool name to version, such as ruff, basedpyright, semgrep |
| `toolchain` | `lockfile` | Blob id of `.github/test-requirements.lock` |
| `toolchain` | `pyproject` | Blob id of `pyproject.toml`, which holds the tool and pytest configuration |
| `toolchain` | `image` | Digest of the worker image |
| `toolchain` | `environment` | The environment key: each variable the unit reads, with its value |
| `static` | `gate` | Gate name, such as `ruff` or `type_gate` |
| `static` | `head` | Path to blob id of every head file the gate grades |
| `static` | `base` | Path to blob id of every base file the gate compares against |
| `static` | `grader` | Digest of the grader script tree the gate runs: the gate script's import closure at the protected grader revision |
| `static` | `toolchain` | The toolchain above |
| `test` | `tests` | Test id to its fingerprint from `scripts/dep_manifest.py` |
| `test` | `opened` | Path to blob id of each file a test opened outside the manifest |
| `test` | `invocation` | The pytest argument list, plugins included, in order |
| `test` | `toolchain` | The toolchain above |
| `stats` | `path` | The test file |
| `stats` | `inputs` | Source to digest of every source the test file reaches |
| `stats` | `scope` | Source to changed lines, for the sources the test file reaches |
| `stats` | `measured` | Whether the inputs come from measured coverage |
| `stats` | `discovery` | Digest of the discovery inputs |
| `stats` | `sources` | Path to sha256 of each configuration file, `.github/test-requirements.lock` and `.github/test-excludes.txt` among them, and each `scripts/ci_mutation/` file |
| `stats` | `options` | The pytest options, in order |
| `stats` | `lockfile` | sha256 of the head's `.github/test-requirements.lock`, or null without one |
| `mutant` | `module` | Path of the module that holds the function |
| `mutant` | `function` | Name of the changed function |
| `mutant` | `definition` | Digest of the function's whole definition text, from `scripts.dep_manifest.unit_hashes` |
| `mutant` | `module_code` | Digest of the module level code, the `<module>` entry of `unit_hashes` |
| `mutant` | `names` | The mutant names of the function |
| `mutant` | `mutmut` | The pinned mutmut version |
| `mutant` | `runner` | Digest of the `scripts/ci_mutation/` tree |
| `mutant` | `covering` | Test id to fingerprint of every test that covers the function |
| `mutant` | `toolchain` | The toolchain above |
| `grade` | `repository` | Owner and name of the repository |
| `grade` | `head` | Head commit sha |
| `grade` | `base` | Base commit sha |
| `grade` | `tier` | Delivery tier: pull request, queue or promotion |
| `grade` | `grader` | The protected grader revision |

### Rules the keys rely on

- `stats_key` hashes exactly the stats cache identity of schema 4 with its own record form
  (default JSON separators), so its digest equals `StoredStats.identity` for the same inputs and
  the lab reuses the stats the cache already holds. Its file inputs are sha256 digests of the file
  bytes, the one exception to blob ids, because the stats cache identity hashes them that way.
  Its lockfile and pyproject inputs are the `.github/test-requirements.lock` and `pyproject.toml`
  entries of `sources`.
  Its runtime is the `lockfile` input, never the interpreter or installed packages of the process
  computing it, so the scheduler keys a stats unit exactly as the worker's stats cache stores it
  for the same head. Every CI install and the baked lab worker venv install from that file, with
  `.github/test-excludes.txt` as their excludes list, which `sources` keys.
- A change to `.github/test-requirements.lock` or `pyproject.toml` rekeys every unit kind. A grade
  is keyed by its head, which names both files.
- The lab runs pytest itself, so no workflow file under `.github/` changes a test verdict. The
  pytest invocation, which a workflow used to define, is the test input instead. This replaces the
  manifest's `.github/` prefix invalidation, as the lab's test expansion design specifies; the
  plan's earlier rule that any change under `.github/` invalidates every test fingerprint does not
  apply to lab keys.
- The test expander applies the manifest's fallbacks before it builds `tests` and `opened`: a test
  that reaches code by dynamic import, starts a process the tracer cannot follow or opens a file
  outside the manifest is fingerprinted on the whole measured source tree, and each file opened
  outside the manifest enters `opened` by blob id.
- Line numbers, the changed line scope and mutation clearances stay out of a mutant key; they apply
  at grade time.
- The expander marks a unit that reads the clock or the network nondeterministic, and the store
  never caches its verdict, so no builder keys one.

## Scheduler loop

`scripts.ci_lab.scheduler.Scheduler.tick(now_ms)` runs one grading pass:

1. Intake reads new heads from the `ci.lab.head.>` lab heads stream and admits only actors on the
   runner trust list of the repository's dev branch. A head on the fix branch of any swarm in
   panic grades in the hotfix class. An admitted plant head, a push to a branch under
   `refs/heads/diffcheck/lab-plant-`, then takes as its base the merge base of the head with the
   dev head the remote lists (`MirrorExpander.merge_base`), before it is held or keyed, and grades
   in the pull request class with that commit as base and grader, so both twins of a plant cut
   from one dev commit share it. When git cannot resolve one, the pass prints
   `cannot resolve the dev merge base of plant head <head>: <error>` and never holds the head.
2. Each head waits in its lane (repository, branch and class) for the quiet window, 30 seconds by
   default; a newer head of the lane restarts it and a hotfix waits for none. A pass starts at most
   eight heads whose window ended (`STARTS_PER_PASS`): the most urgent class first (hotfix, pull
   request, queue, promotion, backfill) and, within a class, the head that has waited longest. A head
   still quiet or held by a pause takes no place; a head put back for its manifest fetch takes one,
   since its expansion ran on the pass. The other heads wait for a later pass, so every pass reaches
   steps 5 to 8 however many heads wait.
3. Expansion reads the grader's test workflow and input key registry and the head and base trees
   from the git mirror with `git ls-tree` and `git show`, never a checkout, and keys one static
   unit per gate step, described in [Static units](#static-units). A merge group, promotion or
   backfill head also gets the `unit` job's test units, described in [Test units](#test-units),
   and a promotion or backfill head its mutation units, described in
   [Mutation units](#mutation-units).
4. The verdict store answers what it can. A newer head of the lane drops the older head's pending
   units, carries its leased and finished jobs and cancels its check runs. A push grade of a tree a
   merge group grade passed carries that grade's units for every gate both grades hold: the unit
   suite and `test-count`, since the queue rules require lint, type-gate and semgrep to be skipped. The
   rest is held for the budget.
5. Held units are enqueued as `scripts.ci_lab.budget.admits` lets them in, the most urgent grade first.
   Every unit of a class the day's core budget does not shed is enqueued; a shed class enqueues nothing
   until the budget resets the next UTC day, though its units already in the queue still run. Backfill
   takes only the slots workers offered free on a lease poll within the ready window after the units
   already waiting in the queue. A newer head of the lane drops the older head's held units.
6. Expired leases requeue while `lab.retry.lost` of the repository's dev `delivery.yaml` allows (once by
   default); the next loss is `lost`.
7. Finished jobs are collected. A job whose outcome `retry.decide` allows another run under
   `lab.retry` goes back to pending with one more attempt, so a `crash` reruns once by default.
   Otherwise a graded outcome with an uploaded report becomes a verdict in the
   store; `lost`, `crash` and `cannot_run` stay out of it and make their grade red. A lease
   completes only with an outcome `scripts/ci_lab/outcomes.py` lists in `COMPLETED`: `passed`,
   `failed`, `gate_failed`, `survived`, `crash` or `cannot_run`. Any other outcome is refused
   with `403`, records nothing and stores no verdict; the lease stays held until it expires.
8. Check runs move `queued` on receipt, `in_progress` on the first unit a worker starts and
   `completed` on the last, one per gate with units and `required` for the whole grade. A pull
   request or hotfix grade also posts one check run `completed` with conclusion `skipped` on its
   first pass for every gate the base's `lab.authoritative` lists that it runs no unit of. A completed
   check run never moves again. The grade state keeps each check run's id, and the scheduler seeds
   its poster with that id and the last status it posted before the next move, so a restarted
   scheduler advances the same check run instead of opening a second one. A grade of a head pushed
   to a `refs/grade/*` ref records the same moves and posts none, leaving its check runs to the
   relay. A gate that the grader revision's `delivery.yaml` lists as authoritative for the grade's
   class posts as `lab/<gate>`, the check run the lab-verdict job waits for: `lab.authoritative`
   for pull request and hotfix grades and `lab.queue.authoritative` for merge group grades, read
   from the base, so a head never widens either list. Every other gate and `required` post as
   `lab-shadow/<gate>`, the runs the shadow comparison reads, and promotion and backfill grades
   list no gate. The grade state keeps the list, so a restarted scheduler names each check run as
   it did before. A refused post ends nothing: the pass records
   `held <check> <status> on <head> for a later pass: <error>` and goes on grading, and the first
   pass after a one minute hold (`POST_HOLD_MS`) posts the check run again. A refusal of the App
   token (`GitAuthRefused`) or of the network (`OSError`) holds the grade posts of that repository;
   a refusal GitHub answers (`PostRefused`) holds only that grade's posts, kept in the grade state
   as `hold`, so the other grades keep posting and a restarted scheduler keeps the hold. A grade
   whose units are all final closes only once every one of its check runs is posted. The failed
   check of a head that cannot expand is outside these holds: it is tried once, since its dropped
   head keeps no grade to retry from. `GitHubAppAuth.get_token` names the permissions a refused
   token asked for and GitHub's answer, such as
   `GitHub App cannot supply scoped checks write credentials: GitHub answered 422` for the check
   runs token, or `GitHub answered 201 with a token outside that scope` when the minted token lacks
   a requested permission or repository; for a refused check runs token, read the App
   installation's `permissions.checks` in the org installations listing.

Expansion frames a head by its class (`scripts.ci_lab.scheduler.frame`): a pull request or hotfix grade
keeps the lab gates the pull request rules require and names every other required job as a gate without
units, with no test scope and no mutation; a merge group grade does the same for the queue suite; a
promotion or backfill grade keeps every lab gate. A gate without units passes, so a grade turns red only
through what the lab grades.

A pull request or hotfix grade reads `lab.authoritative` from the `delivery.yaml` of its base, the
grader revision, so the head cannot widen it. Each listed gate the frame gives no unit, such as `unit`,
`unit-selected`, `shard-check` or `mutation`, which those classes never expand, lands in the grade
state's `idle` list and gets its `skipped` check run on the first pass. The `lab-verdict` job then
reads `skipped` for it instead of failing the gate when its 20 minute wait ends. `Gate — Required`
accepts `skipped` only for a gate it does not require on that head, so a lab that skips a gate the
head requires keeps the pull request red.

A gate passes when every unit passed. The `required` conclusion is
`scripts.ci_lab.scheduler.fold`, the rules of the `Gate — Required` step of the test workflow over
the gates as the needs of a run of the grade's class: pull request and hotfix grades fold as
`pull_request`, queue grades as `merge_group`, promotion as `push` and backfill as
`workflow_dispatch`. `tests/fixtures/ci_lab/gate_required.json` records the step's verdict on cases
built from the needs of real runs; one test holds `fold` to every case and another holds the cases
to today's step.

The scheduler service (`scripts.ci_lab.scheduler.serve`) ticks the lab relay after each pass and
prints the relay's actions after the pass's own. `main` builds the relay once at start on its own
lab heads consumer, the `ci-lab-relay` durable filtered to `ci.lab.head.notice`, which bus
provisioning creates beside the scheduler's `ci-lab-scheduler` durable. A green grade of a pushed
`refs/grade/<task>` head with a `branch` option then reaches that branch, its check runs and its
draft pull request. The relay keeps the id of each check run it creates on the grade's state under
the check run's name as GitHub answers and seeds its poster with those ids before it posts, so a
relay retried after the service restarts, such as one whose pull request call failed after its
posts, creates no second completed check run. The ids the scheduler keeps under gate names for its
own posts, such as shadow check runs of the same grade, never seed the relay. A relay whose git or
HTTP call fails is named `retry <head> for <branch>: <error>` and stays pending for the next tick.
A relay whose push, check runs or pull requests token the App refuses is named
`held <head> for <branch> for a later pass: <refusal>`, stays pending and holds every pending head
of its repository for `POST_HOLD_MS`; the heads of other repositories still relay in the same
tick, and the first tick after the hold relays the held heads again. The hold lives in the relay's
memory, as the scheduler's repository post hold does, so a restarted service tries the held heads
on its first tick.

A grade's state is one JSON document at `agentihooks-ci:grade:<run>`, where `<run>` is the grade
key of the repository, head, base, class and grader revision. Live grades sit in
`agentihooks-ci:grades`, held heads in `agentihooks-ci:waiting`, a lane's latest run at
`agentihooks-ci:lane:<repository>|<lane>|<class>` and a passing merge group grade at
`agentihooks-ci:tree:<repository>|<tree>`. A rerequested check run grades the head again under
`<run>-<now_ms>`, bypassing negative verdicts in the store.

Known limits: a lab head event carries no branch yet, so a head's lane is its own sha and a newer
head supersedes nothing until intake records the branch; expansion keys static gates, test units
and mutation units only, so the other jobs the lab does not run yet pass by their frame; a
rerequested grade reuses the finished jobs of its own head.

### Static units

Expansion keys one `static` unit per gate step of the static jobs of the grader revision's Tests
workflow: `lint`, `type-gate`, `semgrep` and `test-count`, the jobs
`scripts.ci_lab.static_gates.STATIC_JOBS` names. The grader revision is the head of a promotion or
backfill grade and the base of every other grade. `scripts.ci_lab.scheduler.MirrorExpander.expand`
reads three inputs at that revision from the git mirror and passes them to
`scripts.ci_lab.static_gates.discover`:

| Input | Read with | Passed to `discover` as |
|---|---|---|
| `.github/workflows/test.yml` | `git show <grader>:.github/workflows/test.yml` | The workflow text |
| `.github/input-keys.json` | `git show <grader>:.github/input-keys.json` | The input key registry |
| Every `.yml` and `.yaml` file directly in `.github/workflows` | `MirrorExpander._workflows`: `git ls-tree -r` of the grader revision, then one `git cat-file --batch` for the matching blobs | The reusable workflows: each file parsed with `yaml.safe_load`, an empty file as `{}`, keyed by file name |

The reusable workflows hold the same files, keys and values as `scripts.ci_wiring.load` returns
for a checked out tree. A file in a folder below `.github/workflows` is not read. GitHub does not
run a reusable workflow from such a folder either.

`discover` walks the static jobs in that order and labels their steps:

- A job with its own `steps` labels them with the job name, such as `lint`.
- A job with `uses: ./.github/workflows/<file>` looks `<file>` up in the reusable workflows. It
  labels the steps of each job of that workflow `<job>/<reusable job>`. The Tests workflow's
  `semgrep` job calls `semgrep.yml`, whose jobs are `scan`, `feature-tests` and `charts`.
- A job of that workflow that calls a further workflow with its own `uses:` has no steps to label.
  `discover` skips it and grades the other jobs of the workflow, and
  [Jobs the lab leaves to Actions](#jobs-the-lab-leaves-to-actions) names it.

A step is a gate step when it has a `run` that calls a tool `static_gates.TOOLS` names or runs
`python` on any module or script other than `basedpyright`, `ruff` and `venv`
(`static_gates.UNGATED_MODULES`). Its gate name is the label, a `/`, then the step's `id`, or
else its name (or the first line of its run) in lower case words joined by `-`, such as
`semgrep/scan/fail-on-findings-new-against-the-base`. Each unit names one gate step, and
its key is `static_key` over the gate name, the head files under the gate's registry paths, the
base files under its base paths, the grader revision and the toolchain (see
[Inputs per kind](#inputs-per-kind)). The plan groups units by label, so each label is one gate
with its own check run.

On dev, `python -m scripts.ci_lab.static_gates --root .` lists 43 gate steps:

| Gate | Steps from | Gate steps |
|---|---|---|
| `lint` | The `lint` job of `test.yml` | 28 |
| `type-gate` | The `type-gate` job of `test.yml` | 2 |
| `semgrep/scan` | The `scan` job of `semgrep.yml` | 4 |
| `semgrep/feature-tests` | The `feature-tests` job of `semgrep.yml` | 2 |
| `semgrep/charts` | The `charts` job of `semgrep.yml` | 3 |
| `test-count` | The `test-count` job of `test.yml` | 4 |

Before pull request
[#11978](https://github.com/the-cloud-clockwork/agentihooks/pull/11978), the expander passed only
the workflow and the registry. The `semgrep` job's call to `semgrep.yml` then failed discovery
with `semgrep uses ./.github/workflows/semgrep.yml, which was not given`, so every agentihooks head
was dropped as `cannot expand` and the lab graded none at any class.

#### What a gate's steps read

A step's `if` condition and the `${{ }}` expressions in its `env` read context names.
`discover` records four things on each `StaticGate`:

- `producers`: the earlier steps of the gate's job whose `steps.<id>.outputs.<name>`,
  `steps.<id>.outcome` or `steps.<id>.conclusion` the gate reads, followed on through the names
  each such step with a `run` reads, in job order. A `uses` step can be a producer, but the lab
  never runs it, so its own `if` and `env` are never read.
- `reads`: every name the gate and its producers with a `run` read, sorted.
- `matrix`: each `matrix.<key>` in `reads` that the job's `strategy.matrix` holds at one value,
  with that value. A key that lists one value qualifies. A matrix written as an expression, or
  one with an `include`, gives none, since an `include` can add a combination with another value.
- `missing`: the names in `reads` the static executor cannot supply.

The static executor supplies these names:

| Name | Value |
|---|---|
| `inputs.base`, `github.event.pull_request.base.sha` | The lease's base revision |
| `github.sha`, `github.event.pull_request.head.sha` | The lease's head |
| `github.event_name` | `pull_request`, at every class, since a static unit's key holds no class |
| `github.run_id` | The lease's job id |
| `github.run_attempt`, `github.run_number` | `1` |
| `runner.temp` | The unit's run folder, the `$RUNNER_TEMP` its steps see |
| `matrix.<key>` | The value the gate's `matrix` records |
| `secrets.<NAME>` | The worker's `CI_LAB_SECRET_<NAME>`, or empty when the worker has no such variable. Only the secrets the gate reads join the context |
| `steps.<id>.outcome`, `steps.<id>.conclusion`, `steps.<id>.outputs.<name>` | Set as each producer runs |

Since the event is `pull_request` in every class, a lab unit runs or skips each step as a pull
request run of Actions does. A merge group, promotion or backfill grade can therefore differ from
that class's Actions run: a step whose `if` needs another event is skipped, and a step whose `if`
needs `pull_request` runs. Three lab gradable gates read the event name on dev, and
`tests/ci_lab/test_static_gates.py` pins that list, so a new one fails the test until someone
checks the pull request event keeps its verdict:

| Gate | In a lab unit |
|---|---|
| `lint/prove-the-mutation-gate-kills-with-the-real-test-and-fails-with-a-weakened-one` | Its `self-plant` producer runs, since it needs `pull_request` |
| `test-count/fetch-the-base-suite-count-recorded-on-dev` | Skipped: it also needs the `app-token` producer, a `uses` step. Every failure path of the step exits 0, so it never changes the job's verdict, and the count floor collects the base suite itself |
| `test-count/record-the-head-count-for-the-next-queue-group` | Skipped: it runs only on `merge_group`, with `continue-on-error`, so it never changes the job's verdict |

`scripts.ci_lab.run_static.run` runs the producers before the gate, in job order, in the gate's
sandbox and run folder, and gives each its own `GITHUB_OUTPUT` file. A `uses` producer, or one
whose `if` is false, has outcome and conclusion `skipped`. A producer that exits 0 has outcome and
conclusion `success`, and its `name=value` output lines become its outputs. An output that no step
wrote reads as empty. A producer that exits non zero, or runs out of time, ends the unit with its
own code and log, and the gate never runs, as Actions ends the job. Any other output line,
including a delimited multi line block, raises `<id> wrote an output line that is not name=value`
without the line's text.

The executor writes no secret value into the unit report or log. As Actions masks a secret, it
replaces each non empty secret value the gate read with `***` in the unit log and in the step
summary the report carries.

A gate with any missing name is not lab gradable. The expander leases no unit for it, so its
verdict stays with Actions. `python -m scripts.ci_lab.static_gates --root .` prints each gate's
producers by id, its `reads`, its `matrix` and its `missing`. On dev two `lint` gates are not lab
gradable:

| Gate | Missing |
|---|---|
| `lint/refuse-changelog-fragments-named-off-the-branch-or-opening-without-a-section-heading` | `github.head_ref`, `github.ref_name` |
| `lint/artifact-sanity-checks` | `github.base_ref`, `github.event.before`, `github.event.merge_group.base_sha` |

Before pull request
[#12486](https://github.com/the-cloud-clockwork/agentihooks/pull/12486), the executor supplied
neither `github.event_name`, `runner.temp` nor any `matrix.<key>`. Three of the four `test-count`
gate steps and `lint/prove-the-mutation-gate-kills-with-the-real-test-and-fails-with-a-weakened-one`
were not lab gradable, and `test-count` was not a static job.

Before pull request
[#12422](https://github.com/the-cloud-clockwork/agentihooks/pull/12422), the context held only
`inputs.base`. A gate that read any other name raised `no value for '<name>' in the context`
before its step ran, so its unit was requeued once and then lost.

#### Which grades run static units

[Expansion frames a head by its class](#scheduler-loop), and the frames keep these static gates:

| Class | Static gates with units |
|---|---|
| Pull request, hotfix | `lint`, `type-gate` and `test-count`. `semgrep` is framed as a gate without units, which passes. The pull request rules name the job `semgrep` (`scheduler.PULL_REQUIRED`), while its units sit under the `semgrep/scan`, `semgrep/feature-tests` and `semgrep/charts` labels |
| Merge group | `test-count`, the one static job the queue rules require (`scheduler.QUEUE_REQUIRED`) |
| Promotion, backfill | All six |

#### Jobs the lab leaves to Actions

The pull request rules also require `docs` and `writer-lock`. The lab frames both as gates without
units, so their Actions jobs keep the verdict. `scripts.ci_lab.static_gates.CI_ONLY` names each
with its reason, and `ci_only` lists them with one more kind of job:

| Job | Reason |
|---|---|
| `docs` | It builds the pages with the Jekyll image under `docker run`, and lab units run with no Docker daemon |
| `writer-lock` | It runs a Postgres container and calls nested reusable workflows, and lab units run with no Docker daemon or network |
| `<static job>/<job>` | A job of a workflow a static job calls that calls a further workflow. Its reason names that workflow |

`python -m scripts.ci_lab.static_gates --root . --ci-only` prints one JSON line per such job in
workflow order. On dev it prints:

```json
{"job": "writer-lock", "reason": "runs a Postgres container and calls nested reusable workflows, and lab units run with no Docker daemon or network"}
{"job": "docs", "reason": "builds the pages with the Jekyll image under docker run, and lab units run with no Docker daemon"}
```

#### A head that cannot expand

The pass that releases a head from its lane catches a `ValueError`, `KeyError`, `SyntaxError`,
`yaml.YAMLError`, `jsonschema.ValidationError` or failed git command from expansion. A promotion or
backfill head raises the `SyntaxError` when a changed Python file under `hooks/` or `scripts/` does
not parse, as mutation expansion compares that file with its base. It records
the action `cannot expand <head>: <error>` and drops the head: the head has already left
`agentihooks-ci:waiting` and is not put back, so it gets no grade. `Scheduler._refuse` then posts
one completed `lab-shadow/required` check run with conclusion `failure` on the head, whose summary
reads `cannot expand <head> against <base>: <error>`, so the head still shows a lab status. A head
with no plan has no authoritative gates, so the check posts under the shadow name. A post of that
check that the App token, GitHub or the network refuses is recorded as
`cannot post the failed required check on <head>: <error>` and not retried, since the dropped head
keeps no grade, and the pass goes on. A head pushed to a grade ref gets no check run, since
GitHub does not hold that head. A manifest fetch that raises `FetchPending` or
`FetchUnanswered` holds the head again instead, as
`holding <head>: <error>`, until the tenth `FetchUnanswered` in a row drops the head with this
check run; [Test units](#test-units) describes both. The scheduler service prints
each action on its own line:

```bash
kubectl logs -n ci-lab deploy/ci-lab-scheduler | grep 'cannot expand'
```

| Text after `cannot expand <head>:` | Cause |
|---|---|
| `<job> uses <uses>, which was not given` | A static job calls a workflow that is not a file directly in `.github/workflows` at the grader revision, such as a workflow of another repository (`<owner>/<repo>/.github/workflows/<file>@<ref>`), or the caller passed no reusable workflows |
| `<gate> names two gate steps` | Two gate steps resolve to the same gate name |
| `'steps'` or `'jobs'` | The called workflow has no `jobs`, or one of its jobs has neither `steps` nor `uses` |
| A YAML parse error | A workflow file directly in `.github/workflows` at the grader revision does not parse, even one that no static job calls |
| `no dependency manifest at or before <base>` | A merge group, promotion or backfill head needs [test units](#test-units) and no commit on the base's first parent chain has a stored or fetchable manifest |
| A `jsonschema` message, such as `0 is less than the minimum of 1` or `Additional properties are not allowed ('max' was unexpected)` | The grader revision's `delivery.yaml` breaks its schema. A hotfix, pull request or merge group head reads it for its authoritative gates and a merge group head also for its [entry limit](#configure-the-entry-limit), so every such head graded at that revision is dropped. The lines after the message name the schema rule and the instance path, such as `On instance['lab']['queue']['full_suite_entries']` |

#### A head no node mirror can hold

Each CI node's cache mirrors only the repository's `refs/heads/*` and `refs/pull/*/head` refs, so a
worker cannot fetch a commit no such ref reaches, such as the head of an ejected merge group whose
`gh-readonly-queue` branch is gone. Before the scheduler expands a released head it asks
`MirrorExpander.reachable`, which lists those refs with `git ls-remote` and accepts the head when a
ref names it or when the dev branch head, fetched into the scheduler's mirror, descends from it. It
never fetches the head itself, since GitHub may no longer serve it. Any non-zero exit of the
ancestor check reads as not reachable, including the exit 128 git gives for a head the scheduler's
mirror does not hold. A head that check refuses is retired with the action
`retiring <head>: no branch or pull request head ref of <repo> carries <head>`: it gets no grade and
no unit, so no worker leases one. A pull request head also gets one completed `lab-shadow/required`
check run with conclusion `failure` and that summary; a hotfix, merge group, promotion or backfill
head gets none. Such a pull request head is never the current head of a pull request, since GitHub
keeps `refs/pull/<n>/head` on each pull request's last head, so the post marks a superseded commit
and holds no merge. That post is tried once, like the cannot expand post: a refusal by the App
token, GitHub or the network ends the same action with
`; cannot post the failed required check on <head>: <error>`, is not retried, and the pass goes
on. A head pushed to a grade ref skips the reachability check, since the lab remote serves it. A
failed `git ls-remote` raises like any other failed git command, so the head takes the
[cannot expand](#a-head-that-cannot-expand) path.

#### The worker reads the same workflows

The static executor finds its gate again on the worker. `scripts.ci_lab.executors.gate(grader_tree,
name)` runs `discover` over the grader tree it cut for the lease, with `ci_wiring.load(grader_tree)`
as the reusable workflows, and returns the gate step the unit names. When that discovery raises,
or the grader workflow has no gate step of that name, the unit ends with no outcome. The worker
prints `ci-lab worker: unit <job> crashed: <error>` and its traceback to its standard error and the
lease expires, so the unit follows the expired lease rule of step 6 of the
[Scheduler loop](#scheduler-loop).

#### What a failed static unit's check run shows

A gate's check run text shows, ahead of its unit keys, the last 4096 characters of each failed
static unit's stored log, `lab/static/<digest>/log.txt`, with secret patterns redacted, in a fence
under the unit key, so a reader of the check run alone sees which diagnostic failed the gate. The
blocks together stay within half of GitHub's 65535 character text limit; past it the blocks that
fit end with a note that the later ones are left out. A passing unit, a failed unit with no stored
or an empty log, and the required check show only unit keys.

#### Limitations

- Only workflows directly in the grader's `.github/workflows` resolve. A static job that calls a
  workflow of another repository cannot expand.
- A reusable workflow resolves one level deep. A job of the called workflow that calls a third
  workflow gets no units, and its Actions job keeps the verdict; `--ci-only` names it.
- Every expansion parses each workflow file directly in the folder, so one file that does not
  parse drops every head the lab grades at that grader revision.
- Pull request and hotfix grades run no `semgrep` units, as the frame table above shows.

#### Verify

List the gate labels discovery finds in a checkout, which match the keys a promotion grade's plan
holds:

```bash
python -m scripts.ci_lab.static_gates --root . \
  | python -c 'import json,sys; print(sorted({json.loads(line)["job"] for line in sys.stdin}))'
```

On dev it prints
`['lint', 'semgrep/charts', 'semgrep/feature-tests', 'semgrep/scan', 'test-count', 'type-gate']`.
The `checks` of a promotion grade hold one key per gate with units, so the six static gates
appear there beside `unit`, the mutation gates the head has units for and `required`:

```bash
curl -s http://lab:8800/grades/<sha> | jq '.grades[] | select(.class == "promotion") | .checks | keys'
```

| Guard | What it holds |
|---|---|
| `tests/ci_lab/test_scheduler.py::test_the_mirror_expander_expands_the_real_tests_workflow_with_its_reusable_workflows` | `MirrorExpander.expand` over a git origin holding the repository's real workflows and registry keys one unit per gate step that `discover` lists, under the same labels, `semgrep/scan`, `semgrep/feature-tests` and `semgrep/charts` included |
| `tests/ci_lab/test_executors.py::test_gate_returns_a_gate_of_a_reusable_workflow_the_real_tests_workflow_calls` | `executors.gate` over the repository tree returns a `semgrep/` gate step equal to the one discovery lists |
| `tests/ci_lab/test_scheduler.py::test_the_mirror_expander_keys_static_gates_from_git_objects_without_a_checkout` | The reusable workflows hold only the `.yml` and `.yaml` files directly in the folder, an empty file as `{}` |

The first two tests map to the `lab-reusable-workflow-gates` entry of `feature-map.json`. Before
the fix, both failed with `ValueError: semgrep uses ./.github/workflows/semgrep.yml, which was not
given` in
[Tests run 44270664482](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44270664482/job/134686811797),
and both passed in
[Tests run 44272811567](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44272811567).

#### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| A head gets no grade and no check runs, and the scheduler log reads `cannot expand <head>: semgrep uses ./.github/workflows/semgrep.yml, which was not given` | The scheduler runs a release older than pull request #11978, so its expander passes no reusable workflows | `kubectl -n ci-lab get deploy ci-lab-scheduler -o jsonpath='{.spec.template.spec.containers[0].image}'` names a release that carries the fix |
| The same text names a workflow you just added to a static job | The called file is in a folder below `.github/workflows`, or not on the grader revision yet. A pull request, hotfix or merge group grade reads the base, so a new workflow counts only once the base holds it | `git ls-tree --name-only <grader> .github/workflows/` lists the file |
| Every head graded at one grader revision is dropped with a YAML error | A workflow file directly in `.github/workflows` at that revision does not parse | In a checkout of that revision, `python -c 'import pathlib,yaml; [print(p) or yaml.safe_load(p.read_text()) for p in sorted(pathlib.Path(".github/workflows").glob("*.y*ml"))]'` prints each file before parsing it, so the last name printed before the traceback is the file |
| The worker's standard error reads `ci-lab worker: unit <job> crashed: ValueError('semgrep uses ./.github/workflows/semgrep.yml, which was not given')` | The worker runs a release older than the fix while the scheduler expands with it | The `ci-lab-worker` image, as for the scheduler; the unit's lease expires and follows step 6 of the [Scheduler loop](#scheduler-loop) |
| A pull request grade passes `semgrep` with no units | The pull request frame names the job `semgrep` while its units sit under the `semgrep/` labels | Expected today; see [Which grades run static units](#which-grades-run-static-units) |

### Test units

`scripts.ci_lab.expand_tests.units` turns the dependency manifest into test units of kind `test`:
one per `expand_tests.batches` batch, with batches cut at `BATCH_SECONDS` (60) from the head's
`.test_durations`. Each unit's key is `digests.test_key` over its tests' fingerprints on the head
tree, the blob ids of the files its tests opened outside the manifest (their records' `outside`
list), the pytest invocation and the toolchain. The expander reads every head file in one
`git cat-file --batch` for the fingerprints.

A batch starts as one test file. A file is split only when its tests' summed durations pass 60
seconds and none of its tests carries an xdist group. `expand_tests._parts` then deals its tests,
longest first, each to the lightest part, and keeps the smallest part count from two up whose
parts each fit 60 seconds; when no count below the file's test count fits, every test is its own
batch. A test that `.test_durations` does not list counts 0 seconds, and a head with no
`.test_durations` file expands with no durations, so each of its test files is one batch.

| Head | Unit job |
|---|---|
| Pull request, hotfix | No test units; the manifest is not read |
| Merge group | The batches that hold a test the group's entries invalidate, or every batch when `interaction.decide` returns `FULL` |
| Promotion, backfill | Every batch; the verdict store is not consulted for the choice |

The [mutation units](#mutation-units) of a promotion or backfill head are built from the same
manifest and test fingerprints.

The manifest comes from `scheduler.StoredManifests`: the packed manifest at
`lab/manifests/<owner>--<name>/<commit>.json` (`objects.manifest_folder`) of the first commit on
the first parent chain of the head's base that has one. Runners cannot reach the lab store, so the
lab pulls each manifest itself: a commit among the newest `MANIFEST_DEPTH` (10) of that chain with
none stored goes through `manifests.fetch`, which finds the commit's dev push run of the Tests
workflow, lists the run's `dep-manifest-packed` artifact and downloads its archive with a token
the lab's GitHub App scopes to actions read (`manifests.Actions`, one per repository through
`manifests.Fetcher`). Its `packed.json` is written once at that commit's key, so a stored commit is
never fetched again. A commit without such a run or artifact, an expired artifact, an archive
GitHub answers is gone (410) and a manifest recorded for another commit fetch nothing; a refused
or unanswered request or an archive without a readable `packed.json` raises `manifests.FetchRefused`.
A request GitHub never answered or answered with a server error, including token mint requests,
raises its subclass `manifests.FetchUnanswered`, and the scheduler puts the head back in its
lane with the quiet window restarted, so the first pass after that window fetches again. The
head's waiting entry counts these fetches in a row, and the `UNANSWERED_FETCHES`th (10) drops the
head as `cannot expand <head>: 10 manifest fetches in a row got no answer or a server error, the
last: <error>` and posts its failed `lab-shadow/required` check run naming them, so a head GitHub
keeps failing ends with a lab status. Any other refusal drops the head with its reason. The service
runs each fetch off the scheduler pass through
`manifests.Background`, at most `WORKERS` (4) at once: while a fetch runs the call raises
`manifests.FetchPending`, and the scheduler puts the head back in its lane with its quiet window
unchanged, so the next pass reads the finished fetch and a manifest request GitHub never answers
holds no pass from reaping leases or advancing grades. Until the fetch finishes each pass repeats the
head's expansion up to the manifest, its git reads included; the mirror's `git fetch` of a missing
sha still runs on the pass. A failed fetch answers once; any other answers every call for `FRESH_S`
(300 seconds) after the fetch started. The dev push artifact is kept 7 days, so a commit older
than that is answered only from the store. A head that needs test units and finds none fails with
`no dependency manifest at or before <base>` and is dropped with that reason, so a merge group
never folds green on an empty unit job.

A test batch's manifest, the dependency records of its tests, lives in the object store at
`lab/test/<digest>/manifest.json` beside its verdict, written once when a grade start holds the batch
for a job. Neither the grade state nor the job hash in Redis carries it, so neither grows with a
batch's records, and the lease path reads it from there into the lease reply. Scheduler startup
reads and rewrites no stored grade or job.

Release 2.17.1095 copied each manifest into every grade slot and job hash and restored missing ones
at startup over every live grade, which took the swarm Redis from about 4 GiB to 25 GiB.
`python -m scripts.ci_lab compact`, run once with the scheduler environment before the scheduler
starts on a release without that copy, moves every manifest a live grade's slot or a queued, leased
or finished job hash still carries to its object store path and removes it from Redis. It finds those
records through the live grade set and the queue's state sets, so the lab's Redis user, which may not
SCAN, runs it. An unfinished test slot whose batch has no stored manifest gets the batch's slice of the
packed manifest at its grade's commit. A grade the scheduler writes while the command runs stays as
the scheduler wrote it, manifests included, so run the command again while `grades written meanwhile`
reads above zero. The command prints its counts as
JSON: `grade manifests moved`, `job manifests moved`, `grades stripped`, `jobs stripped`,
`batches sliced`, `batches without a packed manifest` and `grades written meanwhile`.

A batch verdict lists the files the batch opened outside its tests' manifest records. The expander
puts the `commit` of the manifest it keyed a head's test units on in the plan's `manifest`, and the
grade's state keeps it; a pull request or hotfix grade keys no test units and keeps none. When the
scheduler stores the verdict of a graded job whose uploaded report carries a non empty `outside`
list, whatever its graded outcome, `note_outside` writes it as a note of that manifest commit at
`lab/manifests/<owner>--<name>/outside/<commit>/<digest>.json`, holding the unit's tests and the
sorted list, where `<digest>` is `digests.digest` of that note. A grade with no manifest commit writes
no note. The object store is write once, so the packed `<commit>.json` manifests are never rewritten,
and the same tests and list write one note per commit. A `StoredManifests` read that finds a stored
manifest merges each note of that manifest's commit into the `outside` list of each test record the
note names, sorted and without duplicates; the next expansion keys those tests on the measured tree
and the batch on the noted files' blob ids. A test module path a note names that the manifest lists
no record for goes into the manifest's `unlisted` mapping instead, with the same merge, and an unknown
test node id is skipped. A note written
while grades keyed on an older commit's manifest never reaches a newer commit's records or keys. A
manifest the read fetches has no note yet. Every read first removes, through `Objects.remove`, each
note of the repository whose commit has no stored `<commit>.json`, which includes the notes at
`outside/<digest>.json` written before notes carried a commit. `scripts.dep_manifest.invalidated`,
pull request test selection in `scripts.ci_select.recorded_tests` and mutation test selection in
`scripts.ci_mutation.plan.covering_tests` and `line_covering_tests` count a change to a file in a
record's `outside` list as a change to that test, as they do for a file in its `files` list. The
list is per batch, so every test of the batch takes it.

For a merge group head the expander builds `interaction.Group` from the mirror: the group's
changes against the base, one entry per first parent commit between base and head with that
commit's own changes, and the `pyproject.toml` flag from `ci_select.pyproject_reaches_tests`. The
cached test units are the tests of every batch the verdict store answers, a negative verdict
included, and the entry limit is `interaction.entry_limit` of the grader's `delivery.yaml` lab
block. The clauses are in the
[Lab merge group interaction rule](delivery-tiers.md#lab-merge-group-interaction-rule). Beside the
batches the rule picks, or every batch on `FULL`, the expander adds one unit per test module in the
entries' changes that the head holds and the manifest lists no test of, built by
`expand_tests.unlisted_units`: its member is the module's path and its key is the module's broad
fingerprint over the head tree and the files its `unlisted` notes name, as
[Test files the manifest does not know](delivery-tiers.md#test-files-the-manifest-does-not-know)
describes.

Since a push grade folds only the static gates,
[Post merge testing with automatic revert](../research/post-merge-testing.md) measures the red
`dev` rate from the Tests workflow's runs on GitHub, not from lab push grades.

#### Configure the entry limit

`lab.queue.full_suite_entries` in `delivery.yaml` is the most entries a merge group may hold before
the `entries` clause sends it to the whole suite. The `queue` block and the key are optional:
`interaction.entry_limit` returns `FULL_SUITE_ENTRIES` (5), the merge queue's largest group, when
either is left out. Dev's `delivery.yaml` sets neither, so dev merge groups run at a limit of 5.

```yaml
lab:
  authoritative: []
  mutation:
    run_on: every_push
    mutants_per_changed_line: all
    arid: []
  check_prefix: lab
  queue:
    authoritative: []
    full_suite_entries: 3
```

With `full_suite_entries: 3`, a group of four or five entries runs every batch, and a group of one
to three runs only the batches its entries invalidate unless another clause holds. The expander
reads the key from the grader revision on every expansion, and a merge group's grader is its base,
so a change applies to the first merge group whose base holds it, with no scheduler restart.

`queue` accepts only `authoritative` and `full_suite_entries`, and `full_suite_entries` is an
integer of at least 1. Expansion validates the whole document against `delivery.DELIVERY_SCHEMA`
with `jsonschema` directly, so its error carries no `delivery.yaml` path prefix, while
`delivery.lab(root)` reports the same refusal with one:

| `queue` value | First line of the expansion error | `delivery.lab(root)` error |
|---|---|---|
| `{full_suite_entries: 0}` | `0 is less than the minimum of 1` | `delivery.yaml lab.queue.full_suite_entries: 0 is less than the minimum of 1` |
| `{full_suite_entries: 2.5}` | `2.5 is not of type 'integer'` | `delivery.yaml lab.queue.full_suite_entries: 2.5 is not of type 'integer'` |
| `{max: 2}` | `Additional properties are not allowed ('max' was unexpected)` | `delivery.yaml lab.queue: Additional properties are not allowed ('max' was unexpected)` |
| `[]` | `[] is not of type 'object'` | `delivery.yaml lab.queue: [] is not of type 'object'` |

A refused document drops the head as [a head that cannot expand](#a-head-that-cannot-expand): the
action is `cannot expand <head>: ` followed by the whole `jsonschema` message, whose next lines name
the schema rule and `On instance['lab']['queue']['full_suite_entries']`. Validate the file in a
checkout before it reaches dev:

```bash
python -c 'from pathlib import Path; from scripts.ci_lab import delivery; delivery.lab(Path("."))'
```

#### Check which clause a merge group took

The scheduler records the chosen batches, not the clause. The script below rebuilds the group from
a checkout that holds both commits, the way `MirrorExpander` does, and prints the clause
`interaction.full_reason` returns for the `harness`, `entries` and `overlap` clauses, or `None`.
It passes an empty manifest, so the `cache` clause never holds here:

```bash
cat > clause.sh <<'OUTER'
python - "$1" "$2" <<'EOF'
import subprocess
import sys
from pathlib import Path

import yaml

from scripts import ci_select, dep_manifest
from scripts.ci_lab import interaction

base, head = sys.argv[1:3]
root = Path(".")
commits = subprocess.check_output(["git", "rev-list", "--first-parent", f"{base}..{head}"], text=True).split()
entries = tuple(tuple(dep_manifest.changes(root, f"{commit}^", commit)) for commit in commits)
changes = tuple(dep_manifest.changes(root, base, head))
group = interaction.Group(changes, entries, ci_select.pyproject_reaches_tests(root, base, head))
lab = yaml.safe_load(subprocess.check_output(["git", "show", f"{base}:delivery.yaml"], text=True))["lab"]
limit = interaction.entry_limit(lab)
print(f"entries {len(entries)} of limit {limit}")
print("clause", interaction.full_reason(group, {"tests": {}}, (), limit))
EOF
OUTER
bash clause.sh <base> <head>
```

Run against dev's own first parent chain, three squash merges stay under the default limit and
seven do not:

```text
$ bash clause.sh "$(git rev-parse dev~3)" "$(git rev-parse dev)"
entries 3 of limit 5
clause None
$ bash clause.sh "$(git rev-parse dev~7)" "$(git rev-parse dev)"
entries 7 of limit 5
clause entries
```

A group that merged has its head on dev, so its base and head are both on dev's first parent
chain. An ejected group's commits leave with its `gh-readonly-queue` branch, so check it while it
runs.

| Guard | What it holds |
|---|---|
| `test_a_pull_request_or_hotfix_head_expands_no_test_units` | A pull request or hotfix head gets no test units |
| `test_a_merge_group_head_runs_only_the_batches_its_entries_invalidate` | A merge group runs only the batches holding an invalidated test |
| `test_a_merge_group_head_whose_entries_invalidate_no_test_runs_no_batch` | A group whose entries invalidate no test holds an empty `unit` job |
| `test_a_merge_group_head_runs_every_batch_when_an_interaction_clause_holds` | Every batch runs when a clause holds |
| `test_a_promotion_or_backfill_head_runs_every_batch_without_reading_the_cache` | Promotion and backfill heads run every batch and read no verdict |
| `test_a_head_without_stored_durations_keeps_each_test_file_whole` | No `.test_durations` keeps each test file one batch |
| `test_a_head_that_needs_test_units_without_a_manifest_cannot_expand` | A head that needs test units and has no manifest cannot expand |
| `test_a_merge_group_head_refuses_an_entry_limit_the_delivery_schema_refuses` | `full_suite_entries: 0` raises `0 is less than the minimum of 1` |
| `test_a_head_whose_grader_delivery_breaks_its_schema_is_dropped_with_its_reason` | A schema refusal drops the head with its reason and no grade |

The tests are in `tests/ci_lab/test_scheduler.py` and map to the `lab-scheduler-test-units` entry
of `feature-map.json`.

#### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| A merge group, promotion or backfill head gets no grade and the log reads `cannot expand <head>: no dependency manifest at or before <base>` | No commit on the base's first parent chain has a manifest stored, and none of its newest 10 commits has a fetchable `dep-manifest-packed` artifact from a dev push run of the Tests workflow | `gh run list --workflow test.yml --branch dev --event push --limit 10` lists those runs; an artifact older than 7 days has expired |
| `cannot expand <head>: 0 is less than the minimum of 1`, or another `jsonschema` message | The grader revision's `delivery.yaml` breaks its schema | [Configure the entry limit](#configure-the-entry-limit); run `delivery.lab` in a checkout of `<base>` |
| A merge group runs every batch | A clause of `interaction.full_reason` holds, or the entries' changes invalidate every test | Run the [clause check](#check-which-clause-a-merge-group-took). When it prints `None`, either the `cache` clause held, which the script does not evaluate, or an entry changes a global input `dep_manifest.global_input` names, such as `pyproject.toml`, `.github/test-requirements.lock`, `.test_durations` or a `.py` file outside `hooks/`, `scripts/` and `tests/`, or a `tests/` module whose name does not start with `test_`; `git diff --name-only <base> <head>` names the files. The `cache` clause holds when more than half the manifest's tests sit in batches whose key has no stored verdict, as on a new or emptied store |
| A merge group's `unit` job holds no test units and passes | No entry changes a file a manifest test reads, as in a group of documentation changes, and no entry adds a test module the manifest does not list | `git diff --name-only <base> <head>`; the result is expected |
| A merge group runs tests no entry touches | A batch runs whole when it holds one invalidated test, and an entry that changes a global input of the manifest, such as `.github/test-requirements.lock`, invalidates every test | The batch's members in the unit, and each entry's changes |
| The log repeats `holding <head>: <error>` | A manifest fetch is still running or GitHub did not answer it | The manifest paragraph above describes both holds |

### Mutation units

A promotion or backfill head gets two more jobs, built by `scripts.ci_lab.expand_mutation` in a
temporary worktree of the head. `MirrorExpander._mutation` cuts it with `tempfile.mkdtemp` under
`CI_LAB_MIRROR_STORE` and `git worktree add` from the repository's store, and removes it with
`shutil.rmtree` and `git worktree prune` when expansion ends, also when it raises; the rest of
expansion still reads the mirror with `git ls-tree` and `git show`:

| Job | Units |
|---|---|
| `mutation` | One `mutant` unit per function the head changes against its base, naming every mutant of that function, or several when those mutants price over the sandbox timeout |
| `mutation-stats` | One `stats` unit per test file present at the head that holds a test running such a function, with the worker's stats options |

The changed lines are `scripts.ci_mutation.scope.changes_since` of the base and head, the lines the
pull request mutation gate grades. `expand_mutation.covering` maps each function to the fingerprint
of every dependency manifest test that ran it or a function nested in it, and those fingerprints
enter the mutant key, so a changed covering test rekeys the unit. The stats units warm the stats
cache the worker's mutant runner reads before it runs the mutants. The manifest only names the tests
of the base, while the mutant runner runs every test module of the head, so
`expand_tests.unlisted_test_hashes` hashes each test module of the head that holds no manifest test,
with every conftest pytest loads for it and every test tree module either imports, and those hashes
enter every mutant key too: a head that adds or edits such a module rekeys its units. The manifest
records no file such a module reads, so while the head holds one, the hashes also cover every file
under the measured trees and the test trees, the files its test unit is keyed on: an edit to a
fixture it reads rekeys the mutant units as well.

| Head | Mutation jobs |
|---|---|
| Pull request, hotfix, merge group | None: these heads get no mutation units. The merge group `Gate — Required` rules require no mutation job and the test workflow runs none on `merge_group`; a pull request's mutation jobs run on Actions, as [Pull request mutation gate](mutation-gate.md) describes |
| Promotion, backfill | Both, unless the head changes no Python line under `hooks/` or `scripts/` |

A mutant unit passes only when every mutant is killed, so a surviving mutant fails the `mutation`
gate and the promotion grade folds red. A changed function no recorded test ran still gets its unit;
the runner finds no test reaching it, grades its mutants `no tests` and fails the unit.

#### Measured lines

`scripts.ci_mutation.scope.changes_since(root, base, head)` diffs the head against its merge base
with the base (`git diff <base>...<head>`) under `hooks/` and `scripts/`, and keeps:

- `.py` files the head still holds;
- files whose syntax tree still differs once docstrings, and the annotations of undecorated
  functions, are stripped, so a change to only those mutates nothing;
- lines the whole diff does not colour as moved unchanged (`--color-moved=blocks`, indentation
  changes allowed), so code moved between files mutates nothing.

A kept line selects the function or method mutmut trampolines whose span holds it, a nested
function selecting the function that holds it. A line of module level code selects no function.
A head with no kept line gets neither mutation job, and its grade folds on its other gates.

#### What a head needs

| Input | Where | Without it |
|---|---|---|
| `[tool.mutmut]` `source_paths` | The head's `pyproject.toml` | The worker cannot trampoline its copy of the head, so the executor raises a `KeyError` before the runner starts |
| A quoted `mutmut==<version>` requirement | The head's `pyproject.toml`, such as the `dev` extra of `[project.optional-dependencies]` | Expansion raises `<checkout>/pyproject.toml pins no mutmut version` |
| A dependency manifest stored at or before the base | The lab store, fetched as [Test units](#test-units) describes | Expansion raises `no dependency manifest at or before <base>` before any mutation unit is built |
| `CI_LAB_MIRROR_STORE` | The scheduler's [environment](#environment) | The scheduler does not start |
| `CI_LAB_MUTATION_CACHE`, `CI_LAB_KILLERS`, `CI_LAB_MUTATION_BUDGET`, `CI_LAB_MUTATION_CAP` | The worker's [environment](#environment) | The worker does not start |

This repository's `pyproject.toml` holds both:

```toml
[project.optional-dependencies]
dev = [
    # the other development requirements
    "mutmut==3.6.0",
]

[tool.mutmut]
source_paths = ["hooks/", "scripts/"]
```

A `ValueError`, `KeyError`, `SyntaxError`, failed git command, YAML error or schema validation
error raised in expansion makes the scheduler print
`cannot expand <head>: <error>` and drop the head; nothing of it is graded, and the head gets a
failed `lab-shadow/required` check run naming the error (see
[A head that cannot expand](#a-head-that-cannot-expand)).

#### Unit keys

A mutant unit's key is the sha256 of the JSON, object keys sorted, of a list holding:

1. the function's mutmut name, such as `scripts.alpha.x_second`, or `scripts.sample.xǁBoxǁgrow`
   for the method `grow` of the class `Box`;
2. the function's source text, without the blank lines and comments above it;
3. `expand_mutation.module_digest`: the syntax tree of the module's code outside its functions and
   methods, so whitespace and comments do not count;
4. `expand_mutation.runner_digest`: the mutmut pin and the sha256 of every tracked file under
   `scripts/ci_mutation/`;
5. the function's entry of `expand_mutation.covering`: test id to fingerprint;
6. the unlisted test hashes described above.

Its members are `<name>__mutmut_1` to `<name>__mutmut_<n>`, one per mutation mutmut creates in the
function. A function whose mutants price over the 1800 second sandbox timeout gets several units
instead, each holding a part of its mutants under the sha256 of the list of the function's key and
the part's members, as [Mutant units over the sandbox timeout](#mutant-units-over-the-sandbox-timeout)
describes; a function priced within the timeout keeps one unit under the function's key.
A stats unit's key is `StoredStats.identity` of its test file at the head, over the
fields the `stats` rows of [Inputs per kind](#inputs-per-kind) list, with the worker's options
`-q -p no:cacheprovider`. The expander builds both keys itself, without the `digests` builders, so
they are bare 64 hex digests rather than the `<kind>-v<schema>-<digest>` form the
[digest specification](#digest-specification) describes. `scripts.ci_lab.objects.unit_path`
refuses a key of that form with `ValueError: not a <kind> unit key: <key>`, so the
[verdict store](#verdict-store) cannot hold a stats or mutant verdict yet.

#### How a worker runs them

Both kinds run through `scripts.ci_lab.executors.mutation`, with the grader tree's copy of the runner grading the private trampolined copy,
as [Worker executors](#worker-executors) describes:

| Kind | Runner requests | Passes when |
|---|---|---|
| `stats` | One stats request over the unit's test file, with the copy as root and `CI_LAB_MUTATION_CACHE` | The runner exits 0 |
| `mutant` | A stats request over every tracked `test_*.py` under `tests/` of the copy with the same cache, then a mutant request with the unit's mutant names, the tests those stats records show reaching its function with their seconds, `CI_LAB_KILLERS`, `CI_LAB_MUTATION_BUDGET` and `CI_LAB_MUTATION_CAP` | The runner exits 0 and every row reads `killed` |

The stats request of a mutant unit reuses every record in `CI_LAB_MUTATION_CACHE` whose inputs
still match, such as those the grade's stats units stored, and measures only the other test files.
Each mutant row has a status:

| Status | Meaning |
|---|---|
| `killed` | A test failed against the mutant |
| `survived` | Every test the unit runs passed against the mutant |
| `suspicious` | The mutant's child process exited with a code other than 0 or 1 |
| `no tests` | No stats record shows a test reaching the function; every mutant of the unit reads it, at 0 seconds |

A mutant unit's uploaded report holds `collection_seconds`, the `rows` as `name`, `status` and
`seconds`, the unit's `seconds` and its cache lookups; a stats unit's report holds its `stats`
records in place of the rows. A runner that exits non zero, such as on a
collection error in the chosen tests, uploads no rows and fails the unit.

#### Mutant units over the sandbox timeout

Each sandbox run of a mutant unit, its stats run and then its graded run, is bounded by
`executors.UNIT_TIMEOUT_S`, the worker's 1800 second `worker.UNIT_TIMEOUT_SECONDS`. A run past it
has its process group killed and writes no result, so the unit fails: its report holds no rows,
and its log stops where the kill landed, with no line naming the timeout. To keep a changed function
with many mutants under that limit, `MirrorExpander._mutation` passes the timeout to
`expand_mutation.mutant_units`, which prices each changed function's mutants before it builds
their units:

| Step | Rule |
|---|---|
| Price | `n` mutants times `scripts.ci_mutation.plan.MUTANT_SECONDS`, 5.6 seconds a mutant |
| Keep | A price at or under 1800 seconds, up to 321 mutants, keeps one unit of every mutant under the function's key |
| Split | Over it, `expand_mutation.split_members` cuts the mutants, in mutant order, into the fewest parts whose largest prices within 1800 seconds; part sizes differ by at most one, and every mutant lands in exactly one part |
| Key | Each part's key is the sha256 of the JSON of a list holding the function's key and the part's members, so each part gets its own slot, verdict, lease and worker run |

The parts the scheduler cuts at 1800 seconds:

| Mutants of the function | Units | Mutants per unit |
|---|---|---|
| 1 to 321 | 1 | all of them |
| 322 | 2 | 161 and 161 |
| 400 | 2 | 200 and 200 |
| 642 | 2 | 321 and 321 |
| 643 | 3 | 214, 214 and 215 |
| 1000 | 4 | 250 each |

The parts of one function share its `mutation-stats` units: the scheduler builds one stats unit
per test file that runs a function the mutant units name, whatever the number of parts. Each
part's worker run repeats the stats request over every test file, and that request reuses the
`CI_LAB_MUTATION_CACHE` records whose inputs still match. Each part's graded request names the
same covering tests, so every part grades its mutants against the same tests a single unit would.

The price is a fixed 5.6 seconds a mutant. It reads neither the seconds the stats records give the
covering tests nor the lanes `CI_LAB_MUTATION_BUDGET` and `CI_LAB_MUTATION_CAP` open in the graded
run, so a unit of 321 or fewer mutants whose covering tests are slow can still reach the limit.

No environment variable, `delivery.yaml` key or command line flag changes the split. Both inputs
are constants changed by a pull request: `worker.UNIT_TIMEOUT_SECONDS` also sets the worker's 2100
second termination grace, which the chart's `worker.terminationGracePeriodSeconds` matches, and
`plan.MUTANT_SECONDS` also prices the pull request mutation planner's `plan.estimate` and the lab's
`pricing.estimate` of a mutant unit with no measured duration. A caller of `expand_mutation.mutant_units` that passes no
`timeout` gets the default of infinity and never splits.

Print the parts a function of 400 mutants gets, from a checkout with the development environment:

```bash
python -c 'from scripts.ci_lab.executors import UNIT_TIMEOUT_S; from scripts.ci_lab.expand_mutation import split_members; members = tuple(f"scripts.alpha.x_second__mutmut_{i}" for i in range(1, 401)); print([(part[0], part[-1], len(part)) for part in split_members(members, UNIT_TIMEOUT_S)])'
```

```text
[('scripts.alpha.x_second__mutmut_1', 'scripts.alpha.x_second__mutmut_200', 200), ('scripts.alpha.x_second__mutmut_201', 'scripts.alpha.x_second__mutmut_400', 200)]
```

Count the units a changed line of a real file gets, here line 19 of `scripts/ci_lab/pricing.py`, in
`estimate`. The keys this prints differ from the lab's, since it passes no covering tests and no
unlisted test hashes, but the members and the split are the same:

```bash
python -c 'from pathlib import Path; from scripts.ci_lab import expand_mutation; from scripts.ci_lab.executors import UNIT_TIMEOUT_S; units = expand_mutation.mutant_units(Path("."), {"scripts/ci_lab/pricing.py": [19]}, {}, {}, timeout=UNIT_TIMEOUT_S); print([(unit.members[0], len(unit.members)) for unit in units])'
```

```text
[('scripts.ci_lab.pricing.x_estimate__mutmut_1', 20)]
```

One tuple per unit: 20 mutants price at 112 seconds and stay one unit.

#### Example

A promotion head changes line 6 of `scripts/alpha.py`:

```python
def first():
    return 1


def second():
    return 2
```

The base's manifest records `tests/test_beta.py::test_second` running `scripts/alpha.py::second`
and the tests of `tests/test_alpha.py` running `first`. Beside its `unit` job the grade gets:

| Job | Kind | Members |
|---|---|---|
| `mutation-stats` | `stats` | `tests/test_beta.py` |
| `mutation` | `mutant` | `scripts.alpha.x_second__mutmut_1` to `scripts.alpha.x_second__mutmut_<n>` |

`first` gets no unit because none of its lines changed, and `tests/test_alpha.py` no stats unit
because none of its tests runs `second`. Had the head also deleted `tests/test_beta.py`, the grade
would get no `mutation-stats` job and the same mutant members under a new key, since the deleted
file changes the covering test's fingerprint; the unit's stats request then finds any test of the
head that reaches `second`.

Had mutmut created 400 mutants in `second`, they would price at 2240 seconds, over the 1800 second
limit, and the grade would get the same `mutation-stats` unit and two units in place of the one
mutant unit:

| Job | Kind | Members | Key |
|---|---|---|---|
| `mutation` | `mutant` | `scripts.alpha.x_second__mutmut_1` to `scripts.alpha.x_second__mutmut_200` | sha256 of the function's key and these 200 members |
| `mutation` | `mutant` | `scripts.alpha.x_second__mutmut_201` to `scripts.alpha.x_second__mutmut_400` | sha256 of the function's key and these 200 members |

#### Read a grade's mutation gates

```bash
curl -s "http://lab:8800/grades/$SHA" | jq '.grades[]
  | select(.class == "promotion" or .class == "backfill")
  | {run, conclusion, mutation: .checks.mutation, stats: .checks["mutation-stats"]}'
```

A check run is posted only for a gate with units, so a promotion or backfill grade without
`mutation` and `mutation-stats` checks expanded no mutation unit. A `mutation` check that completes
`failure` turns `required` red; the report its worker uploaded names each mutant and its status.
A split function adds one unit per part to the grade's `units.total`. The `mutation` check
completes when every part is final and fails when any part fails.

#### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| A promotion grade has no `mutation` check | The head changes no kept line: only tests, docs, files outside `hooks/` and `scripts/`, docstrings, annotations or moved code | `git diff <base>...<head> -- hooks/ scripts/` |
| A pull request or hotfix grade has no `mutation` check | Those classes never expand mutation units | The Actions mutation jobs grade a pull request into `dev`, as [Pull request mutation gate](mutation-gate.md) describes |
| A merge group grade has no `mutation` check | Merge group grades hold no mutation gate, and the test workflow runs no mutation on `merge_group` | Nothing to fix |
| `cannot expand <head>: <checkout>/pyproject.toml pins no mutmut version` | The head's `pyproject.toml` holds no quoted `mutmut==<version>` | Pin mutmut in the head's `pyproject.toml` |
| `cannot expand <head>: no dependency manifest at or before <base>` | No manifest is stored or fetchable on the base's first parent chain | Read [Test units](#test-units) for how the lab fetches manifests |
| `cannot expand <head>:` naming a failed `git worktree add` | The repository's store lacks the head commit, or `CI_LAB_MIRROR_STORE` is not writable | `git -C <store> cat-file -t <head>` and the folder's permissions |
| `cannot expand <head>: <message> (<unknown>, line <n>)` | A changed Python file under `hooks/` or `scripts/` on a promotion or backfill head does not parse | `python -m py_compile <file>` on a checkout of the head names the line |
| Every row of a failed mutant unit reads `no tests` | No test of the head reaches the changed function | Add a test that calls the function |
| Rows read `survived` | A test runs the function but none fails against the mutant | Assert on the behaviour the mutant changes |
| Rows read `suspicious` | A mutant's child crashed or was killed | Read the unit's log |
| One changed function shows as several `mutant` units of the `mutation` job | Its mutants price over the 1800 second limit: more than 321 of them | Nothing to fix; each part's report names its own mutants, and [Mutant units over the sandbox timeout](#mutant-units-over-the-sandbox-timeout) prints the parts |
| A failed mutant unit's report holds no rows and its log stops mid run | A sandbox run passed the 1800 second limit and was killed, or the runner exited non zero | The report's `seconds` spans both runs: under 1800, no run hit the limit and the runner failed. At 1800 or more, a run may have hit it, and the unit's log is the output of its last run, the one that stopped. The price reads no test time, so slow covering tests let a unit of 321 or fewer mutants reach the limit: speed up those tests, or split the function in the code so it creates fewer mutants |
| A folder with a random name stays under `CI_LAB_MIRROR_STORE` | The scheduler stopped during an expansion, before its cleanup ran | Remove the folder; the `git worktree prune` that ends the next promotion or backfill expansion of that repository drops its registration |

### Grades of a commit

`GET /grades/<sha>` on port 8800 answers every grade of one commit, open or closed, ordered by
run: the live grades of that head and the closed grades its evidence markers
(`lab/heads/<sha>/<run>`) name. The sha is 40 or 64 lowercase hex characters; anything else
answers `400` with `{"error": "a grade lookup names one full commit sha"}`. A commit the lab
never graded answers an empty list.

```bash
curl -s http://lab:8800/grades/0123456789abcdef0123456789abcdef01234567 | jq '.grades[] | {class, conclusion, units}'
```

Each grade is `scripts.ci_lab.scheduler.summary` of its state:

| Field | Meaning |
|---|---|
| `run`, `repo`, `head`, `base`, `tree`, `grader` | What the grade grades and the grader revision it read |
| `class`, `lane` | Its scheduling class and lane |
| `conclusion` | `success` or `failure` once every unit is final, `null` while it runs |
| `superseded` | Whether a newer head of its lane cancelled it |
| `units` | `total` units and how many are `final` |
| `checks` | Each check run's last posted `status` and `conclusion`, `required` included |

## Worker executors

`agentihooks ci-lab-worker` builds the pod from the [worker environment](#environment), leases units
from the scheduler and runs each through `scripts.ci_lab.executors`, the adapter its unit kind
names. Every grant of `POST /lease` carries the job's `head`, `base` and `grader` revisions: the
scheduler enqueues a grade's misses with the grade's base and its plan's grader revision.

| Kind | Trees | Executor inputs |
|---|---|---|
| `static` | head, base and grader, read only, shared by the pod's units | the gate the unit names from the grader tree's workflow, input registry and reusable workflows (see [The worker reads the same workflows](#the-worker-reads-the-same-workflows)), the venv of the head's `.github/test-requirements.lock` digest and the lease fields and secrets its steps read (see [What a gate's steps read](#what-a-gates-steps-read)) |
| `test` | head, read only | the batch and its selected manifest records, read from the object store into its lease, the venv of the head's lockfile digest, `CI_LAB_FLAKES` and one pytest worker per slot |
| `stats` | a private copy of the head with every file under the `[tool.mutmut]` source paths behind its trampolines, and the grader, read only, shared by the pod's units | the tree as root, `CI_LAB_MUTATION_CACHE` and the venv of the copy's `.github/test-requirements.lock` digest |
| `mutant` | the same private trampolined copy and read only grader | the same venv, `CI_LAB_KILLERS`, `CI_LAB_MUTATION_BUDGET`, `CI_LAB_MUTATION_CAP` and the tests that the stats records of every test module in the tree show reaching the mutant's function, with their seconds |

The mutation runner runs in the sandbox as `python -m scripts.ci_lab.run_mutation REQUEST OUTPUT`
from the grader tree, so the grader revision's copy of the runner grades the unit and a head change
to the runner changes no verdict. Each request names the private copy as its `tree`; every pytest
session the runner starts runs there and imports that copy's `scripts` package. A mutant unit passes only when every mutant is killed; a runner that exits
non zero fails its unit. The scheduler splits a function whose mutants price over the 30 minute
unit time limit into several mutant units, as
[Mutant units over the sandbox timeout](#mutant-units-over-the-sandbox-timeout) describes. Each adapter uploads a JSON report with the unit's `seconds` (and `flaky`
for a test unit), which the scheduler stores with the verdict, and releases every tree it cut,
also when the executor raises. SIGTERM stops leasing and lets running units drain within the
termination grace: 2100 seconds, the 30 minute unit time limit (`worker.UNIT_TIMEOUT_SECONDS`,
which every executor's pod timeout reads) plus the five delivery calls of a unit (two upload
URLs, two puts and the completion) at the 60 second HTTP timeout. The worker Deployment of the
deployment repository's `ci-lab` chart sets `worker.terminationGracePeriodSeconds` to the same 2100 seconds, so
a rollout lets the longest unit finish and deliver before the pod is killed.

Check the wiring offline:

```bash
agentihooks ci-lab-worker --self-test | python -c 'import json,sys; print(json.load(sys.stdin)["environment"]["names"])'
```

## Verdict store

`scripts.ci_lab.store.Store` keeps one verdict per unit key, the key the
[digest specification](#digest-specification) builds, such as `test-v1-<64 hex digest>`. Each
verdict lives in two places:

- A report object and a log object in the object store, each written once. They are the record.
- An index entry in Redis, the only thing `get` reads. A lost index is rebuilt from the report
  objects with `reindex`.

The scheduler environment names the Redis and object store the lab uses, `CI_LAB_REDIS_URL` and
`CI_LAB_MINIO_*` (see [Environment](#environment)). The scheduler service builds its `Store` from them; any other
caller constructs the Redis client and the object backend and passes both in, as in
[Use the store](#use-the-store).

### Verdicts

`store.Verdict` is a frozen dataclass:

| Field | Type | Meaning |
|---|---|---|
| `kind` | `str` | Unit kind in lowercase letters, such as `test` or `mutant`; it must equal the key's kind prefix |
| `key` | `str` | The unit key |
| `passed` | `bool` | Whether the unit passed; `False` is a negative verdict, stored and returned like a pass |
| `flaky` | `bool` | The unit failed, then passed on identical inputs |
| `seconds` | `float` | How long the unit took |

A flaky verdict is always a pass. `Verdict(..., passed=False, flaky=True)` raises
`ValueError: a flaky verdict is a pass: <key>`, and so does any stored verdict read back from Redis
or a report that breaks the rule.

### Redis keys

| Key | Type | Holds |
|---|---|---|
| `agentihooks-ci:verdict:<kind>:<key>` | string | The verdict as JSON with sorted keys, no expiry |
| `agentihooks-ci:disabled` | set | The kinds `get` never returns a verdict for |
| `agentihooks-ci:unfiled` | hash | Per kind, the JSON list of the reuse audit's mismatches whose ledger task is not filed yet; see [What a mismatch does](#what-a-mismatch-does) |

The constants are `PREFIX` (`agentihooks-ci:`), `INDEX` (`agentihooks-ci:verdict:`),
`DISABLED` and `UNFILED`. Other lab keys share the prefix, such as the daily CI budget at
`agentihooks-ci:budget:<YYYY-MM-DD>`, so a pattern that targets verdicts is
`agentihooks-ci:verdict:*`, never `agentihooks-ci:*`. An index value reads:

```json
{"flaky": false, "key": "test-v1-<64 hex digest>", "kind": "test", "passed": true, "seconds": 1.5}
```

Read an entry and the disabled kinds from a shell, with `KEY` set to a unit key such as
`test-v1-<64 hex digest>`:

```bash
redis-cli -u "$CI_LAB_REDIS_URL" GET "agentihooks-ci:verdict:test:$KEY"
redis-cli -u "$CI_LAB_REDIS_URL" SMEMBERS agentihooks-ci:disabled
```

### Object layout

`scripts.ci_lab.objects` names every path. Paths are relative to the store root: a directory for
`LocalObjects`, a bucket and key prefix for `S3Objects`.

| Path | Written by | Holds |
|---|---|---|
| `lab/<kind>/<digest>/report.json` | `Store.put` | `{"report": <report>, "verdict": <verdict>}` as JSON with sorted keys |
| `lab/<kind>/<digest>/log.txt` | `Store.put` | The unit's log bytes as given |
| `lab/grades/<run>/grade.json` | `evidence.record`, write once, called by the scheduler when it closes a grade | One grade run's evidence: its inputs and the units it used |
| `lab/heads/<head>/<run>` | `evidence.record`, write once | An empty marker naming each grade run of a head commit |
| `lab/uploads/<job>/report.json` and `log.txt` | A worker, through an [upload URL](#worker-uploads) | A leased job's report and log |

`<digest>` is the 64 hex digest at the end of the key, so `test-v1-<digest>` lands in
`lab/test/<digest>/`. The schema version stays out of the path because the digest already covers
it. `unit_path(kind, key, name)` builds a unit path and raises `ValueError: not a <kind> unit key:
<key>` when the key does not read `<kind>-v<n>-<64 lowercase hex>` or names another kind.
`is_report(path)` matches only `lab/<kind>/<64 hex>/report.json`, so grade, head and upload objects
are never read as verdicts.

Two backends implement the `Objects` protocol of `write_once`, `read`, `keys`, `remove` and
`upload_url`. `remove(path)` deletes one object, unlinking the file or calling `delete_object`, and
takes a missing path as already gone:

| Backend | Construct | `write_once(path, data)` | `upload_url(path, expires_s)` |
|---|---|---|---|
| `LocalObjects` | `LocalObjects(root)` with a `Path` | Writes a staging file beside the path atomically, then hard links it to the path; an existing path fails the link and the call returns `False`. The staging file is removed either way | The `file://` URI of the path; it never expires |
| `S3Objects` | `S3Objects(client, bucket, prefix)` with a botocore or boto3 S3 client | `put_object` with `If-None-Match: *`; a `PreconditionFailed`, `412`, `ConditionalRequestConflict` or `409` answer returns `False`, any other error raises | A presigned `put_object` URL valid for `expires_s` seconds |

`S3Objects` stores each path at `<prefix><path>` in the bucket and strips the prefix again in
`keys`, which follows listing pages up to 10,000 and raises `ArtifactError` beyond that. The bucket
must honour `If-None-Match: *` on PUT; a server that ignores it lets a second put overwrite a
report. `LocalObjects` refuses any path that resolves outside its root.

### Store a verdict

`put(verdict, report, log)` takes the verdict, the report as a JSON-serializable mapping and the
log as bytes. It returns `True` when this call stored the verdict and `False` when the key was
already stored.

1. It writes `report.json` with `write_once`. The report object is the write once guard: the first
   put to create it owns the key.
2. When it created the report, it writes `log.txt` with `write_once` and sets the index entry to
   this verdict with a plain `SET`, which replaces an entry left over from objects that were lost.
   It returns `True`.
3. When the report already existed, it writes no log, reads the stored verdict from the report and
   indexes it with `SET NX`, so an existing entry stays as it is and a missing one comes back. It
   returns `False`.

| Situation | Result |
|---|---|
| A second put of a stored key, with any verdict | `False`; the report, the log and the index keep the first verdict |
| A put after Redis lost the index | `False`; the index entry comes back holding the first verdict |
| A put after the unit's objects were lost | `True`; the new verdict is written and replaces the old index entry |
| A put that finds a report with no log, left by a writer that stopped between the two writes | `False`; no log is added, because a log of another run would not match the report |
| A put while the kind is disabled | Stored and indexed as usual; `get` misses it until `enable` |
| A key of another kind or shape | `ValueError` from `unit_path` before anything is written |

### Look up a verdict

`get(kind, key, *, bypass_negative=False)` reads Redis only and returns the stored `Verdict`, or
`None`:

- when `kind` is in `agentihooks-ci:disabled`;
- when the key has no index entry, including after a Redis flush until `reindex` or a put of that
  key restores it;
- with `bypass_negative=True`, when the stored verdict has `passed` false. A pass is still
  returned, so a caller can rerun red units while it reuses green ones.

By default a negative verdict is a hit like a pass.

### Disable and enable a kind

`disable(kind)` adds the kind to `agentihooks-ci:disabled` and `enable(kind)` removes it. While a
kind is disabled, every `get` of it misses, so every unit of that kind runs again; puts still store
and index its verdicts, which become hits after `enable`. Other kinds keep hitting.

```python
verdicts.disable("test")
verdicts.enable("test")
```

The disabled set lives only in Redis. A Redis flush enables every kind again, and `reindex` does
not restore the set, so disable the kind again after a flush.

The [reuse audit](#reuse-audit) is the caller that disables a kind, when a regrade of one of its
units differs from the cached verdict.

### Rebuild the index

For a running deployment, [Recover the verdict index](#recover-the-verdict-index) explains how
to request this operation at scheduler startup without writing code inside its pod.

`reindex()` lists every object under `lab/`, keeps the paths `is_report` matches, reads each report
and indexes its verdict with `SET NX`. It returns how many entries it added. Entries already present
stay unchanged and are not counted, so a second run returns `0`. Grade, head and upload objects are
skipped. It reads every report once, so its time grows with the number of stored verdicts. Run it
after Redis lost the verdict index, such as a flush or a new instance without persistence. With `client` and `verdicts` built as in
[Use the store](#use-the-store), two verdicts stored, and `key` and `stored` set to the first one's
key and verdict:

```python
>>> client.flushall()
>>> verdicts.get("test", key) is None
True
>>> verdicts.reindex()
2
>>> verdicts.get("test", key) == stored
True
>>> verdicts.reindex()
0
```

### Worker uploads

A worker writes a leased job's report and log through URLs the scheduler mints.
`Uploads(objects, expires_s=900)` is the lease API's `UploadSigner`: `upload_url(job, name)`
returns the URL for `lab/uploads/<job>/report.json` with `name="report"` or `log.txt` with
`name="log"`. On `S3Objects` it is a presigned PUT URL valid for 900 seconds unless `expires_s`
says otherwise; on `LocalObjects` it is a `file://` URI. An upload is not write once: a second PUT
to the same URL replaces the object. A presigned URL grants a write to that one object until it
expires, so keep it out of logs.

The scheduler builds its `S3Objects` with `scheduler.storage`, whose client signs SigV4 URLs over
the host alone, region `us-east-1` and path style, so a worker's PUT may carry any `Content-Type`.
A SigV2 URL, which botocore signs when no signature version is set, also signs the `Content-Type`:
the form type urllib adds to every worker PUT then makes MinIO answer 403 `SignatureDoesNotMatch`.

Workers ask the scheduler's lease API on port `8800`:

```http
GET /upload-url?job=<job>&token=<lease token>&name=report
```

| Status | Body | When |
|---|---|---|
| 200 | `{"url": "<upload URL>"}` | The token holds the job's lease and `name` is `report` or `log` |
| 403 | `{"error": "lease token refused"}` | `job` or `token` is missing, or the token does not hold the lease; checked before `name` |
| 400 | `{"error": "upload name must be report or log"}` | Any other `name` |

On `LocalObjects` the worker writes the file at the `file://` path. On `S3Objects` the URL signs only
the host header, so a plain PUT of the bytes uploads the file:

```bash
url="$(curl -fsS "$CI_LAB_SCHEDULER_URL/upload-url?job=$JOB&token=$TOKEN&name=log" | jq -r .url)"
curl -fsS -X PUT --upload-file log.txt "$url"
```

### Use the store

With a local directory:

```python
import os
from pathlib import Path

import redis

from scripts.ci_lab import digests, objects, store

client = redis.Redis.from_url(os.environ["CI_LAB_REDIS_URL"], decode_responses=True)
verdicts = store.Store(client, objects.LocalObjects(Path("/srv/ci-lab")))

key = digests.test_key(inputs)
hit = verdicts.get("test", key)
if hit is None:
    verdict = store.Verdict(kind="test", key=key, passed=True, flaky=False, seconds=12.4)
    verdicts.put(verdict, {"tests": 40, "failed": []}, log)
```

With an S3 compatible bucket such as MinIO, reusing `client` from the local example, where
botocore reads its credentials from its usual environment variables:

```python
import botocore.session

s3 = botocore.session.get_session().create_client("s3", endpoint_url="https://objects.example.test")
blobs = objects.S3Objects(s3, "ci-lab", "ci/")
verdicts = store.Store(client, blobs)
signer = objects.Uploads(blobs)
```

`inputs` is the unit's `digests.TestInputs` and `log` the batch log as bytes. The tests in
`tests/ci_lab/test_store.py` run the store behaviours on fakeredis against both backends; the case
of lost objects runs on `LocalObjects` only.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `get` returns `None` for a unit that ran | The kind is disabled, the index entry is gone, or the caller bypassed a negative verdict | `SISMEMBER agentihooks-ci:disabled <kind>` and `EXISTS agentihooks-ci:verdict:<kind>:<key>`; after a flush run `reindex()` |
| `put` returns `False` and `get` returns another verdict than the one put | The key was stored first by another put; the first verdict stands | Read `lab/<kind>/<digest>/report.json` |
| `ValueError: not a <kind> unit key: <key>` | `Verdict.kind` differs from the key's kind, or the key is not `<kind>-v<n>-<64 hex>` | Build the key with the `digests` builder of that kind |
| `ValueError: a flaky verdict is a pass: <key>` | A verdict has `flaky` true and `passed` false | Set `flaky` only on a pass that followed a failure |
| `reindex()` returns `0` right after a flush | No report objects under `lab/` at this root, from a wrong directory, bucket or prefix; or every entry was already indexed | `verdicts.objects.keys("lab/")` lists the stored objects |
| A unit folder holds `report.json` without `log.txt` | The writer stopped between the two writes; later puts never add a log | To run the unit again, delete its folder and its index entry; the next put stores a fresh verdict |
| A worker gets `403` from `/upload-url` | The lease expired or the token belongs to another lease | Heartbeat before the lease expires; once it has expired, lease the job again |

## Recovery

The node mirror and uv cache can be rebuilt from the repository. The Redis verdict index can
be rebuilt from report objects; a lost object bucket needs its backup restored first. The
deployment repository owns the CI lab chart, node cache script and
[bucket backup runbook](#deployment-recovery-runbook).
Change chart values through that repository's delivery process. Pod recreation and scheduler
restarts below retain the deployed code.

The scheduler supports startup reindexing. The node recovery steps use the deployment's
node-cache DaemonSet. The mirror age gauge is one unlabelled series read from the `fetched-at`
stamp under the scheduler's own `CI_LAB_GIT_MIRROR`, as listed in [Metrics](#metrics), so it
shows no other node's mirror; a successful fetch log alone does not prove the fresh-node metric
requirement.

### Deployment recovery runbook

In the deployment repository, open `docs/2K-CI-LAB.md`, section `Recovery`, for the cluster's
cache paths, bucket addresses and backup target. Chart overrides belong in
`k8s/charts/ci-lab/values.yaml`; the refresh script is
`k8s/charts/ci-lab/files/warm-node-cache.sh`. Keep deployment addresses in that repository.
[Where they live and how they deploy](#where-they-live-and-how-they-deploy) also lists the
deployment's lab monitoring resources.

### Configure node recovery

These chart overrides use an example host cache root. Keep the root consistent with worker
mounts, and retain the chart's node selection and tolerations so a cache pod reaches each CI node:

```yaml
nodeCache:
  path: /var/cache/example-ci-lab
  uid: 1001
mirror:
  intervalSeconds: 60
scheduler:
  repository: https://github.com/the-cloud-clockwork/agentihooks.git
  reindexOnStart: false
```

The chart's refresh interval defaults to 60 seconds. The `ci-lab-node-cache` DaemonSet mounts
`nodeCache.path` as `/cache`; its `prepare` init container creates `git` and `uv` for
`nodeCache.uid`. A cold autoscaled node needs no manual cache creation. Its first refresh:

1. Creates the bare mirror at `git/agentihooks.git` under the cache root.
2. Fetches all branches and pull request heads from `scheduler.repository`.
3. Atomically updates the mirror's `fetched-at` stamp with the last successful fetch time in
   epoch seconds.
4. Warms `uv` from the dev branch's `.github/test-requirements.lock`, applying
   `.github/test-excludes.txt` when present.

The uv marker `uv/warmed-lock` records the lockfile blob. An unchanged blob skips another
install; a repository without that dev lockfile logs a skip. This cache contains packages for
venv creation, not the verdict index.

Inspect the cache pods and their refresh output:

```bash
kubectl -n ci-lab get daemonset ci-lab-node-cache
kubectl -n ci-lab get pods -l app.kubernetes.io/component=node-cache -o wide
kubectl -n ci-lab logs -l app.kubernetes.io/component=node-cache -c warm --tail 20
```

A successful refresh logs `mirror fetched`, followed by `uv cache warmed for lock <blob>` or
`uv cache current for lock <blob>`. The stamp advances before uv warming, so a failed package
install can follow a successful git fetch. When the scheduler's `CI_LAB_GIT_MIRROR` is a node
cache mirror, verify that its `ci_lab_mirror_age_seconds` sample is below `mirror.intervalSeconds`
after one completed loop. For every other node, read its `fetched-at` stamp with the commands
above and retain the fetch output as cache evidence.

### Recover an interrupted refresh or deleted cache

Each refresh first acquires `git/.warm.lock` with `flock`. With that lock held, it removes
stale `*.lock` files inside the mirror before fetching. A killed fetch therefore recovers on
the next loop. A concurrent refresh logs
`node cache refresh skipped: another run holds the cache lock` and tries again on the next loop.

Deleting the host cache root leaves running pods mounted on the deleted directory. Deleting
only its `git` directory also needs pod recreation: the root belongs to root, so the refresh
user cannot recreate that directory. The refresh logs `node cache refresh failed` and the
stamp stops advancing. Recreate the node-cache pod on the affected node so the kubelet and
`prepare` init container recreate the directories:

```bash
NODE=example-ci-node
kubectl -n ci-lab delete pod -l app.kubernetes.io/component=node-cache \
  --field-selector "spec.nodeName=$NODE"
```

Restart affected worker pods as well when they retain the deleted mount. A replacement cache
pod rebuilds the mirror and uv cache like a cold node; cache loss does not restore or remove
the report objects in the bucket.

### Recover the verdict index

Restore access to Redis and the existing report bucket before starting the scheduler. Preserve
the [scheduler startup prerequisites](#run-the-scheduler-service), including bus consumers,
repository access and `CI_LAB_URL`; reindexing runs after dependency construction and before
either HTTP listener starts.

For a host service with that environment already configured, request one recovery startup:

```bash
CI_LAB_REINDEX_ON_START=true agentihooks ci-lab-scheduler
```

Only lowercase `true` requests recovery. Unset, `false`, `True` and `1` all skip reindexing.
For the chart deployment, deliver this override through git instead:

```yaml
scheduler:
  reindexOnStart: true
```

The chart injects `CI_LAB_REINDEX_ON_START=true` and rolls the scheduler. Read its startup output:

```bash
kubectl -n ci-lab logs deployment/ci-lab-scheduler \
  | grep --line-buffered -E 'reindexed [0-9]+ verdicts'
```

The scheduler prints and flushes `reindexed N verdicts`, where N counts entries added, then
starts serving. The line `reindexed 1 verdicts` is an example for one missing entry, not a
deployed recovery result. Existing entries remain unchanged; another run can print
`reindexed 0 verdicts`. The [store rebuild rules](#rebuild-the-index) explain the report scan.

If another recovery pass is needed while the chart setting remains on, restart the scheduler:

```bash
kubectl -n ci-lab rollout restart deployment/ci-lab-scheduler
kubectl -n ci-lab rollout status deployment/ci-lab-scheduler
```

After verification, deliver `scheduler.reindexOnStart: false` through git, or remove the
environment override from the host service. Leaving it on reads every report on every start
and delays readiness as the bucket grows. Reindexing restores verdict entries only: queued
and leased units remain lost, and Redis-only disabled kinds become enabled after a Redis
flush. Restore those disable decisions before resuming reuse; see [Disable and enable a kind](#disable-and-enable-a-kind).

### Restore a lost bucket

Use the [deployment recovery runbook](#deployment-recovery-runbook) to identify the backup
endpoint and retained copy.
The supplied deployment's nightly MinIO backup mirrors buckets without deleting objects from
the backup. A restore cannot recover reports written after the last successful backup; their
units miss the verdict cache and grade again.

From a workstation with `mc`, inject the endpoints and credentials through the approved secret
mechanism. Set `CI_LAB_RESTORE_ENDPOINT` and `CI_LAB_BACKUP_ENDPOINT` to the destination and
backup addresses, and `CI_LAB_MINIO_BUCKET` to the bucket, for example `ci-lab-artifacts`.
`MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` below are environment references, never literal values:

```bash
mc alias set lab-restore "$CI_LAB_RESTORE_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc alias set lab-backup "$CI_LAB_BACKUP_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "lab-restore/$CI_LAB_MINIO_BUCKET"
mc mirror --overwrite --preserve "lab-backup/$CI_LAB_MINIO_BUCKET" "lab-restore/$CI_LAB_MINIO_BUCKET"
```

Restore the bucket before recovering its Redis index. Retain both the fresh-node metric sample
and the deployed scheduler's `reindexed N verdicts` line to prove full recovery.

### Recovery troubleshooting

| Symptom | Check and action |
|---|---|
| No cache pod on the new node | Check DaemonSet readiness, node selection, tolerations and the `prepare` init container before investigating the refresh script |
| `mirror fetched` followed by `node cache refresh failed` | The stamp was already updated; inspect repository lockfile access, package downloads and uv cache permissions |
| Refresh repeatedly skips | Another run holds `git/.warm.lock`; confirm that run's state before treating a stale mirror lock as the cause |
| Refresh fails after cache directory deletion | Recreate the affected node-cache pod and workers holding deleted mounts |
| Mirror age sample is absent | The scheduler found no `fetched-at` stamp holding a whole number under its `CI_LAB_GIT_MIRROR` at the scrape; check that path is a node cache mirror and read its stamp |
| No reindex line | Check the exact environment value and startup dependency errors; the chart's default `false` omits the variable |
| Reindex prints zero | Existing entries are preserved, or no matching reports exist in the configured bucket; check the endpoint, bucket and report objects |
| Scheduler readiness is delayed during recovery | Reindex reads every report before serving; inspect startup logs and object store access, then disable the startup setting after recovery |
| Verdicts return but queued work or disabled kinds are missing | Reindex restores only verdict entries; reconstruct pending work and restore disable decisions separately |

## Reuse audit

`scripts.ci_lab.audit` checks the [verdict store](#verdict-store)'s reuse. The `grade` method of
`ports.lab` grades units again with no store reuse, and the audit compares each fresh verdict with
the cached one. When one differs, the audit stops reuse of that kind, files a ci task on the ledger
and raises `CILabVerdictMismatch`. The task asks for the fix of an input the kind's key misses; the
audit itself only detects the difference, not its cause.

| Call | Units it grades again | Class |
|---|---|---|
| `promotion_audit(ports, head)` | Every unit of the head's cached promotion grade, from `ports.lab.suite(head)` | `Priority.PROMOTION` |
| `sample(ports, day, share=audit.SHARE)` | A deterministic share of the units the store answered on `day`, from `ports.lab.hits(day)`; `SHARE` is `0.05`, 5 percent | `Priority.BACKFILL` |

Both return the audit's `Regrade` and write nothing when every compared verdict agrees.

The audit is a library. No scheduler pass, command, workflow or tick step calls `promotion_audit`
or `sample` yet, and no `Lab` implementation exists outside `tests/ci_lab/test_audit.py`, so a
caller supplies its own ports.

### Ports

`audit.Ports(store, lab, tasks, slug)` is a frozen dataclass:

| Field | Type | What the audit does with it |
|---|---|---|
| `store` | `store.Store` | Reads each cached verdict with `get`, stops reuse with `disable` and keeps each kind's mismatches with `keep_unfiled` until `filed` |
| `lab` | `audit.Lab` | Lists the units to compare and grades them |
| `tasks` | `audit.Tasks` | Files one task per differing kind with `add_task(slug, fields, by)`; `scripts.swarm.ledger_client.LedgerClient` has that method |
| `slug` | `str` | The ledger the tasks go to |

`audit.Lab` is a protocol of three methods:

| Method | Returns |
|---|---|
| `suite(head)` | Every unit of the head's cached promotion grade, as `audit.Cached` |
| `hits(day)` | Every unit the store answered on that UTC day, once per unit and head, as `audit.Cached` |
| `grade(units, tier)` | An `audit.Regrade`: the run of a grade of those units in the tier's class with no store reuse, and the `store.Verdict` of each unit it graded |

`audit.Cached(kind, key, head, run)` names one unit a grade answered: its kind and key, the head it
graded and the run whose verdict that grade used. That run is the cached run a mismatch names.
`audit.Regrade(run, verdicts)` holds the audit's own run and a tuple of verdicts.

### Sample of a day's hits

`chosen(day, hits, share)` picks the units `sample` grades. `sample` takes one day per call;
nothing calls it on a schedule yet.

1. The seed is `digest({"day": day})`, the sha256 hex of the record's canonical JSON, as in the
   [digest specification](#digest-specification).
2. Each hit ranks by `digest({"seed": <seed>, "unit": "<kind>:<key>", "head": <head>})`, lowest
   hex string first.
3. It returns the first `ceil(share * len(hits))` hits of that ranking.

The same day and hits give the same units on every machine and every run, in whatever order the
hits arrive. Another day gives another selection. The same unit hit on two heads is two hits that
rank apart, so one can be picked without the other. `day` is hashed as given and never parsed: the
tests pass a UTC day as `YYYY-MM-DD`, and two callers that format the day differently pick
different units.

| Hits that day | Units graded at `share=0.05` |
|---|---|
| 0 | 0 |
| 1 to 20 | 1 |
| 21 to 40 | 2 |
| 41 | 3 |
| 100 | 5 |

```python
>>> from scripts.ci_lab import audit
>>> hits = [
...     audit.Cached("test", f"test-v1-{n:064x}", "a" * 40, f"grade-v1-{n:03d}")
...     for n in range(40)
... ]
>>> len(audit.chosen("2026-11-18", hits, audit.SHARE))
2
>>> audit.chosen("2026-11-18", hits, 0.05) == audit.chosen("2026-11-18", hits[::-1], 0.05)
True
>>> audit.chosen("2026-11-18", hits, 0.05) == audit.chosen("2026-11-19", hits, 0.05)
False
```

### Mismatches

`mismatches(store, units, regrade)` reads each unit's cached verdict with `store.get` and its
fresh verdict from the regrade, in the order of `units`:

| Cached verdict | Fresh verdict | Result |
|---|---|---|
| Pass | Fail | Mismatch |
| Fail | Pass | Mismatch |
| Flaky pass | Fail | Agrees: a flaky pass already failed once on the same inputs |
| Pass or fail | The same | Agrees |
| None: no index entry, or the kind is disabled | Any | Not compared |
| Any | None: the regrade holds no verdict for the unit | Not compared |

`get` runs with its default, so a cached negative verdict is compared like a pass. A disabled
kind's `get` misses, so an audit compares nothing of a kind that is already disabled. It still
grades that kind's units: the audit passes every unit it listed to `grade` and skips only the
comparison, so each later audit spends a grade on a disabled kind for nothing.

### What a mismatch does

The audit first reads the mismatches an earlier audit could not file with `audit.unfiled(store)`
and puts them ahead of the ones it finds itself. When any mismatch remains, the audit:

1. Disables every differing kind with `Store.disable` and records its mismatches with
   `Store.keep_unfiled`, all of them before it files any task. The whole kind stops hitting, its
   agreeing units included; other kinds keep hitting.
2. Files one task per kind, in sorted kind order, with `tasks.add_task(slug, fields, "swarm")`,
   and drops the kind's record with `Store.filed` once the ledger takes the task. `"swarm"` is
   `audit.SENDER`.
3. Raises `CILabVerdictMismatch`.

Each task carries these fields:

| Field | Value |
|---|---|
| `task` | `lab-mismatch-<kind>-<16 hex>`, where the hex is the first 16 characters of `digest({"kind": <kind>, "run": <audit run>})`, so one id per kind and audit run; the prefix is `audit.TASK_PREFIX` |
| `title` | `Lab verdict mismatch: <kind> reuse disabled` |
| `lane` | `ci` |
| `kind` | `ci` |
| `rank` | `urgent` |
| `description` | Every mismatch of the kind with its cached run and the audit run, then the rule that reuse stays off until a pull request enables the kind again |
| `not_duplicate` | `audit.NOT_DUPLICATE`: `The lab audit files one task per kind and audit run whose regrade differs from the cached verdicts.` |

A description reads, with two units of the kind differing:

```text
The lab audit regraded test units differently from their cached verdicts: <key one> cached passed by run grade-v1-first, regraded failed by run grade-v1-audit; <key two> cached passed by run grade-v1-second, regraded failed by run grade-v1-audit. Reuse of test verdicts is off until a pull request enables the kind again with the fix for the input its key misses.
```

`CILabVerdictMismatch` is a `RuntimeError`. Its `mismatches` attribute is a tuple of
`audit.Mismatch(cached, regraded, cached_run, audit_run)` across every kind, in the order of the
units, and its message names the differing kinds sorted and comma separated:

```text
regraded verdicts differ from the cached ones for static, test
```

When the ledger refuses a task, the exception `add_task` raised propagates and
`CILabVerdictMismatch` is not raised. Every differing kind is already disabled, so none is reused.
The refused kind and every later kind in sorted order have no task, and their mismatches stay in
`agentihooks-ci:unfiled` with each cached verdict, regraded verdict, cached run and audit run. The
next `promotion_audit` or `sample` files those tasks again with the same units, under its own audit
run's task id, although it no longer compares the disabled kind, and raises `CILabVerdictMismatch`
naming them. `audit.unfiled(store)` returns the recorded mismatches as `audit.Mismatch` values,
ordered by kind.

### Enable reuse again

The audit never enables a kind. Reuse of a kind comes back through a pull request that fixes the
input its key misses, as the task says. No code enables a kind when that pull request merges, and
no service, command or workflow calls `Store.enable` yet. The call is:

```python
verdicts.enable("test")
```

It removes the kind from `agentihooks-ci:disabled`. A Redis flush empties that set too, which
enables every kind again; see [Disable and enable a kind](#disable-and-enable-a-kind).

`enable` leaves the kind's entry in `agentihooks-ci:unfiled`. A kind enabled before its task is
filed is disabled again by the next audit, which files the recorded units; a recorded unit that
differs again in that audit is listed twice in the task.

### Use it

With `client` and `verdicts` built as in [Use the store](#use-the-store), `lab` an object with the
`Lab` methods, `slug` the ledger and `head` a dev head:

```python
import datetime

from scripts.ci_lab import audit
from scripts.swarm.ledger_client import LedgerClient

ports = audit.Ports(store=verdicts, lab=lab, tasks=LedgerClient(), slug=slug)

try:
    audit.promotion_audit(ports, head)
    audit.sample(ports, datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d"))
except audit.CILabVerdictMismatch as found:
    for mismatch in found.mismatches:
        print(mismatch.cached.kind, mismatch.cached.key, mismatch.cached_run, mismatch.audit_run)
    raise
```

A mismatch in the promotion audit stops the sample in this example; call each in its own `try` to
run both.

### Verify

The tests in `tests/ci_lab/test_audit.py`, under the feature map entry `lab-reuse-audit`, run in CI
on a fakeredis verdict store, a fake lab and a recording ledger. They cover a seeded mismatch that
disables its kind, files the task with both runs and raises; matching verdicts that file nothing;
the flaky pass rule; units with no verdict on either side; one task per differing kind; a ledger
that refuses the task; the sample's share and class; and the sample's ranking from the day digest.

After an audit raised, read the disabled kinds and the task it filed, with `KIND` set to a
differing kind, `RUN` to the audit run and `SLUG` to the ledger in `Ports.slug`:

```bash
redis-cli -u "$CI_LAB_REDIS_URL" SMEMBERS agentihooks-ci:disabled
task="$(python - "$KIND" "$RUN" <<'PY'
import sys

from scripts.ci_lab.audit import TASK_PREFIX
from scripts.ci_lab.digests import digest

kind, run = sys.argv[1:]
print(f"{TASK_PREFIX}{kind}-{digest({'kind': kind, 'run': run})[:16]}")
PY
)"
agentihooks ledger --slug "$SLUG" get "tasks/$task"
```

Run the `python` step from the root of a checkout of this repository. With `KIND=static` and
`RUN=grade-v1-audit` it prints `lab-mismatch-static-73a2319d23e54d76`.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `CILabVerdictMismatch: regraded verdicts differ from the cached ones for <kinds>` | A regrade differed from a cached verdict of each named kind | `found.mismatches` lists every unit with its cached run and the audit run; the filed task names the same |
| Every unit of a kind runs again after an audit | The audit disabled the kind | `SISMEMBER agentihooks-ci:disabled <kind>`; reuse comes back as in [Enable reuse again](#enable-reuse-again) |
| A later audit finds nothing for a kind that differed before | The kind is disabled, so its `get` misses and nothing of it is compared | `SISMEMBER agentihooks-ci:disabled <kind>` |
| A unit that ran is never compared | It has no index entry, or the regrade holds no verdict for it | `EXISTS agentihooks-ci:verdict:<kind>:<key>`, and the verdicts of the `Regrade` the lab returned |
| A cached pass regraded red is not reported | The cached verdict is flaky; a flaky pass agrees with a failure | The `flaky` field of the index entry |
| The ledger's error propagates instead of `CILabVerdictMismatch` | `add_task` raised for one kind | The ledger error names the ledger and its refusal detail, not the kind; `SMEMBERS agentihooks-ci:disabled` lists the disabled kinds, this audit's among them. Each differing kind is already disabled, so none is reused. The refused kind and every later kind in sorted order have no task, and a later audit files none because a disabled kind is not compared. The mismatch list is lost with the exception. Each unit's cached run comes from `ports.lab.suite(head)`, or from `chosen(day, ports.lab.hits(day), share)` for a sample. The audit run and the fresh verdicts exist only in the `Regrade` that `grade` returned, which the audit does not return on this path, so they are recoverable only from the lab's own record of that grade. File those ci tasks by hand, naming the kind and both runs |
| An audit grades units of a kind it then does not compare | The kind is disabled; the audit grades every listed unit and skips only the comparison | `SMEMBERS agentihooks-ci:disabled`; leave a disabled kind's units out of what the lab lists |
| Two callers sample different units on the same day | They pass the day in different formats, or their lab returns different hits | Pass the UTC day as `YYYY-MM-DD` and compare the `hits(day)` lists |

## Retry policy

`scripts.ci_lab.retry` decides whether a unit runs again after a result. Under the default counts a
unit whose worker was lost gets one more run, and a test failure, a static gate failure or a
survived mutant is final at once. The one rerun of a failed test is the
[flake rule](#the-flake-rule) in the run tests engine. `decide` returns `retry` or `final`, and
`red` says whether a final result makes its unit's grade red. `retry.policy` reads the counts from `lab.retry` in
`delivery.yaml`.

The scheduler calls it on every finished job; [What reaches the policy](#what-reaches-the-policy)
covers the lease and crash paths.

### Outcomes

`retry.UnitResult` is a frozen dataclass:

| Field | Type | Meaning |
|---|---|---|
| `outcome` | `str` | How the run ended, one of the outcomes below |
| `attempts` | `int` | How many times the unit already ran again: `0` on its first run, `1` on its first rerun |

`decide` compares the outcome text exactly, case included, and sorts it into one of three kinds:

| Outcome | What happened | Kind | Count it spends |
|---|---|---|---|
| `passed` | The unit passed | pass | none; always final and green |
| `lost` | The unit's lease expired | loss | `lost` |
| `crash` | The worker running the unit crashed | loss | `lost` |
| `failed` | A test failed | failure | `failed` |
| `gate_failed` | A static gate failed | failure | `failed` |
| `survived` | A mutant survived | failure | `failed` |
| Any other text, such as `Passed` or `timeout` | Unknown to the policy | failure | `failed` |

An outcome the policy does not know is a failure, so a misspelled outcome never earns a retry
under the default counts.

### Decide

`decide(result, rules=retry.DEFAULT)`:

1. `passed` is `final`.
2. A loss may run again `rules.lost` times; any other outcome `rules.failed` times.
3. It returns `retry` while `result.attempts` is below that number, else `final`.

Under the default counts, `lost: 1` and `failed: 0`:

| Outcome | `attempts` 0 | `attempts` 1 | `attempts` 2 |
|---|---|---|---|
| `passed` | `final` | `final` | `final` |
| `lost`, `crash` | `retry` | `final` | `final` |
| `failed`, `gate_failed`, `survived`, any other | `final` | `final` | `final` |

`red(result)` is `True` for every outcome except `passed`. It does not check that the result is
final, so call it only after `decide` returned `final`. A unit lost on its first run and lost again
on its rerun is `final` and red:

```python
>>> from scripts.ci_lab import retry
>>> first = retry.UnitResult(outcome="lost", attempts=0)
>>> second = retry.UnitResult(outcome="lost", attempts=1)
>>> retry.decide(first)
'retry'
>>> retry.decide(second)
'final'
>>> retry.red(second)
True
```

With `retry.Policy(lost=2, failed=1)` a lost unit runs again twice and a failed one once:

| Result | `decide` |
|---|---|
| `UnitResult("lost", 1)` | `retry` |
| `UnitResult("lost", 2)` | `final` |
| `UnitResult("failed", 0)` | `retry` |
| `UnitResult("survived", 0)` | `retry` |
| `UnitResult("failed", 1)` | `final` |

### Configure the counts

`lab.retry` in the repository's `delivery.yaml` sets the two counts. The block is optional, and so
is each key; a missing count keeps its default.

```yaml
lab:
  authoritative: []
  mutation:
    run_on: every_push
    mutants_per_changed_line: all
    arid: []
  check_prefix: lab
  retry:
    lost: 1
    failed: 0
```

| Key | Default | Allowed | Effect |
|---|---|---|---|
| `lost` | `1` | An integer, `0` or more | Reruns `decide` allows after an expired lease or a crashed worker; `0` makes the first loss final and red |
| `failed` | `0` | An integer, `0` or more | Reruns `decide` allows after a test failure, a static gate failure, a survived mutant or an unknown outcome; above `0` it reruns graded failures |

The scheduler reads the counts from the repository's dev `delivery.yaml` when it starts; see
[What reaches the policy](#what-reaches-the-policy).

`scripts.ci_lab.delivery.lab(root)` validates the block with the rest of `lab` and raises
`delivery.DeliveryError` naming the path:

| `retry` value | Error |
|---|---|
| `{lost: -1}` | `delivery.yaml lab.retry.lost: -1 is less than the minimum of 0` |
| `{lost: 1.5}` | `delivery.yaml lab.retry.lost: 1.5 is not of type 'integer'` |
| `{lost: one}` | `delivery.yaml lab.retry.lost: 'one' is not of type 'integer'` |
| `{lost: true}` | `delivery.yaml lab.retry.lost: True is not of type 'integer'` |
| `{extra: 1}` | `delivery.yaml lab.retry: Additional properties are not allowed ('extra' was unexpected)` |
| `[]` | `delivery.yaml lab.retry: [] is not of type 'object'` |

`policy(lab)` turns a validated block into a `retry.Policy`:

```python
from pathlib import Path

from scripts.ci_lab import delivery, retry

rules = retry.policy(delivery.lab(Path(".")))
result = retry.UnitResult(outcome="crash", attempts=0)

if retry.decide(result, rules) == "retry":
    print("run the unit again with attempts", result.attempts + 1)
elif retry.red(result):
    print("grade the unit red")
```

Pass `policy` only a block `delivery.lab` returned. It does not validate: an unknown key raises
`TypeError: Policy.__init__() got an unexpected keyword argument`, and a negative count passes
through.

### The flake rule

The one rerun of a failed test lives in the run tests engine, not in this policy.
`scripts.ci_lab.run_tests.run` runs a failed batch a second time on identical inputs inside the same
unit run:

- A pass on the second attempt is `passed` with `flaky` set, and writes one flake ledger row per
  test the first attempt's `flakes.jsonl` names. When that file names none, it writes one row per
  test in the batch with the error `pytest exited with code <code>`.
- A second failure is `failed` and final, so the store keeps the negative verdict.

`lab.retry.failed` does not control that rerun. `run_tests.Verdict.attempts` counts attempts inside
the run, `1` or `2`, while `UnitResult.attempts` counts reruns of the unit; with the default
`failed: 0` every failure is final whichever number is passed.

### What reaches the policy

Each scheduler pass applies the counts on two paths:

| Event | What the scheduler does |
|---|---|
| A lease expires | `queue.Queue.reap` moves the job back to pending with one more `attempts` while `attempts` is below `lab.retry.lost`, else sets its state and outcome to `lost` |
| A worker completes a lease | `decide` reads the job's outcome and `attempts`; on `retry`, `queue.Queue.retry` moves the job back to pending with one more `attempts` and the unit stays open, else the outcome is final |
| A unit's executor raises | The unit stops heartbeating and its lease is never completed, so it reaches the scheduler as an expired lease. The worker prints `ci-lab worker: unit <job> crashed: <repr of the error>` to standard error on its next lease cycle, followed by the error's traceback, whose last line names the path a missing file error could not open |

A worker that completes a lease with `crash` therefore runs once more under the default
`lost: 1`, and a second `crash` is final and red. `tests/ci_lab/test_retry.py` covers the decision
table, and the `lab-scheduler-test-units` feature tests cover the scheduler's retry path.

`queue.Queue.retry` moves only a job whose state is `done`: it adds one to `attempts`, clears the
job's worker and outcome and adds it back to the pending set under the same job id with a new
sequence number; a job in any other state is left as it is and the call returns `False`. A job
reaping marked `lost` never reaches it, since reaping marks a job `lost` only once its `attempts`
reached `lab.retry.lost`, where `decide` is `final`.

An attempt that is run again leaves no trace in the verdict store: the scheduler stores a verdict,
report and log only for the final attempt, and only when that attempt is graded and uploaded a
report. Each finished attempt with an uploaded report is still counted once in the unit metrics of
its class and charged to the day's budget, because `queue.Queue.tally` marks the attempt by its
`attempts` number. With `retry: {failed: 1}`, a unit that fails and then passes stores one passing
verdict, and both attempts count when both uploaded a report.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| A failed test, gate or mutant is never rerun | `failed` defaults to `0`: a graded failure is final | Read `lab.retry.failed` in `delivery.yaml`; the flake rule reruns a failed test batch once on its own |
| A lost unit goes red after one rerun | `lost` defaults to `1`, so the second loss is final | Read `lab.retry.lost`; search the worker's standard error for `crashed` |
| An expired lease stays leased and is never rerun | No scheduler pass ran since it expired | Check the scheduler service is running; each pass reaps expired leases |
| An outcome that should retry is final | The outcome text is not exactly `lost` or `crash`, so it counts as a failure | Compare the outcome the worker sent with the strings `lost` and `crash` |
| `DeliveryError` naming `lab.retry` | A count is negative, not an integer, a boolean, or the block holds another key or is not a mapping | Match the message to the error table in [Configure the counts](#configure-the-counts) |
| `TypeError` from `retry.policy` | The block was not validated and holds an unknown key | Read the block with `delivery.lab(root)` first |
| Changing `lab.retry` leaves reruns unchanged | The scheduler reads the counts only when it starts | Restart the scheduler service after the change reaches dev |
| The scheduler exits at start with a `jsonschema.ValidationError`, such as `-1 is less than the minimum of 0` | Dev's `delivery.yaml` breaks its schema; `build` reads the counts through `MirrorExpander.retries`, which validates the whole document before any listener binds | Run `delivery.lab` in a checkout of dev, as in [Configure the entry limit](#configure-the-entry-limit), and fix the file by pull request |
| A unit ran twice but the store holds one verdict | The first attempt was run again, so only the final attempt's verdict is stored | Expected; the unit metrics count each attempt that uploaded a report |

## Shadow comparison

`scripts.ci_lab.shadow` compares the lab's shadow verdicts with GitHub Actions, one row per gate
and head. `Shadow.observe(payload)` takes each `check_run.completed` delivery: a check run from the
`github-actions` app gives the Actions verdict of the gate its job name maps to, and a
`lab-shadow/<gate>` check run from the lab's own GitHub App gives the lab verdict and the grader
revision its summary names. `Sources(static, lab_app)` names that App's slug; a `lab-shadow/` run
from any other app, a workflow token included, is ignored. Its caller observes deliveries one at a
time, from one scheduler once a scheduler feeds it. Until then the recorder is a library with no
live caller.

### Gates

| Check run name | Gate |
|---|---|
| `unit*`, such as `unit-selected (3.12, 1)` | `tests` |
| `mutation*`, such as `mutation-stats` | `mutation` |
| A static job, such as `lint`, `type-gate (3.12)` or `semgrep / scan` | its gate id: `lint`, `type-gate`, `semgrep/scan` |
| A static step on the lab side, such as `lab-shadow/lint/lint-check` | the gate id of its job, `lint` |
| Anything else | listed in `agentihooks-ci:shadow-unmapped` |

The matrix suffix in parentheses and the ` / ` of a reusable workflow job drop before the match;
`static_jobs(root)` returns the static gate ids discovered from the workflow tree. A success is a
pass, a failure or a timeout is a fail, and every other conclusion carries no verdict, so a
cancelled or skipped rerun leaves the earlier verdict of its check run in place. Each side folds
its check runs: a gate fails on a head when any of its check runs failed, and a rerun with a
verdict replaces the run it repeats. A row is recorded on every delivery once both sides hold a
verdict, so a head whose shards land one by one can turn into a mismatch and back; the counter
counts each turn into a mismatch, and the report reads the row as it stands.

### Rows

Once a gate and head hold both verdicts, `Shadow.record(head, gate, lab_verdict, actions_verdict,
grader)` writes:

| Where | What |
|---|---|
| Redis `agentihooks-ci:shadow:<gate>:<head>` | A hash with one `lab:<check run>` or `github-actions:<check run>` field per check run, and `head`, `gate`, `grader`, `lab_verdict`, `actions_verdict`, `mismatch` (`0` or `1`) |
| `lab/shadow/<gate>/<head>/<lab>-<actions>.json` | The row, written once per verdict pair |
| ClickHouse `swarm.lab_shadow` | One row per gate, head and verdict pair through the metrics outbox |
| `ci_lab_verdict_mismatch_total{gate}` | Plus one each time a gate and head turn into a mismatch |

### Report

```bash
python -m scripts.ci_lab shadow report --since "$GRADER_REVISION" --repo . --redis "$CI_LAB_REDIS_URL"
```

`--redis` defaults to `CI_LAB_REDIS_URL` and `--repo` to the current directory, a git checkout
that holds the grader revisions. A `--since` revision that names no commit in `--repo` exits 2.
The report counts the rows whose grader revision is the given one or descends from it:

```text
since 5c6db6a99a1b
heads compared 2
gate lint compared 1 mismatches 1
gate tests compared 2 mismatches 0
unmapped docs
```

`shadow report --gate <gate>` is a different command: the acceptance rule report that a gate's
flip pull request pastes. It reads Actions jobs and `lab-shadow/<gate>` check runs from GitHub,
not these rows. See [CI lab shadow acceptance report](lab-shadow.md).

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| A gate never appears in the report | Only one side has a verdict for it, or its check runs were skipped or cancelled | `HGETALL agentihooks-ci:shadow:<gate>:<head>` shows which side is missing |
| A job you expect to compare is listed unmapped | Its name matches no gate rule, or the static job is not in the workflow tree the recorder read | `python -m scripts.ci_lab.static_gates` lists the static jobs |
| A row is left out of a report since a revision | Its grader revision does not descend from that revision in `--repo`, or is not fetched there | `git merge-base --is-ancestor <since> <grader>` in that checkout |
| `error: --redis or CI_LAB_REDIS_URL names the lab Redis` | Neither the flag nor the variable is set | Pass `--redis` or export `CI_LAB_REDIS_URL` |
| `error: --since <revision> names no commit in <repo>` | The revision is mistyped or not fetched into `--repo`, or `--repo` is no git checkout | `git -C <repo> rev-parse --git-dir` succeeds; `git -C <repo> fetch origin dev`, then `git -C <repo> cat-file -t <revision>` prints `commit` |
| Lab verdicts never land although `lab-shadow/` check runs exist | The runs come from an app other than `Sources.lab_app` | The check run's `app.slug` in the delivery |

## Unit sandbox

`scripts.ci_lab.sandbox.Sandbox` runs each lab unit. The static gate executor,
`scripts.ci_lab.run_static.run`, runs one gate step in it, and the run tests engine,
`scripts.ci_lab.run_tests.run`, runs each test batch in it. Every run gets:

- A private run folder under the sandbox root, made with `tempfile.mkdtemp`. It holds `home`,
  `tmp` and `runtime`, each mode `0700`, and the combined standard output and error in `log.txt`.
  The sandbox does not delete the folder; the caller removes it.
- An environment cleared with `env -i` and rebuilt from the caller's whitelist and the private
  variables. A private variable replaces a whitelisted variable of the same name.
- A port range from its `PortAllocator`, given back when the run ends.
- No network unless the caller passes `net=True`.
- Its own session. When the unit exits, cleanly, with a failure or at its timeout, the sandbox sends
  `SIGKILL` to the whole process group, so no child it left behind outlives the run. A timeout's
  outcome code is `None`.

| Variable | Value |
|---|---|
| `HOME` | `<run folder>/home` |
| `TMPDIR` | `<run folder>/tmp` |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` | `.config`, `.cache`, `.local/share` and `.local/state` under `HOME` |
| `XDG_RUNTIME_DIR` | `<run folder>/runtime` |
| `TZ`, `LANG`, `CI`, `PYTHONDONTWRITEBYTECODE` | `UTC`, `C.UTF-8`, `true`, `1` |
| `CI_LAB_PORT_FIRST`, `CI_LAB_PORT_LAST` | The first and last port of the unit's range |

The private folders are environment variables only. The host filesystem stays shared through the
root bind, and only the read only trees are protected.

### Read only trees

`Sandbox(..., readonly=(tree, ...))` takes the trees to protect. Pass absolute paths: `Sandbox`
does not check them, and no test covers a relative tree. Inside bubblewrap,
`scripts.ci_lab.sandbox.argv_for` binds each tree read only with `--ro-bind`, after the
`--dev-bind / /` root bind, so the read only bind covers the writable root at that path. Under a
read only tree a unit cannot create a file or rewrite one, and it cannot restore a write bit with
`chmod`. Each attempt fails with `EROFS`, which the shell prints as `Read-only file system`.
Every other path stays writable, including the unit's working folder.

For a static gate step with the grader tree at `/srv/ci-lab/grader` and the base tree at
`/srv/ci-lab/base`, the unit runs under:

```text
env -i <NAME=value ...> /usr/bin/bwrap --dev-bind / / \
  --ro-bind /srv/ci-lab/grader /srv/ci-lab/grader \
  --ro-bind /srv/ci-lab/base /srv/ci-lab/base \
  --unshare-net --die-with-parent -- \
  bash --noprofile --norc -eo pipefail -c '<the step run>'
```

The binds follow the order of `readonly`, and the static gate executor passes the grader tree
first. `--unshare-net` follows the binds when the unit has no network. A unit runs inside
bubblewrap whenever it needs network isolation or holds a read only tree:

| `net` | Read only trees | bubblewrap | Network inside the unit |
|---|---|---|---|
| `False` | none | required | none: only loopback in a new namespace |
| `False` | one or more | required | none |
| `True` | none | not used; the unit runs under `env -i` only | the host network |
| `True` | one or more | required | the host network |

When bubblewrap is required and `bwrap` is not on `PATH`, `Sandbox.run` raises `CannotRun`
before starting anything. `CannotRun` means the unit cannot run on this worker and its verdict is
red; `run_static.run` and `run_tests.run` do not catch it, so their caller records that red
verdict.

| Unit | `CannotRun` message |
|---|---|
| With read only trees, with or without network | `bwrap is not installed, so a unit with read only trees cannot run` |
| Without network and without read only trees | `bwrap is not installed, so a unit without network cannot run` |

### Which units get read only trees

| Runner | Runs from | Read only trees | Network |
|---|---|---|---|
| `run_static.run(gate, head_tree, base_tree, grader_tree, settings)` | The head tree | The grader tree and the base tree | Never |
| `run_tests.run(batch, tree, venv, slots, lab)` | The tree the batch runs from | None | `lab.net` |
| `python -m scripts.ci_lab.run_mutation REQUEST OUTPUT` | The grader tree; its pytest sessions run in the request's `tree` | The grader tree | Never |

`run_static.run` prepares the step the way the workflow runs it:

- `RUNNER_TEMP` is a fresh folder under `settings.root`. Its `grader` and `base` entries are
  symlinks to the real tree paths, and it holds the empty `GITHUB_STEP_SUMMARY` file.
- `GITHUB_WORKSPACE` and the working folder are the head tree.
- `PATH` is `settings.venv/bin` followed by `/bin:/usr/bin`. `settings.whitelist` and the step's
  own `env` values, with their `${{ }}` expressions read from `settings.context`, come after it
  and can replace it. Neither can replace `RUNNER_TEMP`, `GITHUB_WORKSPACE` or
  `GITHUB_STEP_SUMMARY`.

The bind applies to the real tree paths, so a write through `$RUNNER_TEMP/grader/...` or
`$RUNNER_TEMP/base/...` is refused as well. A gate step can therefore read the grader's scripts
and the base files but cannot rewrite the grader that grades it, or the base it compares against.
It can still write into the head tree, its step summary and its private folders.

Limitations:

- Test units hold no read only trees: `run_tests` builds its `Sandbox` without `readonly` and
  receives no grader or base tree.
- `RUNNER_TEMP` is writable, so a step can delete or replace its own `grader` or `base` symlink.
  That changes only what its own run reads; the trees stay unchanged.
- The worker service runs both runners through `scripts.ci_lab.executors` (see
  [Worker executors](#worker-executors)).

### Head venv builds

`venvs.venv_for(lockfile_digest, tree, root, build)` returns the baked image venv when its
marker holds the digest. The executors pass the sha256 of the head's tracked
`.github/test-requirements.lock` (`venvs.LOCKFILE`), the file the image hashes into that marker;
`uv.lock` is not tracked and is never read. Otherwise it builds `root/<digest>` once (`root` is
`Pod.venvs`, `/work/venvs` on the worker Deployment), under a lock per digest, by running uv
in a `Sandbox` from `build` (`venvs.Build`: ports, whitelist and time limit). The executors pass
`Pod.build`, the pod's ports, whitelist and unit time limit.

| Part | Value |
|---|---|
| Commands | the worker's `uv` with `venv --clear --python <worker python> root/<digest>`, then `pip install --python root/<digest>/bin/python --constraint <tree>/.github/test-requirements.lock --excludes <tree>/.github/test-excludes.txt -r <tree>/pyproject.toml --extra dev --extra all` |
| Runs from | the head tree, with network, under `env -i` and no bubblewrap |
| Environment | `build.whitelist`, `PATH` (the whitelist's, else `/bin:/usr/bin`) and the sandbox's private folders |
| Run folder | one per command under `root/builds`, removed after the command ends with code 0 |

uv and every build hook it starts get no other worker variable in their environment. A build
that runs past the unit time limit or ends with a
nonzero code raises `CannotRun` naming its kept log and writes no marker, so the next call
builds again. A worker without `uv` on its `PATH` raises
`uv is not installed, so a venv cannot be built`. The build is isolated by environment only:
it runs as the worker's user with no PID namespace, so it can still read and write the host
filesystem, `/proc` included.

#### Which venv a unit gets

Every unit kind selects its venv the same way before it runs: the `static` and `tests` executors
from the shared head tree, and `mutation`, for both `stats` and `mutant` units, from its private
trampolined copy of the head. Each calls
`venvs.venv_for(lockfile_digest(tree), tree, pod.venvs, pod.build)`, where
`executors.lockfile_digest(tree)` is the sha256 of the bytes of
`<tree>/.github/test-requirements.lock`:

| Case | Venv the unit runs on | uv cache lookup in the unit's report |
|---|---|---|
| The digest equals the text of `/opt/agentihooks-venv/lockfile.sha256` | The baked image venv `/opt/agentihooks-venv`, used as it is, without taking the build lock | hit |
| An earlier unit on this pod built the digest | `<CI_LAB_WORK_ROOT>/venvs/<digest>`, whose `lockfile.sha256` holds the digest | hit |
| Neither | `<CI_LAB_WORK_ROOT>/venvs/<digest>`, built by the two commands above, then marked with the digest | miss |
| The head has no `.github/test-requirements.lock` | None: `lockfile_digest` raises `FileNotFoundError` and the unit crashes before it runs | none, no report |

`<CI_LAB_WORK_ROOT>` is `/work` on the worker Deployment, so a pod keeps its built venvs at
`/work/venvs/<digest>`, the build lock of each digest at `/work/venvs/<digest>.lock` and the run
folders of builds in progress or kept after a failure under `/work/venvs/builds`. Units on the
same pod that need the same digest wait on that lock, and only the first one builds. `/work` is an
`emptyDir`: built venvs count against the pod's 20 GiB `ephemeral-storage` limit, nothing removes
the venv of a digest no head uses any more, and a new pod starts with none of them. The
[uv cache](ci-lab-node-cache.md) under `/cache/uv` is read only to the worker, and the build's
`env -i` drops `UV_CACHE_DIR`, so a build does not read it.

The image writes the marker when it bakes the venv. `docker/ci-lab/Dockerfile` installs the
project with `--constraint .github/test-requirements.lock --excludes .github/test-excludes.txt`
into `/opt/agentihooks-venv`, then runs
`sha256sum .github/test-requirements.lock | cut -d ' ' -f 1 > /opt/agentihooks-venv/lockfile.sha256`.
The `CI lab image` workflow runs `docker/ci-lab/smoke.sh <image> <digest>` with the sha256 of the
lock it built from, and the smoke fails when the baked marker holds any other digest. A push to
`dev` that changes `.github/test-requirements.lock` or `.github/test-excludes.txt` runs that
workflow and publishes the `dev` tag, which the chart's `worker.image.tag` names with
`imagePullPolicy: Always`, so worker pods created after it carry a venv baked from the new lock.
A head whose lock matches the commit the image was built from therefore runs on the baked venv,
and any other lock costs one build per pod.

A change to `.github/test-requirements.lock` gives the head's units a new venv: the worker
computes a new digest and builds that venv on each pod that leases one of them. It also rekeys
the head's static, test and stats units. The scheduler keys the `toolchain.lockfile` input of
static and test units on the git blob id of the same file in `MirrorExpander.expand`, and the
`runtime` of stats units on the sha256 of its bytes through `stats_cache.runtime_digest`, so no
verdict stored under the old lock answers them. Mutant unit keys, built in `expand_mutation`,
take neither input. A head without the lock is still expanded, with an empty `lockfile` input and
a null stats `runtime`, and its units then crash on the worker as the last row above shows.

#### Verify the venv keying

From a checkout, print the digest a head's units select their venv by:

```bash
head=origin/dev
git show "$head:.github/test-requirements.lock" | sha256sum | cut -d ' ' -f 1
```

Read the digest the running worker image baked, and the venvs that pod built:

```bash
pod=$(kubectl -n ci-lab get pods -l app.kubernetes.io/component=worker \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n ci-lab exec "$pod" -- cat /opt/agentihooks-venv/lockfile.sha256
kubectl -n ci-lab exec "$pod" -- sh -c 'ls /work/venvs; cat /work/venvs/*/lockfile.sha256'
```

When the first command and the baked marker print the same digest, the head's units run on the
baked venv and `/work/venvs` gains no folder for it. Otherwise the first unit of that head on the
pod builds `/work/venvs/<digest>`, and its marker prints the digest once the build ends.
`executors.settings` creates `/work/venvs` when the worker starts, so on a pod that has built no
venv `ls` prints nothing and `cat` fails with `No such file or directory` on the unmatched
pattern. The uv lookups reach the scheduler's counters, read as in
[Verify the running service](#verify-the-running-service):

```bash
curl -fsS http://lab:9108/metrics | grep -E '^ci_lab_cache_(hits|misses)_total\{cache="uv"\}'
```

Misses rise once per pod and lock while a new lock is first leased, and hits rise for every other
unit.

### Use it

Run one gate step of the grader's workflow over a head:

```python
import json
from pathlib import Path

from scripts import ci_wiring
from scripts.ci_lab import run_static, static_gates
from scripts.ci_lab.sandbox import PortAllocator

grader = Path("/srv/ci-lab/grader")
base = Path("/srv/ci-lab/base")
head = Path("/srv/ci-lab/head")

workflow = (grader / ".github/workflows/test.yml").read_text()
registry = json.loads((grader / ".github/input-keys.json").read_text())
gates = {gate.name: gate for gate in static_gates.discover(workflow, registry, ci_wiring.load(grader))}

settings = run_static.Settings(
    root=Path("/srv/ci-lab/runs"),
    venv=Path("/srv/ci-lab/venv"),
    ports=PortAllocator(first=20000, last=20999, width=100),
    whitelist={},
    timeout=600,
    context={"inputs.base": "origin/dev"},
)
result = run_static.run(gates["lint/lint-check"], head, base, grader, settings)
print(result.green, result.code, result.log, result.report)
```

`python -m scripts.ci_lab.static_gates --root /srv/ci-lab/grader` prints one JSON line per gate,
and its `name` field is the key above. Discover gates from the grader tree, never from the head.
Pass absolute paths for the trees, `settings.root` and `settings.venv`. Nothing checks them, and
the step runs from the head tree, so a relative `RUNNER_TEMP`, `GITHUB_WORKSPACE` or `PATH` entry
resolves against the head. The worker needs bubblewrap for every static gate run.

To run any other command with read only trees, build the sandbox directly:

```python
from pathlib import Path

from scripts.ci_lab.sandbox import PortAllocator, Sandbox

trees = (Path("/srv/ci-lab/grader"), Path("/srv/ci-lab/base"))
unit = Sandbox(
    root=Path("/srv/ci-lab/runs"),
    cwd=Path("/srv/ci-lab/head"),
    ports=PortAllocator(first=20000, last=20999, width=100),
    whitelist={"PATH": "/usr/bin:/bin"},
    readonly=trees,
)
outcome = unit.run(["bash", "-c", 'touch "$1/planted.txt"', "probe", str(trees[0])], timeout=60)
print(outcome.code, outcome.log.read_text())
```

That run exits non zero, and its log reads
`touch: cannot touch '/srv/ci-lab/grader/planted.txt': Read-only file system`.

### Verify

Check that bubblewrap can create user and network namespaces on the worker host:

```bash
bwrap --dev-bind / / --unshare-net true && echo isolates
```

Check that a read only bind refuses a write on that host:

```bash
probe="$HOME/ro-probe"
mkdir -p "$probe"
bwrap --dev-bind / / --ro-bind "$probe" "$probe" --unshare-net --die-with-parent -- \
  bash -c 'echo planted > "$1/planted.txt"' probe "$probe"
ls -A "$probe"
```

The `bwrap` command prints `Read-only file system` and exits 1, and `ls` prints nothing.

The pull request tier proves the sandbox under real bubblewrap in the writer lock workflow's
`lab-sandbox` job. It runs on every pull request that changes `scripts/ci_lab/sandbox.py`,
`scripts/ci_lab/run_static.py`, `scripts/ci_lab/run_tests.py`, one of their three test files or
`.github/workflows/writer-lock.yml`, and fails when any of those tests fails or skips. See
[Pull request job under real bubblewrap](#pull-request-job-under-real-bubblewrap).
These tests prove the read only trees:

| Test | Proves |
|---|---|
| `test_run_static.py::test_the_step_runs_with_the_grader_and_base_trees_bound_read_only` | The exact bubblewrap arguments of a gate step: the root bind, the grader bind, the base bind, `--unshare-net`, `--die-with-parent` |
| `test_run_static.py::test_a_step_cannot_write_into_the_grader_or_base_tree` | Under real bubblewrap, a step's writes through `$RUNNER_TEMP/grader` and `$RUNNER_TEMP/base` are refused, both trees stay empty and its write into `$GITHUB_WORKSPACE` lands |
| `test_sandbox.py::test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree` | With and without network, `chmod`, a rewrite and a new file under each tree fail with `EROFS` |

In `feature-map.json`, `lab-static-read-only-trees` maps the bubblewrap arguments test of
`test_run_static.py`. `lab-sandbox-read-only-trees` maps the `test_sandbox.py` tests that check
the `--ro-bind` arguments and the `CannotRun` refusal without `bwrap`. The two write refusal tests
above carry no feature entry; the job that runs them unskipped is mapped as
`lab-sandbox-bwrap-pull-request-tier`, below.

### Pull request job under real bubblewrap

The four tests that need real bubblewrap skip in the unit shards of the Tests workflow, which run
on self hosted runners where bubblewrap cannot isolate. Each one first probes
`bwrap --dev-bind / / --unshare-net true` and skips when `bwrap` is missing or the probe fails.
The `lab-sandbox` job of the `Writer lock` workflow (`.github/workflows/writer-lock.yml`) runs
them on a GitHub hosted runner, where a skip turns the job red:

| Test | Skip reason when bubblewrap cannot isolate |
|---|---|
| `test_sandbox.py::test_loopback_works_and_egress_is_refused_without_network` | `bubblewrap cannot create a network namespace on this runner` |
| `test_sandbox.py::test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree[False]` | `bubblewrap cannot create namespaces on this runner` |
| `test_sandbox.py::test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree[True]` | `bubblewrap cannot create namespaces on this runner` |
| `test_run_static.py::test_a_step_cannot_write_into_the_grader_or_base_tree` | `bubblewrap cannot create namespaces on this runner` |

The job runs every test of the three files, so the argument and refusal tests that need no real
bubblewrap run there too.

#### When the job runs

| Run | `lab-sandbox` |
|---|---|
| A pull request whose diff names a path in `LAB_SANDBOX_INPUTS` | Runs, called from the Tests workflow's `writer-lock` job |
| A pull request that changes none of those paths | Skipped |
| A direct dispatch of the `Writer lock` workflow, on any branch | Runs, whatever the branch changed |
| The Tests workflow on a push to `dev`, or on its own manual dispatch | Skipped |
| A merge group | Not reached: the Tests `writer-lock` job runs only on `push`, `pull_request` and `workflow_dispatch` |

The job's condition:

```yaml
if: (github.event_name == 'pull_request' && needs.inputs.outputs.lab_sandbox == 'true') || github.workflow == 'Writer lock'
```

GitHub gives a called workflow its caller's `github` context, so under the Tests workflow
`github.workflow` is `Tests` and the second clause holds only when the `Writer lock` workflow
itself is dispatched. A Tests run on a pull request that changes no input shows
`writer-lock / lab-sandbox` as skipped.

The `inputs` job's `Select lab sandbox inputs` step (id `lab-sandbox`) runs only on
`pull_request`. It diffs the merge base of `origin/<base branch>` and the pull request head with
`git diff --no-renames --name-only -z`, compares each changed path with every entry of
`LAB_SANDBOX_INPUTS` for exact equality, and writes `due=true` or `due=false` to the job's
`lab_sandbox` output:

```yaml
LAB_SANDBOX_INPUTS: >-
  scripts/ci_lab/sandbox.py scripts/ci_lab/run_static.py scripts/ci_lab/run_tests.py
  tests/ci_lab/test_sandbox.py tests/ci_lab/test_run_static.py tests/ci_lab/test_run_tests.py
  .github/workflows/writer-lock.yml
```

Paths match whole. A change to `scripts/ci_lab/worker.py`, or to any other module under
`scripts/ci_lab`, does not select the job. With renames off, a file moved away from an input path
shows as deleted under its old name, and that selects it.

#### What the job does

The job runs on `ubuntu-latest` with a 10 minute timeout. It finished in under 40 seconds in the
green delivery run.

| Step | Does | Fails the job when |
|---|---|---|
| Checkout | Checks out `github.sha` without persisted credentials | |
| Python and uv | Python 3.12, and uv `0.12.23` pinned by checksum with no cache | |
| `Let bubblewrap create user and network namespaces` | Installs `bubblewrap` with apt only when `bwrap` is not on `PATH`, runs `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, then probes `bwrap --dev-bind / / --unshare-net true` | The install, the sysctl or the probe exits non zero |
| `Install dependencies` | `uv pip install --system` with `.github/test-requirements.lock` and `.github/test-excludes.txt`, through `.github/scripts/pypi-retry.sh`, which retries a package index outage until 150 seconds of backoff are spent, not counting the attempts | The install still fails after its retries |
| `Run the lab sandbox tests under real bubblewrap` | `python -m pytest` over `tests/ci_lab/test_sandbox.py`, `tests/ci_lab/test_run_static.py` and `tests/ci_lab/test_run_tests.py` with `-n 0 -v -rs --tb=short`, its output copied to `$RUNNER_TEMP/lab-sandbox.log` | A test fails (exit 1), no test is collected (exit 5), or the log contains `SKIPPED` (exit 1) |

The apt install reads only the Ubuntu archive source list, `/etc/apt/sources.list.d/ubuntu.sources`,
or `/etc/apt/sources.list` when that file is absent. It passes `Dir::Etc::sourceparts=-`, so no
other source list on the image is read, and `Acquire::Retries=5`. A failed index refresh does not
stop the step: it prints the warning below and installs from the indexes that loaded. In the green
delivery run the hosted image already carried `bwrap`, so the install did not run.

```text
::warning::apt archive index refresh incomplete; installing bubblewrap from the indexes that loaded
```

`-n 0` runs the tests in one process without xdist workers, and `-rs` prints each skip with its
reason in the short summary. GitHub runs a `shell: bash` step as
`bash --noprofile --norc -eo pipefail`, so the pytest exit code survives the `tee`. A skip ends the
step with:

```text
::error::A lab sandbox test skipped, so bubblewrap did not isolate it on this runner.
```

#### How it gates a pull request

The Tests workflow's `writer-lock` job calls `writer-lock.yml`, and `Gate — Required` needs
`writer-lock`. A failed `lab-sandbox` job fails `writer-lock`, so `Gate — Required` is red and the
pull request cannot enter the merge queue. The check shows on the pull request as
`writer-lock / lab-sandbox`. A skipped `lab-sandbox` job leaves `writer-lock` green.

The Tests workflow skips its `writer-lock` job when `lab.authoritative` in `delivery.yaml` names
`writer-lock`, and `Gate — Required` then reads the `lab/writer-lock` check run in its place
(`.github/gate-wiring.json`). The list is empty on `dev`, so Actions runs the job.

#### Runner class

`.github/runner-classes.json` lists `writer-lock.yml/lab-sandbox` as `hosted`, and the runner
routing tests keep the job in their `CLASSED` set of jobs that stay on GitHub hosted runners. The runner class check refuses any other `runs-on` for it:

```bash
python -m scripts.ci_runner_classes
```

It prints `Runner classes: <n> workflows, <m> jobs, 0 problems` when every job matches its class.
A `lab-sandbox` job routed to a self hosted runner label prints
`::error::writer-lock.yml/lab-sandbox: runs-on must be ubuntu-latest for a hosted job` and exits 1.

#### The workflow is frozen

The job runs from the pull request head, like every `Writer lock` and Tests job, so a head could
otherwise edit or drop it on itself. `tests/registry/FROZEN_GATES.json` holds
`.github/workflows/writer-lock.yml` with the blobs the base accepts (pull request
[#11870](https://github.com/the-cloud-clockwork/agentihooks/pull/11870), feature
`frozen-writer-lock-workflow`). The protected grader refuses a head that changes the workflow to
any other content:

```text
The head changes the frozen gate file .github/workflows/writer-lock.yml to content tests/registry/FROZEN_GATES.json on the base does not accept; announce its blob there by its own pull request first.
```

#### Change the job

Adding an input path or a test file, or changing a step, takes two pull requests:

1. Edit `.github/workflows/writer-lock.yml` on a branch, commit it and read its blob with
   `git rev-parse HEAD:.github/workflows/writer-lock.yml`.
2. Open a first pull request that only adds that blob to the `.github/workflows/writer-lock.yml`
   list in `tests/registry/FROZEN_GATES.json`, and merge it. A head that changes the registry and
   the workflow together is refused with
   `The head changes tests/registry/FROZEN_GATES.json and the frozen gate file .github/workflows/writer-lock.yml together; land the registry by its own pull request first.`
3. Open the second pull request with the workflow change and its guard tests. A new input path
   goes into `LAB_SANDBOX_INPUTS` and the cases of `test_lab_sandbox_selection`. A new test file
   goes into the run step, `LAB_SANDBOX_INPUTS`, the argument check of
   `test_the_lab_sandbox_run_fails_on_a_skipped_or_failed_test`, and the expected list of files a
   step reaches in `tests/test_ci_pull_request_api.py::test_no_script_a_step_runs_calls_the_api`.
   The second pull request changes `writer-lock.yml`, so the job selects itself and runs the new
   version.

#### Run it by hand

Dispatch the workflow on a branch and read the job:

```bash
gh workflow run writer-lock.yml --ref <branch>
gh run list --workflow writer-lock.yml --branch <branch> --limit 1 --json databaseId,status,conclusion
gh run view <run id> --json jobs --jq '.jobs[] | select(.name == "lab-sandbox") | {databaseId, conclusion}'
gh run view <run id> --job <job id> --log | grep -E 'PASSED|FAILED|SKIPPED|::error::|::warning::'
```

A dispatch also runs the `scenario` job, the Postgres writer lock scenarios described in
[Shared ledger database sessions](shared-ledger-database.md#verification-in-ci). The `events`,
`tick`, `hive-join` and `features` jobs run only on a pull request, so they stay skipped. A green
`lab-sandbox` job lists every test of the three files as `PASSED`; the delivery head ran 40 with
none skipped.

To show the job catches a weakened sandbox without opening a pull request, commit the plant on a
`diffcheck/` branch and dispatch on it. With `--ro-bind` changed to `--bind` in
`scripts.ci_lab.sandbox.argv_for`:

```bash
git switch -c diffcheck/<name>
git commit -am "Plant a writable grader tree"
git push origin diffcheck/<name>
gh workflow run writer-lock.yml --ref diffcheck/<name>
```

The job is red. `test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree` fails on
both parameters because `chmod`, the rewrite and the new file land in both trees where `EROFS` was
expected, and `test_a_unit_with_read_only_trees_runs_inside_bwrap_with_each_tree_bound_read_only`
fails on both parameters because the arguments no longer carry `--ro-bind`. `run_static.run`
builds its unit through the same `argv_for`, so the job's `test_run_static.py` bind tests,
`test_the_step_runs_with_the_grader_and_base_trees_bound_read_only` and
`test_a_step_cannot_write_into_the_grader_or_base_tree`, see the writable binds as well. Delete the
branch after reading the run with `git push origin --delete diffcheck/<name>`.

#### Guard tests

`tests/test_ci_lab_sandbox_pr.py` holds the job's wiring; every test there maps to the
`lab-sandbox-bwrap-pull-request-tier` entry of `feature-map.json`:

| Test | Proves |
|---|---|
| `test_lab_sandbox_selection` | Runs the selection step's shell with a fake `git`: each of the seven inputs writes `due=true`, `scripts/ci_lab/worker.py` and `README.md` write `due=false`, and the step runs only on `pull_request` |
| `test_the_lab_sandbox_job_feeds_the_required_gate_on_selected_pull_requests` | `Gate — Required` needs `writer-lock`, the Tests workflow calls `writer-lock.yml` on pull requests, and the job's `needs`, `if`, `runs-on` and output wiring hold; the `scenario` job does not need it |
| `test_the_job_fails_unless_bubblewrap_isolates_after_the_userns_restriction_is_lifted` | The prepare step comes before the run step, runs the sysctl and then the probe, and exits with the probe's code |
| `test_the_job_installs_bubblewrap_from_the_ubuntu_archive_when_the_runner_lacks_it` | Without `bwrap` on `PATH`, apt updates and installs from the archive source list only, with retries, before the sysctl |
| `test_the_lab_sandbox_run_fails_on_a_skipped_or_failed_test` | A pass exits 0, a skip and a failure exit 1, an empty collection exits 5, the three files run in order and the log lands in `$RUNNER_TEMP/lab-sandbox.log` |

The prepare step tests run its shell with fake `sudo` and `bwrap` on `PATH`, so they need neither
root nor bubblewrap and run in every unit shard. `tests/test_ci_writer_lock.py` expects the
`lab_sandbox` output of the `inputs` job.

#### Limitations

- Only the three test files in the run step run. A new real bubblewrap test elsewhere is not
  covered until the run step names its file.
- The unit shards still skip the four real bubblewrap tests. This job is the only place they run
  in CI.
- A pull request runs the job only when its diff names a path in `LAB_SANDBOX_INPUTS`. A change to
  a module the sandbox or its two runners import does not select it: for example
  `scripts.ci_mutation.runner`, which `Sandbox` runs units through, or `scripts.ci_lab.static_gates`.
- The job never runs in the merge queue or on a push to `dev`, so a merge group is not retested
  under real bubblewrap.

#### Delivery evidence

Pull request [#11849](https://github.com/the-cloud-clockwork/agentihooks/pull/11849) delivered the
job:

- Skipped before:
  [Tests run 43726066026](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/43726066026),
  job `unit-selected (3.12, 1)` on a self hosted runner, skipped all three real bubblewrap cases that
  existed then.
- Red first:
  [Tests run 44211389907](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44211389907)
  on `25a402b959` failed the 14 guard test cases before the job existed.
- Planted fault, writable trees:
  [Writer lock run 44213673884](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44213673884)
  on a `diffcheck/` branch binding the trees with `--bind` was red. The job then ran
  `tests/ci_lab/test_sandbox.py` alone: 4 failed and 15 passed, and `chmod`, the rewrite and the
  new file landed in both trees.
- Planted fault, no namespaces:
  [Writer lock run 44213944663](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44213944663),
  with `user.max_user_namespaces=0` and no probe, skipped the real cases, and the run step failed
  on the skip.
- Required on pull requests:
  [Tests run 44216326032](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44216326032)
  on `65b1dbed5a` carried a plant only the hosted job could catch. `Gate — Required` was red, and
  the only other failing check was `writer-lock / lab-sandbox`.
- Green:
  [Tests run 44217954418](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44217954418)
  on `d74089e45d`. `writer-lock / lab-sandbox` passed 40 tests with none skipped in under 40
  seconds, and `Gate — Required` passed.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `CannotRun: bwrap is not installed, so a unit with read only trees cannot run` | The worker has no `bwrap` on `PATH`. Every static gate run needs it | `command -v bwrap` on the worker; install the `bubblewrap` package in the worker image |
| The unit log reads `bwrap: No permissions to create a new namespace` and the gate is red | The host blocks unprivileged user namespaces, as Ubuntu's AppArmor restriction does | `sysctl -n kernel.apparmor_restrict_unprivileged_userns` prints `1`; `bwrap --dev-bind / / --unshare-net true` fails |
| A gate step that passes in Actions fails in the lab with `Read-only file system` | The step writes under `$RUNNER_TEMP/grader`, `$RUNNER_TEMP/base` or a path inside either tree, such as a tool cache | Find the path in the unit's `log.txt`; write outputs to `$RUNNER_TEMP`, the head tree or the private `HOME` instead |
| A test unit wrote into the grader tree | Test units get no read only trees | See [Which units get read only trees](#which-units-get-read-only-trees) |
| `CannotRun: no free port range` | Every range of the allocator is in use | The allocator holds `(last - first + 1) // width` ranges; run fewer units at once or widen the span |
| The `lab-sandbox` job fails with `A lab sandbox test skipped, so bubblewrap did not isolate it on this runner.` | A real bubblewrap test skipped because the runner could not create namespaces | The skip reason in the job's `-rs` summary, such as `bubblewrap cannot create namespaces on this runner` |
| `lab-sandbox` fails in `Let bubblewrap create user and network namespaces` on the probe, after the sysctl ran | The runner cannot create user or network namespaces even with the AppArmor restriction lifted | The `bwrap` error in the step log; `runs-on` must be `ubuntu-latest` and the job's class `hosted` |
| `lab-sandbox` fails in that step on the `sysctl` line | The runner kernel has no `kernel.apparmor_restrict_unprivileged_userns` setting, or `sudo` is refused | The `sysctl` error in the step log; the job needs a GitHub hosted Ubuntu runner |
| The apt archive warning appears and the `bubblewrap` install fails | The Ubuntu archive index did not load, and no loaded index carries `bubblewrap` | The apt lines above the failure name the source that did not load |
| `lab-sandbox` fails with pytest exit code 5 and `no tests ran` | A file in the run step's list was renamed, moved or emptied | Update the run step and the guard tests as in [Change the job](#change-the-job) |
| A pull request changed sandbox code and no `writer-lock / lab-sandbox` check ran | The changed path is not one of `LAB_SANDBOX_INPUTS`, which match whole, or the Tests workflow skipped its `writer-lock` job | The `Select lab sandbox inputs` step log in the `writer-lock / inputs` job; or dispatch `Writer lock` on the branch as in [Run it by hand](#run-it-by-hand) |
| The protected grader refuses the pull request with `The head changes the frozen gate file .github/workflows/writer-lock.yml ...` | The head changes the workflow to a blob the base registry does not accept | Announce the blob first, as in [Change the job](#change-the-job) |
| `writer-lock.yml/lab-sandbox: runs-on must be ubuntu-latest for a hosted job` | The job's `runs-on` was routed to a self hosted runner label | Restore `runs-on: ubuntu-latest`; the self hosted runners have no bubblewrap, so every real case would skip |

## Static plant proofs

Run `python -m scripts.ci_lab grade --plant lint` from a repository checkout to prove
an import sorting fault. Use `--plant gate-script` to prove that a head cannot teach its
own comment grader to accept a new directive. Test and mutation plants are separate proofs.

The command also accepts every JSON plant name in the catalog, including dotted names such as
`lint.lint-check`. A plant keeps its full discovered step in `gate`; grading reads that step's job
check and requires its named diagnostic. The JSON result and summary row include `green_run` and
`red_run`, the exact grade identifiers for the two check links. `lint.threshold-pins` is the shorter
alias for the threshold pin plant; its full gate identity is unchanged.

Static execution reads `tests/ci_lab/static_gate_exclusions.json` from the protected
grader revision. Pull requests and hotfixes use their base; promotion and backfill
use the integrated head. Only exact gate name matches are excluded. Each affected
calling job keeps Actions authority. The protected Actions readers apply the same
calling job filter for pull requests and merge groups. A missing catalog preserves existing plans,
and malformed policy refuses expansion. The protected catalog records the reason
for each exclusion.

The command creates an unchanged twin and its planted child from `origin/dev`, with fixed
commit identity and dates. It preserves the checkout, HEAD and normal index. `--base`
selects another protected revision; `--repository` overrides the GitHub URL inferred from
`origin`; `--timeout` bounds each grade wait in seconds, with a default of 900.

Both commits use a branch under `diffcheck/lab-plant-`, derived from the plant and base.
The twin is pushed and must pass before the plant is pushed. A rerun on the same base
reuses those commits; another base gets a separate branch. No plant pull request is opened.

The authenticated push receiver must recognize that prefix, resolve both heads against
trusted `dev` and grade them in the pull request class with their merge base as grader.
Proofs runners hold no lab notice secret and make no request to the receiver. An ordinary
promotion grade cannot satisfy this proof: its grade identity and grader differ.

Only checks carrying the exact grade key are read. The twin's required check must pass;
the plant's required check must fail with `lint` as its only failing static gate. Failed
pushes, expired or incomplete grades and other failing gates fail the command. The failing
gate must carry its expected diagnostic in the trusted check output text: Ruff `I001` or
`forbidden comment`. A check title or summary cannot satisfy that requirement.

Success prints a JSON row. When `GITHUB_STEP_SUMMARY` is set, the command also appends a row
with green and red GitHub check links, which remain usable outside the cluster.
