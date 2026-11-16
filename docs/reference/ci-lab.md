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
| `python -m scripts.ci_lab scheduler [--interval S] [--passes N]` | same | Runs the scheduler service: the lease API on port 8800 and one grading pass every interval (2 seconds), forever unless `--passes` is set |
| `agentihooks ci-lab-worker --self-test` | `python -m scripts.ci_lab worker --self-test` | Refuses any GitHub App variable, checks the worker contract offline and prints it as JSON |

The self tests make no network, Redis, object store or GitHub calls. They exit 0 when the
contract holds and 1 with the reason on standard error when it does not. A self test fails when
a metric name is not a valid Prometheus name starting with `ci_lab_`, a port repeats or falls
outside 1 to 65535, or the worker environment grants `GITHUB_APP_`. `-h` prints usage. The
scheduler without `--self-test` runs its service (see [Scheduler loop](#scheduler-loop)); any other
worker start without `--self-test` exits 2 until the worker loop lands.

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
| `CI_LAB_GIT_MIRROR` | yes | yes | Path of the shared git mirror the worker cuts worktrees from and the scheduler borrows objects from |
| `CI_LAB_MINIO_ENDPOINT`, `CI_LAB_MINIO_BUCKET`, `CI_LAB_MINIO_ACCESS_KEY`, `CI_LAB_MINIO_SECRET_KEY` | yes | no | The object store names the scheduler reads |
| `CI_LAB_MIRROR_STORE` | yes | no | Folder of the scheduler's private git stores, one per repository, that borrow the mirror's objects |
| `CI_LAB_REPOSITORY` | yes | no | Repository URL whose dev runner trust list admits head actors |
| `CI_LAB_IMAGE` | yes | no | Worker image name keyed into static unit keys; empty when unset |
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

## Scheduler loop

`scripts.ci_lab.scheduler.Scheduler.tick(now_ms)` runs one grading pass:

1. Intake reads new heads from the `ci.lab.head.>` lab heads stream and admits only actors on the
   runner trust list of the repository's dev branch. A head on a panic fix branch grades in the
   hotfix class.
2. Each head waits in its lane (repository, branch and class) for the quiet window, 30 seconds by
   default; a newer head of the lane restarts it and a hotfix waits for none.
3. Expansion reads the grader's test workflow and input key registry and the head and base trees
   from the git mirror with `git ls-tree` and `git show`, never a checkout, and keys one static
   unit per gate step.
4. The verdict store answers what it can. A newer head of the lane drops the older head's pending
   units, carries its leased and finished jobs and cancels its check runs. A push grade of a tree
   a merge group grade completed carries that grade's gates. The rest is enqueued.
5. Expired leases requeue while `lab.retry.lost` allows (once by default); a second loss is `lost`.
6. Finished jobs are collected: a graded outcome with an uploaded report becomes a verdict in the
   store; `lost`, `crash` and `cannot_run` stay out of it and make their grade red.
7. Check runs move `queued` on receipt, `in_progress` on the first unit a worker starts and
   `completed` on the last, one per gate with units and `required` for the whole grade. A completed
   check run never moves again.

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
`agentihooks-ci:lane:<repository>|<lane>|<class>` and a completed merge group grade at
`agentihooks-ci:tree:<repository>|<tree>`. A rerequested check run grades the head again under
`<run>-<now_ms>`, bypassing negative verdicts in the store.

Known limits: a lab head event carries no branch yet, so a head's lane is its own sha and a newer
head supersedes nothing until intake records the branch; expansion keys static gates only; a
rerequested grade reuses the finished jobs of its own head.

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
| `lab/grades/<run>/grade.json` | `evidence.record`, write once; no caller in this tree yet | One grade run's evidence: its inputs and the units it used |
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
