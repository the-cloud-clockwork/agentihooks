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
[Worker scheduling priority](#worker-scheduling-priority) covers it.

## Entry points

| Command | Module form | What it does today |
|---|---|---|
| `agentihooks ci-lab-scheduler --self-test` | `python -m scripts.ci_lab scheduler --self-test` | Checks the scheduler contract offline and prints it as JSON |
| `agentihooks ci-lab-scheduler [--interval S] [--passes N]` | `python -m scripts.ci_lab scheduler [--interval S] [--passes N]` | Runs the scheduler service: reads the runtime config, serves the lease API, `/capacity` and `/grades/<sha>` on port 8800 and `/metrics` on 9108, and runs one grading pass every interval (2 seconds), forever unless `--passes` is set |
| `agentihooks ci-lab-worker --self-test` | `python -m scripts.ci_lab worker --self-test` | Refuses any GitHub App variable, checks the worker contract offline and prints it as JSON |
| `agentihooks ci-lab-worker` | `python -m scripts.ci_lab worker` | Runs the worker service: builds the pod from the worker environment, leases units from the scheduler and runs each through the adapter of its kind (see [Worker executors](#worker-executors)) until SIGTERM, then lets running units drain |

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
| Import the lab scheduler's modules inside the swarm image | Swarm container smoke | The built swarm image cannot import the scheduler, interaction, test expansion or the CI test planner |

`docker/ci-lab/smoke.sh` also imports the worker's executors, test runner and pricing inside the
built CI lab image.

## Environment

| Name | Scheduler | Worker | Meaning |
|---|---|---|---|
| `AGENTIHOOKS_BUS*` | yes | yes | Event bus connection settings |
| `CI_LAB_MINIO_*` | yes | yes | Object store endpoint, bucket and credentials, passed by reference |
| `GITHUB_APP_*` | yes | never | GitHub App identity used to post check runs and read dev push manifest artifacts; the App needs checks write and actions read |
| `CI_LAB_REDIS_URL` | yes | no | Queue, lease and verdict index store |
| `CI_LAB_RUNTIME_CONFIG` | yes | no | Path of the mounted runtime config the scheduler reads at start, before it connects anywhere; its budget sets the budget fraction and shed classes of `/capacity` and `/metrics`; `python -m scripts.ci_lab runtime <path>` validates it |
| `CI_LAB_SCHEDULER_URL` | no | yes | Base address of the scheduler lease API |
| `CI_LAB_SLOTS` | no | yes | Number of units the worker runs at once |
| `CI_LAB_GIT_MIRROR` | yes | yes | Path of the shared git mirror the worker cuts worktrees from and the scheduler borrows objects from |
| `CI_LAB_WORK_ROOT` | no | yes | Folder of the worker's private git store, its trees, venvs and unit runs and reports |
| `CI_LAB_MANIFEST` | no | yes | Path of the dependency manifest JSON a test unit's batch is checked against |
| `CI_LAB_FLAKES` | no | yes | Path of the flake ledger a test unit appends to when its retry passes |
| `CI_LAB_MUTATION_CACHE` | no | yes | Folder of the mutation stats cache, keyed by tree content |
| `CI_LAB_KILLERS` | no | yes | Path of the kill memory that orders a mutant's tests, its remembered killer first |
| `CI_LAB_MUTATION_BUDGET` | no | yes | Seconds a mutant unit may take before its mutants run side by side |
| `CI_LAB_MUTATION_CAP` | no | yes | Most mutants a mutant unit grades at once |
| `CI_LAB_MINIO_ENDPOINT`, `CI_LAB_MINIO_BUCKET`, `CI_LAB_MINIO_ACCESS_KEY`, `CI_LAB_MINIO_SECRET_KEY` | yes | no | The object store names the scheduler reads |
| `CI_LAB_MIRROR_STORE` | yes | no | Folder of the scheduler's private git stores, one per repository, that borrow the mirror's objects |
| `CI_LAB_REPOSITORY` | yes | no | Repository URL whose dev runner trust list admits head actors |
| `AGENTIHOOKS_LAB_REMOTE`, `AGENTIHOOKS_LAB_REMOTE_TOKEN` | yes | no | The lab remote URL a mirror fetches a sha GitHub lacks from, and its token, passed by reference; unset, the mirrors fetch from GitHub only |
| `CI_LAB_IMAGE` | yes | no | Worker image name keyed into static unit keys; empty when unset |
| `CI_LAB_CONTROL_TOKEN` | yes | no | Bearer token the [control routes](#control-routes) require, passed by reference; unset, they refuse every request. The swarm host that presses panic or merge priority holds the same variable |
| `UV_CACHE_DIR` | no | yes | Shared uv cache for virtual environments |

A worker started with any `GITHUB_APP_*` variable in its environment refuses to start. It exits
1 and names each variable it found. Only the scheduler holds the App key.

## Ports

| Port | Service | Serves |
|---|---|---|
| `8800` | scheduler | The lease API workers call, the [control routes](#control-routes) a swarm calls, [`/capacity`](#capacity-document) and [`/grades/<sha>`](#grades-of-a-commit) |
| `9108` | scheduler and worker | Prometheus metrics at `/metrics` |

## Control routes

The scheduler port serves three routes a swarm calls with `Authorization: Bearer <token>`, the token being the
scheduler's `CI_LAB_CONTROL_TOKEN`. Any other token, or a scheduler without one, answers 401 and changes nothing. A
head is a pull request head commit or its head branch. The state lives in the lab Redis.

| Route | Body | Effect |
|---|---|---|
| `POST /priority` | `{"heads": [...], "class": "hotfix"}` | Marks the heads hotfix for six hours; a marked head is received in the hotfix class, with no quiet window |
| `POST /pause` | `{"class": "<class>", "except": [...]}` | Holds every waiting head of the class but the excepted ones until resume |
| `POST /resume` | none | Lifts every pause and answers the classes it held |

`agentihooks swarm <slug> panic on --fix <url>` marks the fix branches and the recorded fixer branches hotfix and
pauses `pull_request` except them, printing `lab paused pull request grading except <heads>`; `panic off`, and the
release of an automatic panic, resume the lab only when that swarm's panic paused it. `agentihooks swarm <slug> merge priority <url>` marks
the pull request head hotfix and records the lab's reply under `lab` in the queue journal. The swarm reads the lab
address set with `agentihooks swarm <slug> set lab-url=<address>` and the token from `CI_LAB_CONTROL_TOKEN` on its
host.

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

The scheduler sets every metric its queue, lease and worker state answers, on one
`scripts.ci_lab.metrics.Registry` the `Scheduler` builds and `listen` serves on 9108:

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

A worker unit's report carries `caches` from `scripts.ci_lab.caches.report`: per revision it
leased, a git hit when the pod store already held the commit and a miss when `Mirror.fetch`
fetched it; per venv, a uv hit when it reused one and a miss when `venvs.venv_for` built it.
`ci_lab_mirror_age_seconds`, `ci_lab_github_rate_remaining` and `ci_lab_verdict_mismatch_total`
answer from no queue, lease or worker state and stay without samples. The counters live in the
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
A queue job no live grade holds counts toward the busy slots only.
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
| `CILabWorkersPending` | At least one `ci-lab-worker-*` pod in `ci-lab` is Pending | 15 minutes | the pool | The `cluster-autoscaler-oci-status` ConfigMap for the CI pool, and whether runners hold the CI nodes; every runner outranks a worker, see [Worker scheduling priority](#worker-scheduling-priority) |
| `CILabBudgetShedding` | `ci_lab_class_shed` is 1 for a class: the day's core budget left fell below that class's shed fraction, so its units wait for the budget to reset. Only backfill, promotion and queue shed | 5 minutes | class | Budget left on the dashboard and the shed fractions in the runtime config at `CI_LAB_RUNTIME_CONFIG` |
| `CILabPullRequestBlockedByCap` | Pull request units are queued, every slot is busy, and a HorizontalPodAutoscaler in `ci-lab`, the one KEDA is to build for the workers, wants at least its maximum replicas. An autoscaler at its maximum in another namespace does not count | 10 minutes | the pool; its value is the queued pull request units | The worker maximum and the CI node cap; raising either is the operator's elasticity budget |
| `CILabSchedulerBlind` | Prometheus holds no `ci_lab_slots_wanted` series while the `ci-lab-scheduler` Deployment asks for at least one replica. A scheduler scaled to zero, or no Deployment, keeps it silent | 10 minutes | the scheduler | The scheduler's scrape target and port `9108`; the lab chart's planned KEDA scaler sizes the worker pool from this gauge and holds its fallback floor while it is missing |
| `CILabCacheMissRatioHigh` | The git or uv node cache missed more than half its lookups over 30 minutes, with at least 10 misses. A cache with misses and no hit series counts its hits as 0. The verdict cache never fires it | 15 minutes | cache | The node cache DaemonSet on the CI nodes and the age of its git mirror |
| `CILabScaleFlapping` | Over the last hour, sampled every 5 minutes, the worker Deployment's replicas rose against 5 minutes earlier in at least 4 samples and fell in at least 4. One ramp up and down again does not fire it | no hold | the pool; its value is the rising samples | Lengthen the scale down stabilisation window of the lab chart's planned ScaledObject, since each swing churns CI nodes through the autoscaler |
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

Only the scheduler serves lab metrics; the worker serves none. `scripts.ci_lab.scheduler.listen`
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
| `ci_lab_slots_wanted` | `CILabSchedulerBlind`, the slots panels, the lab chart's planned KEDA scaler | `CILabSchedulerBlind` fires 10 minutes after a `ci-lab-scheduler` Deployment with a replica appears, and the `wanted` lines stay empty. The scheduler sets it from `scripts.ci_lab.budget.slots_wanted` on every scrape |
| `ci_lab_units_overdue` | `CILabUnitsOverdue` | The alert cannot fire |
| `ci_lab_workers_ready` | Workers ready against desired | The `heartbeat` line stays empty |
| `ci_lab_core_seconds_total`, `ci_lab_heads_graded_total` | Core minutes per merged pull request | The stat reads No data |
| `ci_lab_cache_hits_total`, `ci_lab_cache_misses_total` | `CILabCacheMissRatioHigh`, Cache hit ratio | `metrics.observe_records` counts the `verdict` cache but has no live caller, and nothing counts `git` or `uv`, so the alert cannot fire and the panel stays empty |
| `ci_lab_verdict_mismatch_total` | `CILabVerdictMismatch`, Shadow mismatches per gate | `shadow.Shadow` counts it in a registry of its own and has no live caller, so the alert cannot fire |
| `ci_lab_unit_duration_seconds`, `ci_lab_mirror_age_seconds`, `ci_lab_units_redelivered_total`, `ci_lab_units_lost_total`, `ci_lab_github_rate_remaining` | No alert or panel | Nothing, since no alert or panel reads them. `metrics.observe_records` sets the unit duration when a caller runs it; no code sets the other four |

The kube-state-metrics series need the lab chart's objects in the `ci-lab` namespace: the
`ci-lab-scheduler` and `ci-lab-worker` Deployments, the worker pods, and the KEDA ScaledObject
with the HorizontalPodAutoscaler KEDA builds from it. Prometheus reaches port `9108` only through
a ServiceMonitor for the scheduler. As of the deployment repository's pull request 1825, neither
repository defines any of these objects. Until the lab chart ships them, every lab alert stays
inactive, and only the spend estimate and the CI nodes panel show data, since both read node
taints.

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

## Worker scheduling priority

The PriorityClass `ci-lab-worker` ranks lab worker pods below every CI runner
(`arc-runners-oss-checks`, 200) and above the unused `arc-runners-oss-batch-priority` class (100).
When the nodes are full, new pods wait in Pending and the scheduler tries them in priority order as
capacity frees, so a pending pull request check or merge queue runner gets each freed node it fits
before a pending lab worker does. A worker can still take a node too small for the waiting runner.
`ci-lab-worker` and `arc-runners-oss-checks` do not preempt, so a running worker keeps its node
until it ends, and a check can still wait for a node a worker took first. A pod gets this rank only
when its spec names the class;
[Use it from a worker pod](#use-it-from-a-worker-pod) shows how.

| PriorityClass | Value | `preemptionPolicy` | Pods that name it |
|---|---|---|---|
| `qitp-trading` | 1000 | can preempt lower priority pods | Trading workers |
| `arc-runners-oss-checks` | 200 | `Never` | Every CI runner: pull request checks, image builds, merge queue groups and batch mutation runners |
| `ci-lab-worker` | 150 | `Never` | Lab workers |
| `arc-runners-oss-batch-priority` | 100 | `Never` | None; it stays defined in `runner-priority-classes.yaml` until no pod names it |
| none | 0 | `PreemptLowerPriority`, the Kubernetes default | Every pod that names no class |

The class is defined as:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: ci-lab-worker
value: 150
preemptionPolicy: Never
globalDefault: false
description: ci-lab workers schedule after pull request and merge queue runners and ahead of batch runners
```

- `preemptionPolicy: Never`: a pending worker never evicts a running pod to get a node; only its
  place in the queue changes. A higher class that preempts, such as `qitp-trading`, can still evict
  a running worker.
- `globalDefault: false`: the class applies only to pods that name it. A pod that names no class
  keeps priority 0.
- The `description` and the manifest's header comment say lab workers rank ahead of batch runners
  at 100. No pod names that class; batch runners name `arc-runners-oss-checks`, so a lab worker
  waits behind them too.

### Where it lives and how it reaches the cluster

The class sits alone in `k8s/manifests/cluster/ci-lab-priority-classes.yaml` in the deployment
repository, apart from the runner classes in `runner-priority-classes.yaml` beside it. It landed in
that repository's pull request 1740 on 15 November 2026. The `cluster-rbac` Argo CD Application
syncs `k8s/manifests/cluster` recursively from that repository's `dev` branch with automated prune
and self heal, so a change to the class reaches the cluster when it merges into `dev`, with no
promotion step.

The deployment repository's `tests/unit/ci_lab/test_priority.py` holds the file to this one class.
It holds the value to 150, between the batch class (100) and the checks class (200) read from the
runner file, and below `qitp-trading` (1000, pinned as a constant). It also holds the class to
`scheduling.k8s.io/v1`, `Never` and `globalDefault: false`. A re-rank changes that test in the same
pull request. The runner classes keep their own test, `tests/unit/arc_runners_oss/test_priority.py`,
which this class leaves untouched.

### Use it from a worker pod

A pod gets the rank only by naming the class in its spec. The lab chart's worker Deployment is to
carry this in its pod template:

```yaml
spec:
  template:
    spec:
      priorityClassName: ci-lab-worker
```

Admission resolves the name to `spec.priority` when the pod is created, so a running pod keeps the
priority it was created with. Admission refuses a pod that names a class the cluster does not hold. No
chart sets `priorityClassName: ci-lab-worker` yet; until the lab chart's worker Deployment does,
its pods run at priority 0.

### Verify

```bash
kubectl get priorityclass ci-lab-worker
```

```text
NAME            VALUE   GLOBAL-DEFAULT   AGE   PREEMPTIONPOLICY
ci-lab-worker   150     false            42s   Never
```

That output is the proof recorded on pull request 1740 after `cluster-rbac` synced it. The full
order, system classes included:

```bash
kubectl get priorityclass --sort-by=.value
```

The class and priority a running worker pod resolved to, with the output expected once the worker
Deployment names the class:

```bash
kubectl get pod <worker pod> -n <namespace> -o jsonpath='{.spec.priorityClassName} {.spec.priority}{"\n"}'
```

```text
ci-lab-worker 150
```

### Re-rank

`value` is immutable on an existing PriorityClass, so an edit of it in place fails the
`cluster-rbac` sync. To move the lab workers:

1. Add a class with a new name and the new value in `ci-lab-priority-classes.yaml`, and update
   `tests/unit/ci_lab/test_priority.py` to the new name, value and order.
2. Point the worker Deployment's `priorityClassName` at the new class; pods pick it up as they are
   recreated.
3. Once no pod names the old class, delete its document from the file. `cluster-rbac` prunes, so
   the next sync after the merge into `dev` removes the class from the cluster; a pod still naming
   it keeps running, but no new pod naming it is admitted.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `kubectl get priorityclass ci-lab-worker` answers `NotFound` | `cluster-rbac` has not synced the commit that adds it, or its sync failed | `kubectl -n argocd get application cluster-rbac -o jsonpath='{.status.sync.status} {.status.sync.revision}{"\n"}'`; the revision must be a `dev` commit whose tree holds the class |
| The worker ReplicaSet reports `FailedCreate` with `no PriorityClass with name ci-lab-worker was found` | The pod template names the class before the cluster holds it | Sync `cluster-rbac` first, as above; the ReplicaSet retries pod creation by itself |
| Worker pods stay Pending while runner pods take the nodes that free up | The intended order when capacity is contested: every CI runner outranks a lab worker | `kubectl get pods -A --field-selector=status.phase=Pending -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.priorityClassName,PRIORITY:.spec.priority --sort-by=.spec.priority` |
| A worker pod shows priority 0 | Its pod template names no class | The jsonpath check above prints an empty class name |
| A running worker pod was evicted | A higher class that preempts took its node | `kubectl get events -n <namespace> --field-selector reason=Preempted` |
| `cluster-rbac` sync fails after the value was edited | `value` is immutable | Re-rank by name as above |

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
| `push` delivery on `/github` | A branch under `refs/heads/` moved; a deleted branch or a tag records nothing | `ci.lab.head.push` | `after` and `before` | The delivery's sender |
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

The lab remote is a git endpoint beside the scheduler. Its post receive hook,
`python -m scripts.ci_lab.remote.post_receive`, turns each pushed `refs/grade/*` head into a
`ci.lab.head.notice` envelope on the same stream, and prints the head's grade url to the pusher:

```text
$ git push -o branch=<branch> lab HEAD:refs/grade/<task>
remote: lab grade: <CI_LAB_URL>/grades/<sha>
```

The url is the scheduler's [`/grades/<sha>`](#grades-of-a-commit) route. The envelope carries the
pushed `ref` and the `branch` push option, which becomes the head's scheduler lane. Its id covers
both, so the same head pushed again with another branch or to another grade ref is a new head
event. The base is the `-o base=<sha>` push option, else the head's first parent. The hook runs
after git writes the refs, so it cannot turn a push away. The remote's pre receive hook refuses refs
outside `refs/grade/*`. A deleted grade ref, or a root commit pushed without `-o base`, prints
`lab remote: <ref> is not graded` and publishes nothing.

GitHub does not hold a head pushed only to the lab remote, so the scheduler and the relay fetch it
from there. Their mirrors fetch a sha from the GitHub repository first and, when that fetch fails,
from the lab remote `AGENTIHOOKS_LAB_REMOTE` names, but only when that URL names the same
repository. Git gets `AGENTIHOOKS_LAB_REMOTE_TOKEN` as the basic password of the user `lab` for
that URL alone, through its environment config, and no store or log keeps it. A grade of a head pushed
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
| `toolchain` | `lockfile` | Blob id of `uv.lock` |
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
| `stats` | `sources` | Path to sha256 of each configuration file, `uv.lock` among them, and each `scripts/ci_mutation/` file |
| `stats` | `options` | The pytest options, in order |
| `stats` | `interpreter` | `sys.version` |
| `stats` | `platform` | `sys.platform` |
| `stats` | `versions` | Distribution name to version |
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
  Its lockfile and pyproject inputs are the `uv.lock` and `pyproject.toml` entries of `sources`.
- A change to `uv.lock` or `pyproject.toml` rekeys every unit kind. A grade is keyed by its head,
  which names both files.
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
   panic grades in the hotfix class.
2. Each head waits in its lane (repository, branch and class) for the quiet window, 30 seconds by
   default; a newer head of the lane restarts it and a hotfix waits for none.
3. Expansion reads the grader's test workflow and input key registry and the head and base trees
   from the git mirror with `git ls-tree` and `git show`, never a checkout, and keys one static
   unit per gate step. A merge group, promotion or backfill head also gets the `unit` job's test
   units, described in [Test units](#test-units), and a promotion or backfill head its mutation
   units, described in [Mutation units](#mutation-units).
4. The verdict store answers what it can. A newer head of the lane drops the older head's pending
   units, carries its leased and finished jobs and cancels its check runs. A push grade of a tree a
   merge group grade passed carries that grade's units for every gate both grades hold: the unit
   suite, since the queue rules require lint, type-gate and semgrep to be skipped. The rest is held for
   the budget.
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
   `completed` on the last, one per gate with units and `required` for the whole grade. A completed
   check run never moves again. The grade state keeps each check run's id, and the scheduler seeds
   its poster with that id and the last status it posted before the next move, so a restarted
   scheduler advances the same check run instead of opening a second one. A grade of a head pushed
   to a `refs/grade/*` ref records the same moves and posts none, leaving its check runs to the
   relay.

Expansion frames a head by its class (`scripts.ci_lab.scheduler.frame`): a pull request or hotfix grade
keeps the lab gates the pull request rules require and names every other required job as a gate without
units, with no test scope and no mutation; a merge group grade does the same for the queue suite; a
promotion or backfill grade keeps every lab gate. A gate without units passes, so a grade turns red only
through what the lab grades.

A gate passes when every unit passed. The `required` conclusion is
`scripts.ci_lab.scheduler.fold`, the rules of the `Gate — Required` step of the test workflow over
the gates as the needs of a run of the grade's class: pull request and hotfix grades fold as
`pull_request`, queue grades as `merge_group`, promotion as `push` and backfill as
`workflow_dispatch`. `tests/fixtures/ci_lab/gate_required.json` records the step's verdict on cases
built from the needs of real runs; one test holds `fold` to every case and another holds the cases
to today's step.

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

### Test units

`scripts.ci_lab.expand_tests.units` turns the dependency manifest into test units of kind `test`:
one per `expand_tests.batches` batch, with batches cut at `BATCH_SECONDS` (60) from the head's
`.test_durations`. Each unit's key is `digests.test_key` over its tests' fingerprints on the head
tree, the blob ids of the files its tests opened outside the manifest (their records' `outside`
list), the pytest invocation and the toolchain. The expander reads every head file in one
`git cat-file --batch` for the fingerprints.

| Head | Unit job |
|---|---|
| Pull request, hotfix | No test units; the manifest is not read |
| Merge group | The batches that hold a test the group's entries invalidate, or every batch when `interaction.decide` returns `FULL` |
| Promotion, backfill | Every batch; the verdict store is not consulted for the choice |

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
A request GitHub never answered, its token mint included, or answered with a server error raises its
subclass `manifests.FetchUnanswered`, and the scheduler puts the head back in its lane with the quiet
window restarted, so the first pass after that window fetches again; any other refusal drops the head
with its reason. The dev push artifact is kept 7 days, so a commit older
than that is answered only from the store. A head that needs test units and finds none fails with
`no dependency manifest at or before <base>` and is dropped with that reason, so a merge group
never folds green on an empty unit job.

A batch verdict lists the files the batch opened outside its tests' manifest records. When the
scheduler stores the verdict of a graded job whose uploaded report carries a non empty `outside`
list, whatever its graded outcome, `note_outside` writes it as a note of the repository at
`lab/manifests/<owner>--<name>/outside/<digest>.json`, holding the unit's tests and the sorted list,
where `<digest>` is `digests.digest` of that note. The object store is write once, so the packed
`<commit>.json` manifests are never rewritten, and the same tests and list write one note. A
`StoredManifests` read that finds or fetches a packed manifest merges each note of the repository
into the `outside` list of each test record the note names, sorted and without duplicates, and skips
a test the manifest lacks; the next expansion keys those tests on the measured tree and the batch on the
noted files' blob ids. `scripts.dep_manifest.invalidated`, pull request test selection in
`scripts.ci_select.recorded_tests` and mutation test selection in
`scripts.ci_mutation.plan.covering_tests` and `line_covering_tests` count a change to a file in a
record's `outside` list as a change to that test, as they do for a file in its `files` list. The
list is per batch, so every test of the batch takes it, and notes are per repository rather than
per manifest commit.

For a merge group head the expander builds `interaction.Group` from the mirror: the group's
changes against the base, one entry per first parent commit between base and head with that
commit's own changes, and the `pyproject.toml` flag from `ci_select.pyproject_reaches_tests`. The
cached test units are the tests of every batch the verdict store answers, a negative verdict
included, and the entry limit is `interaction.entry_limit` of the grader's `delivery.yaml` lab
block. The clauses are in the
[Lab merge group interaction rule](delivery-tiers.md#lab-merge-group-interaction-rule).

Since a push grade folds only the static gates,
[Post merge testing with automatic revert](../research/post-merge-testing.md) measures the red
`dev` rate from the Tests workflow's runs on GitHub, not from lab push grades.

### Mutation units

A promotion or backfill head gets two more jobs, built by `scripts.ci_lab.expand_mutation` in a
temporary worktree of the head cut from the mirror store and removed after expansion:

| Job | Units |
|---|---|
| `mutation` | One `mutant` unit per function the head changes against its base, naming every mutant of that function |
| `mutation-stats` | One `stats` unit per test file present at the head that holds a test running such a function, with the worker's stats options |

The changed lines are `scripts.ci_mutation.scope.changes_since` of the base and head, the lines the
pull request mutation gate grades. `expand_mutation.covering` maps each function to the fingerprint
of every dependency manifest test that ran it or a function nested in it, and those fingerprints
enter the mutant key, so a changed covering test rekeys the unit. The stats units warm the stats
cache the worker's mutant runner reads before it runs the mutants. The manifest only names the tests
of the base, while the mutant runner runs every test module of the head, so
`expand_tests.unlisted_test_hashes` hashes each test module of the head that holds no manifest test,
with every conftest pytest loads for it and every test tree module either imports, and those hashes
enter every mutant key too: a head that adds or edits such a module rekeys its units.

| Head | Mutation jobs |
|---|---|
| Pull request, hotfix, merge group | None: their frames and `Gate — Required` rules hold no mutation job, and the test workflow runs none on `merge_group` |
| Promotion, backfill | Both, unless the head changes no Python line under `hooks/` or `scripts/` |

A mutant unit passes only when every mutant is killed, so a surviving mutant fails the `mutation`
gate and the promotion grade folds red. A changed function no recorded test ran still gets its unit;
the runner finds no test reaching it, grades its mutants `no tests` and fails the unit.

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
| `static` | head, base and grader, read only, shared by the pod's units | the gate the unit names from the grader tree's workflow and input registry, the venv of the head's `uv.lock` digest and `inputs.base` set to the base revision |
| `test` | head, read only | the batch, the venv of the head's `uv.lock` digest, `CI_LAB_MANIFEST`, `CI_LAB_FLAKES` and one pytest worker per slot |
| `stats` | a private copy of the head with every file under the `[tool.mutmut]` source paths behind its trampolines | the tree as root and `CI_LAB_MUTATION_CACHE` |
| `mutant` | the same private trampolined copy | `CI_LAB_KILLERS`, `CI_LAB_MUTATION_BUDGET`, `CI_LAB_MUTATION_CAP` and the tests that the stats records of every test module in the tree show reaching the mutant's function, with their seconds |

The mutation runner runs in the sandbox as `python -m scripts.ci_lab.run_mutation REQUEST OUTPUT`
from the private tree. A mutant unit passes only when every mutant is killed; a runner that exits
non zero fails its unit. Each adapter uploads a JSON report with the unit's `seconds` (and `flaky`
for a test unit), which the scheduler stores with the verdict, and releases every tree it cut,
also when the executor raises. SIGTERM stops leasing and lets running units drain within the
termination grace.

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

The constants are `PREFIX` (`agentihooks-ci:`), `INDEX` (`agentihooks-ci:verdict:`) and
`DISABLED`. Other lab keys share the prefix, such as the daily CI budget at
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

Two backends implement the `Objects` protocol of `write_once`, `read`, `keys` and `upload_url`:

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
| `store` | `store.Store` | Reads each cached verdict with `get` and stops reuse with `disable` |
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

When any unit differs, the audit:

1. Disables every differing kind with `Store.disable`, all of them before it files any task. The
   whole kind stops hitting, its agreeing units included; other kinds keep hitting.
2. Files one task per kind, in sorted kind order, with `tasks.add_task(slug, fields, "swarm")`.
   `"swarm"` is `audit.SENDER`.
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
`CILabVerdictMismatch` is not raised. Every differing kind is already disabled, so none is reused,
but the refused kind and every later kind in sorted order have no task. The audit keeps no copy of
its mismatches, so on this path the differing units and their runs are lost with the exception.

### Enable reuse again

The audit never enables a kind. Reuse of a kind comes back through a pull request that fixes the
input its key misses, as the task says. No code enables a kind when that pull request merges, and
no service, command or workflow calls `Store.enable` yet. The call is:

```python
verdicts.enable("test")
```

It removes the kind from `agentihooks-ci:disabled`. A Redis flush empties that set too, which
enables every kind again; see [Disable and enable a kind](#disable-and-enable-a-kind).

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
| A unit's executor raises | The unit stops heartbeating and its lease is never completed, so it reaches the scheduler as an expired lease. The worker prints `ci-lab worker: unit <job> crashed: <repr of the error>` to standard error on its next lease cycle |

A worker that completes a lease with `crash` therefore runs once more under the default
`lost: 1`, and a second `crash` is final and red. `tests/ci_lab/test_retry.py` covers the decision
table, and the `lab-scheduler-test-units` feature tests cover the scheduler's retry path.

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
- Its own session. A timeout sends `SIGKILL` to the whole process group, and the outcome's code is
  `None`.

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
marker holds the digest. Otherwise it builds `root/<digest>` once, under a lock per digest, by
running uv in a `Sandbox` from `build` (`venvs.Build`: ports, whitelist and time limit). The
executors pass `Pod.build`, the pod's ports, whitelist and unit time limit.

| Part | Value |
|---|---|
| Command | the worker's `uv` with `sync --frozen --no-install-project --all-extras --python <worker python> --project <tree>` |
| Runs from | the head tree, with network, under `env -i` and no bubblewrap |
| Environment | `build.whitelist`, `PATH` (the whitelist's, else `/bin:/usr/bin`), `UV_PROJECT_ENVIRONMENT=root/<digest>` and the sandbox's private folders |
| Run folder | under `root/builds`, removed after a build that ends with code 0 |

uv and every build hook it starts get no other worker variable in their environment. A build
that runs past the unit time limit or ends with a
nonzero code raises `CannotRun` naming its kept log and writes no marker, so the next call
builds again. A worker without `uv` on its `PATH` raises
`uv is not installed, so a venv cannot be built`. The build is isolated by environment only:
it runs as the worker's user with no PID namespace, so it can still read and write the host
filesystem, `/proc` included.

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

The pull request tier proves the sandbox under real bubblewrap. The writer lock workflow's
`lab-sandbox` job runs on every pull request that changes `scripts/ci_lab/sandbox.py`,
`scripts/ci_lab/run_static.py`, `scripts/ci_lab/run_tests.py`, `tests/ci_lab/test_sandbox.py`,
`tests/ci_lab/test_run_static.py`, `tests/ci_lab/test_run_tests.py` or
`.github/workflows/writer-lock.yml`, and on every manual dispatch of the writer lock workflow.
It runs on a GitHub hosted `ubuntu-latest` runner, installs bubblewrap when the runner lacks it,
and sets `kernel.apparmor_restrict_unprivileged_userns=0`. It then runs those three test files
and fails when any of their tests fails or skips.

The Tests workflow calls the writer lock workflow from its `writer-lock` job, which
`Gate — Required` needs.
These tests prove the read only trees:

| Test | Proves |
|---|---|
| `test_run_static.py::test_the_step_runs_with_the_grader_and_base_trees_bound_read_only` | The exact bubblewrap arguments of a gate step: the root bind, the grader bind, the base bind, `--unshare-net`, `--die-with-parent` |
| `test_run_static.py::test_a_step_cannot_write_into_the_grader_or_base_tree` | Under real bubblewrap, a step's writes through `$RUNNER_TEMP/grader` and `$RUNNER_TEMP/base` are refused, both trees stay empty and its write into `$GITHUB_WORKSPACE` lands |
| `test_sandbox.py::test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree` | With and without network, `chmod`, a rewrite and a new file under each tree fail with `EROFS` |

In `feature-map.json`, `lab-static-read-only-trees` maps the bubblewrap arguments test of
`test_run_static.py`. `lab-sandbox-read-only-trees` maps the `test_sandbox.py` tests that check
the `--ro-bind` arguments and the `CannotRun` refusal without `bwrap`. The two write refusal tests
above carry no feature entry.

### Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `CannotRun: bwrap is not installed, so a unit with read only trees cannot run` | The worker has no `bwrap` on `PATH`. Every static gate run needs it | `command -v bwrap` on the worker; install the `bubblewrap` package in the worker image |
| The unit log reads `bwrap: No permissions to create a new namespace` and the gate is red | The host blocks unprivileged user namespaces, as Ubuntu's AppArmor restriction does | `sysctl -n kernel.apparmor_restrict_unprivileged_userns` prints `1`; `bwrap --dev-bind / / --unshare-net true` fails |
| A gate step that passes in Actions fails in the lab with `Read-only file system` | The step writes under `$RUNNER_TEMP/grader`, `$RUNNER_TEMP/base` or a path inside either tree, such as a tool cache | Find the path in the unit's `log.txt`; write outputs to `$RUNNER_TEMP`, the head tree or the private `HOME` instead |
| A test unit wrote into the grader tree | Test units get no read only trees | See [Which units get read only trees](#which-units-get-read-only-trees) |
| `CannotRun: no free port range` | Every range of the allocator is in use | The allocator holds `(last - first + 1) // width` ranges; run fewer units at once or widen the span |
| The `lab-sandbox` job fails with `A lab sandbox test skipped, so bubblewrap did not isolate it on this runner.` | A real bubblewrap test skipped because the runner could not create namespaces | The skip reason in the job's `-rs` summary, such as `bubblewrap cannot create namespaces on this runner` |
