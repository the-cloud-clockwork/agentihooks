---
title: CI lab service contract
parent: Reference
nav_order: 90
---

# CI lab service contract
{: .no_toc }

The CI lab runs as two services, the scheduler and the worker. This page lists what each one
exposes and reads: its entry point, environment, ports and metric names. Every name lives in
`scripts.ci_lab.contract`; the services and the platform tests are to import these constants by
name, so a rename fails an import instead of dropping a series. The worker's PriorityClass is a
cluster object outside that module; [Worker scheduling priority](#worker-scheduling-priority)
covers it.

## Entry points

| Command | Module form | What it does today |
|---|---|---|
| `agentihooks ci-lab-scheduler --self-test` | `python -m scripts.ci_lab scheduler --self-test` | Checks the scheduler contract offline and prints it as JSON |
| `agentihooks ci-lab-worker --self-test` | `python -m scripts.ci_lab worker --self-test` | Refuses any GitHub App variable, checks the worker contract offline and prints it as JSON |

The self tests make no network, Redis, object store or GitHub calls. They exit 0 when the
contract holds and 1 with the reason on standard error when it does not. A self test fails when
a metric name is not a valid Prometheus name starting with `ci_lab_`, a port repeats or falls
outside 1 to 65535, or the worker environment grants `GITHUB_APP_`. `-h` prints usage; any
other start without `--self-test` exits 2, because the service loops come with the scheduler and
worker tasks.

## Environment

| Name | Scheduler | Worker | Meaning |
|---|---|---|---|
| `AGENTIHOOKS_BUS*` | yes | yes | Event bus connection settings |
| `CI_LAB_MINIO_*` | yes | yes | Object store endpoint, bucket and credentials, passed by reference |
| `GITHUB_APP_*` | yes | never | GitHub App identity used to post check runs |
| `CI_LAB_REDIS_URL` | yes | no | Queue, lease and verdict index store |
| `CI_LAB_RUNTIME_CONFIG` | yes | no | Path of the mounted runtime config; `python -m scripts.ci_lab runtime <path>` validates it |
| `CI_LAB_SCHEDULER_URL` | no | yes | Base address of the scheduler lease API |
| `CI_LAB_SLOTS` | no | yes | Number of units the worker runs at once |
| `CI_LAB_GIT_MIRROR` | no | yes | Path of the shared git mirror the worker cuts worktrees from |
| `UV_CACHE_DIR` | no | yes | Shared uv cache for virtual environments |

A worker started with any `GITHUB_APP_*` variable in its environment refuses to start. It exits
1 and names each variable it found. Only the scheduler holds the App key.

## Ports

| Port | Service | Serves |
|---|---|---|
| `8800` | scheduler | The lease API workers call, and `/capacity` |
| `9108` | scheduler and worker | Prometheus metrics |

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

`METRICS` maps each name to its label names. Environment names have constants too:
`BUS_PREFIX`, `MINIO_PREFIX`, `GITHUB_APP_PREFIX`, `REDIS_URL`, `RUNTIME_CONFIG`,
`SCHEDULER_URL`, `SLOTS`, `GIT_MIRROR` and `UV_CACHE_DIR`, with `SCHEDULER_PORT` and
`METRICS_PORT` for the ports.

## Worker scheduling priority

The PriorityClass `ci-lab-worker` ranks lab worker pods below every CI runner
(`arc-runners-oss-checks`, 200) and above the unused `arc-runners-oss-batch-priority` class (100).
When the nodes are full, new pods wait in Pending and the scheduler places them in priority order as
capacity frees, so a pending lab worker never takes a freed node from a pending pull request check
or merge queue runner. Priority orders only pods that wait: neither class preempts, so a running
worker keeps its node until it ends, and a check can still wait for a node a worker took first. A
pod gets this rank only when its spec names the class;
[Use it from a worker pod](#use-it-from-a-worker-pod) shows how.

| PriorityClass | Value | `preemptionPolicy` | Pods that name it |
|---|---|---|---|
| `qitp-trading` | 1000 | can preempt lower priority pods | Trading workers |
| `arc-runners-oss-checks` | 200 | `Never` | Every CI runner: pull request checks, image builds, merge queue groups and batch mutation runners |
| `ci-lab-worker` | 150 | `Never` | Lab workers |
| `arc-runners-oss-batch-priority` | 100 | `Never` | None in the runner charts; the runner chart keeps it defined until no pod names it |
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
- The `description` text, which the cluster holds as written, and the manifest's header comment
  call the 100 class the batch runners' class. Batch mutation runners name
  `arc-runners-oss-checks` instead, so a lab worker waits behind them too.

### Where it lives and how it reaches the cluster

The class sits alone in `k8s/manifests/cluster/ci-lab-priority-classes.yaml` in the deployment
repository, apart from the runner classes in `runner-priority-classes.yaml` beside it. It landed in
that repository's pull request 1740 on 15 November 2026. The `cluster-rbac` Argo CD Application
syncs `k8s/manifests/cluster` recursively from that repository's `dev` branch with automated prune
and self heal, so a change to the class reaches the cluster when it merges into `dev`, with no
promotion step.

The deployment repository's `tests/unit/ci_lab/test_priority.py` holds the file to this one class,
the value to 150 between the batch class at 100 and the checks class at 200, both read from the
runner file, and below trading, which the test pins as the constant 1000. It also holds the class to
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
| `test` | `tests` | Test id to its fingerprint from `tests/dep_manifest.py` |
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
| `mutant` | `definition` | Digest of the function's whole definition text, from `tests.dep_manifest.unit_hashes` |
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
