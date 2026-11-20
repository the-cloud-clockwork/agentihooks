---
title: CI cost and lead time metrics
parent: Reference
nav_order: 92
---

# CI cost and lead time metrics

`python -m scripts.ci_metrics` measures one hour of agentihooks CI and writes the
result to the ClickHouse tables `swarm.ci_cost` and `swarm.ci_lead_time` through
the metrics outbox. The [hourly CronJob](#the-hourly-cronjob) in the
`agentihooks-swarm` namespace runs it from the swarm image, so every later CI
task reads its before and after numbers from the same rows.

## Running it

```bash
python -m scripts.github_app_cli exec python -m scripts.ci_metrics \
  --repo the-cloud-clockwork/agentihooks \
  --prometheus http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090 \
  [--lag-hours 2] [--until 2026-11-04T15:00:00Z]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--repo` | required | Repository whose workflow runs are measured |
| `--prometheus` | required | Prometheus base URL for the pending runner average, with no trailing slash |
| `--lag-hours` | 2 | Measure the hour that ended this many hours before the current hour, so its runs have finished |
| `--until` | none | Measure the hour that ends at this UTC time instead |

The run needs `AGENTIHOOKS_METRICS_URL`, `AGENTIHOOKS_METRICS_USER` and
`AGENTIHOOKS_METRICS_PASSWORD` (the swarm metrics secret) and a GitHub token for
`gh`. The CronJob takes the token from the GitHub App credentials through
`scripts.github_app_cli exec`, which mints one installation token for the whole
run. A run refuses without the sink settings and fails when the sink takes fewer
rows than the run built. It prints the window and the row counts on one JSON
line and the headline numbers on a second.

Each run ships through a fresh outbox that is removed when the run ends. A flush
gets five seconds and stops at the first statement the sink refuses; while rows
still wait, the run flushes the same outbox again, at most three flushes in all,
so the rows one flush left behind ship on the next. A sink that still refuses or
answers slowly on the third flush fails the run, and the rows it did not take
are missing until the hour is measured again:

```bash
kubectl -n agentihooks-swarm create job --from=cronjob/agentihooks-swarm-ci-metrics ci-metrics-rerun
```

reruns the hour the lag points at, so it recovers a failed run only before the
next hour starts. An older hour needs a run of the module with `--until` set to
the end of that hour. Rows of a window measured twice replace each other.

## The hourly CronJob

The CronJob `agentihooks-swarm-ci-metrics` in the `agentihooks-swarm` namespace
runs the module once an hour. Its manifest is
`k8s/manifests/agentihooks-swarm/ci-metrics-cronjob.yaml` in the deployment
repository, and `tests/unit/agentihooks_swarm/test_ci_metrics_cronjob.py`
there pins its schedule, image, command, Secret references and security
settings.

### Schedule and limits

| Setting | Value | Effect |
|---------|-------|--------|
| `schedule`, `timeZone` | `17 * * * *`, `Etc/UTC` | One run at minute 17 of every hour, UTC |
| `concurrencyPolicy` | `Forbid` | A run that falls due while the previous run is still active is skipped |
| `startingDeadlineSeconds` | 1800 | A run that cannot start within 30 minutes of its time is skipped |
| `backoffLimit`, `restartPolicy` | 0, `Never` | A failed run is not retried |
| `activeDeadlineSeconds` | 1500 | A run still going after 25 minutes is stopped and fails with `DeadlineExceeded` |
| `ttlSecondsAfterFinished` | 86400 | A finished Job and its pod are deleted a day after the run ends at the latest; the history limits below usually remove them sooner |
| `successfulJobsHistoryLimit`, `failedJobsHistoryLimit` | 3, 3 | Only the last three succeeded and the last three failed Jobs are kept, so at hourly runs a succeeded run's log is gone about three hours later |

The CronJob passes neither `--lag-hours` nor `--until`, so every run measures
the hour that ended two hours before the hour it starts in. The run at 10:17 UTC
measures 07:00 to 08:00 UTC, and that window's `swarm.ci_cost` rows are timed at
07:00. Each hour is measured by exactly one scheduled run; a skipped or failed
run leaves its hour without rows until it is measured again as described under
[Running it](#running-it).

### Image and command

The container runs `ghcr.io/the-cloud-clockwork/agentihooks-swarm:release` with
`imagePullPolicy: Always`. The Swarm image workflow moves the `release` tag to
each new release that is the newest version tag, and every run pulls the image
the tag points at, so a release reaches the next run with no manifest change. A
run needs a release that carries `scripts.ci_metrics`; an older image fails
with `No module named scripts.ci_metrics`.

The container command is:

```bash
python -m scripts.github_app_cli exec python -m scripts.ci_metrics \
  --repo the-cloud-clockwork/agentihooks \
  --prometheus http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
```

`scripts.github_app_cli exec` mints one GitHub App installation token from
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY`, puts
it in `GH_TOKEN` and `GITHUB_TOKEN`, and replaces itself with the module. Every
GitHub call of the run is a `gh api` read with that token: the image's `gh`
wrapper calls the GitHub CLI directly when `HIVE_GITHUB_AUTH_MODE` is empty and
`${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/github-token` is missing or empty, and
the pod sets neither. An installation token is valid for one hour, and the 25
minute deadline ends every run before it expires. No wrapper read reserve check
runs on the module's reads: `exec` checks only a command it runs as `gh`, and
here it runs `python`. The module's budget check is the only one that refuses a
run when the shared limit is low.

### Settings and Secrets

The manifest names every credential by reference; no value is in it.

| Variable | Source | Read by |
|----------|--------|---------|
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` | The keys of the same names in the Secret `agentihooks-swarm-hub` | The wrapper, to mint the token |
| `AGENTIHOOKS_METRICS_URL`, `AGENTIHOOKS_METRICS_USER`, `AGENTIHOOKS_METRICS_PASSWORD` | Every key of the Secret `agentihooks-swarm-metrics`, through `envFrom` | The metrics outbox |
| `HOME` | `/home/swarm` | The image's `swarm` user |

Both Secrets come from ExternalSecrets in the deployment repository's
`k8s/manifests/agentihooks-swarm` folder, synced from OpenBao:
`agentihooks-swarm-hub` refreshes every 5 minutes and `agentihooks-swarm-metrics`
every hour. A changed value reaches the first run that starts after the
refresh. The module refuses to start when `AGENTIHOOKS_METRICS_URL` or
`AGENTIHOOKS_METRICS_USER` is empty; the password may be empty.

### Placement and security

- `automountServiceAccountToken: false`: the pod holds no Kubernetes API token.
- The pod runs as user and group 10001, the image's `swarm` user, with
  `runAsNonRoot` and the `RuntimeDefault` seccomp profile. The container drops
  every capability and disallows privilege escalation.
- Node affinity keeps the pod off the `ec2-canary`, `oci-ci`, `oci-research`
  and `oci-swarm` node pools, matched by the cluster's node pool label.
- The container requests 50m CPU and 256 MiB of memory and is limited to
  1 GiB of memory; a run above the limit is killed with `OOMKilled`.

### Deployment

The `agentihooks-swarm` ArgoCD Application syncs the deployment repository's
`k8s/manifests/agentihooks-swarm` folder from `main`, with automated prune and
self heal. A change to the CronJob is a pull request into that repository's
`dev`, and it reaches the cluster when the repository promotes `dev` to `main`.
Self heal reverts a hand edit of the live CronJob, so `kubectl edit` or
`kubectl patch` does not change the schedule; change the manifest and its test
together instead.

### Checking runs

```bash
kubectl -n agentihooks-swarm get cronjob agentihooks-swarm-ci-metrics
kubectl -n agentihooks-swarm get pods -l app.kubernetes.io/name=agentihooks-swarm-ci-metrics
kubectl -n agentihooks-swarm logs <pod name>
```

`LAST SCHEDULE` in the first command shows when the last run started. A pod in
`Completed` succeeded; `Error` or `OOMKilled` failed.
The log of a run that shipped its rows ends with the two lines described under
[Running it](#running-it):

```text
{"since": "2026-11-04T10:00:00+00:00", "until": "2026-11-04T11:00:00+00:00", "rows": 9, "shipped": 9}
{"merged": 2, "runs": 93, "read_runs": 93, "runner_minutes": 2531.3, "per_merged": 1265.6, "cancelled_share": 0.086, "cancelled_minutes": 274.6, "cancelled_minutes_share": 0.108, "tiers": {"branch push": {"runs": 35, "read_runs": 35, "runner_minutes": 505.4, "per_merged": 252.7, "cancelled_share": 0.114, "cancelled_minutes": 41.9, "cancelled_minutes_share": 0.083}, "promotion": {"runs": 14, "read_runs": 14, "runner_minutes": 317.3, "per_merged": 158.6, "cancelled_share": 0.0, "cancelled_minutes": 0.0, "cancelled_minutes_share": 0.0}, "pull request": {"runs": 24, "read_runs": 24, "runner_minutes": 1494.2, "per_merged": 747.1, "cancelled_share": 0.125, "cancelled_minutes": 125.5, "cancelled_minutes_share": 0.084}, "queue": {"runs": 2, "read_runs": 2, "runner_minutes": 211.4, "per_merged": 105.7, "cancelled_share": 0.5, "cancelled_minutes": 107.2, "cancelled_minutes_share": 0.507}, "workflow_dispatch": {"runs": 4, "read_runs": 4, "runner_minutes": 2.7, "per_merged": 1.3, "cancelled_share": 0.0, "cancelled_minutes": 0.0, "cancelled_minutes_share": 0.0}, "workflow_run": {"runs": 14, "read_runs": 14, "runner_minutes": 0.2, "per_merged": 0.1, "cancelled_share": 0.0, "cancelled_minutes": 0.0, "cancelled_minutes_share": 0.0}}, "lead_time": {"pull_requests": 2, "p50_minutes": 492.7, "p90_minutes": 842.4}, "heads": {"p50": 7.0, "p90": 11.0, "max": 12.0}, "pending_runners": {}}
```

The lines above are one hour of the 4 November fixture, 10:00 to 11:00 UTC,
computed with no Prometheus answer, so `pending_runners` is empty; a CronJob
run lists each ARC runner pool there.

The first line names the window, the rows built and the rows the sink took;
the two counts are equal on a green run. The second holds the window's headline
numbers.

To confirm the rows through the Grafana ClickHouse datasource, list the window
rows of the last day; one row per measured hour, so a missing hour is a run
that did not ship:

```sql
SELECT ts, runs, read_runs, estimated_minutes, merged, per_merged, pending_runners
FROM swarm.ci_cost FINAL
WHERE scope = 'window' AND ts > now() - INTERVAL 1 DAY
ORDER BY ts;

SELECT count(), max(ts) FROM swarm.ci_lead_time FINAL
WHERE ts > now() - INTERVAL 1 DAY;
```

### Troubleshooting

| Log line or state | Cause | What to do |
|-------------------|-------|------------|
| No Job at the scheduled minute, `get cronjob` finds nothing | The manifest is not on the deployment repository's `main` yet, or the ArgoCD sync is stuck | Check the Application's sync state with `kubectl -n argocd get application agentihooks-swarm` |
| `No module named scripts.ci_metrics` | The `release` image predates the module | The next run after a release that carries it succeeds |
| `GitHub App cannot supply scoped contents write credentials` | The wrapper could not mint the token: an App key is missing from `agentihooks-swarm-hub` or GitHub refused the App | List the Secret's key names (never its values) and check the ExternalSecret's sync status |
| `ci metrics refused: the metrics sink settings are not set` | `AGENTIHOOKS_METRICS_URL` or `AGENTIHOOKS_METRICS_USER` is empty | Check the `agentihooks-swarm-metrics` ExternalSecret's sync status |
| `<n> job reads would leave fewer than 1000 of <m> API calls; refusing to start` | The shared GitHub limit is low | Rerun after the reset, before the next hour starts |
| `ci metrics refused: 10 pages of closed pull requests did not reach <time>` | More than a thousand closed pull requests into dev were updated after the window began | An hour this far back cannot be measured with `--until`; the page cap is in code |
| `ci metrics failed: shipped <n> of <m> rows to the metrics sink` | Rows still waited after three flushes: ClickHouse refused a statement, a request ran past its 3 seconds, or each 5 second flush ran out | Check the sink, then rerun before the next hour starts |
| `RuntimeError: <start>..<end> listed <n> runs, the most GitHub returns` | The hour listed 1000 runs or more, the most one GitHub run listing returns | The hour cannot be measured by this module; raise the case as a follow up |
| A `CalledProcessError` or `TimeoutExpired` traceback from `gh` | A GitHub read failed: listed reads give up after three attempts, the closed pull request pages and the rate limit read on the first error | Rerun before the next hour starts |
| A `URLError` or `TimeoutError` traceback from the Prometheus query | Prometheus refused the query or did not answer within 60 seconds | Rerun before the next hour starts |
| `DeadlineExceeded` | The run took longer than 25 minutes | Read the log for the slow step; the hour has no rows |
| `OOMKilled` | The run used more than 1 GiB | Raise the limit in the manifest by pull request |

## What one run reads

Every agent shares one GitHub API limit, so one run reads one hour window:

1. The workflow runs created in the window, listed one hour at a time
   (`scripts.ci_minutes.read_runs`).
2. The pull requests merged into dev in the window, from the closed pull
   requests listed newest update first, stopping at the first page that reaches
   an update older than the window. A run whose ten page cap ends first refuses
   rather than count fewer merges.
3. A budget check: one job read per run and one branch read per merged pull
   request are refused when they would leave fewer calls than the reserve
   `scripts.ci_minutes` keeps for the other users of the limit.
4. The jobs of each run, with their steps (`scripts.ci_minutes.read_jobs`). The
   measures use job times; no row stores step times.
5. Every run on each merged pull request's branch, for its lead time.
6. `avg_over_time(sum by (name) (gha_controller_pending_ephemeral_runners)[1h:1m])`
   from Prometheus at the end of the window.

A typical hour lists about 115 runs, so a run makes one or two hundred GitHub
calls.

## What it computes

The definitions are those of the 4 November CI at agent scale report.

| Measure | Definition |
|---------|------------|
| Runner minutes | Summed run time of every job that was not skipped, each job counted once by its id, re-run attempts included |
| Estimated minutes | A workflow's runs whose jobs were not read count at the minutes per run of its read runs |
| Tier | `pull request` (pull request and pull request target events), `queue` (merge group), `promotion` (push to dev), `branch push` (push to any other branch), or the event name |
| Per merged pull request | Estimated minutes over the pull requests merged into dev in the window |
| Cancelled share | Cancelled runs over all runs; cancelled minutes are the runner minutes of cancelled runs. Rows store the counts and minutes, and the share is their ratio |
| Lead time | From the first CI run on the pull request's branch to its merge, over the branch's whole run history. The report read only its 48 hour pull and dropped a branch whose first run was the pull's first |
| Heads | Distinct head commits among the branch's runs created up to the merge |
| Pending runners | Average pending ephemeral runners per ARC runner pool over the hour |

## Rows

Both tables carry the metrics outbox base columns. `ledger` holds the
repository, and event ids are stable, so a rerun of a window replaces its rows.

`swarm.ci_cost` holds one row per window (`scope` `window`, `name` `all`), one
per tier (`scope` `tier`) and one per runner pool (`scope` `pool`), timed at the
start of the window.

| Column | Meaning |
|--------|---------|
| `runs`, `read_runs` | Runs listed, and runs whose jobs were read |
| `runner_minutes`, `estimated_minutes` | Minutes of the read runs, and the estimate over every listed run |
| `cancelled_runs`, `cancelled_minutes` | Cancelled runs and their runner minutes |
| `merged`, `per_merged` | Pull requests merged into dev in the window, and estimated minutes per merged pull request |
| `pending_runners` | Pool average; the window row holds the sum over the pools |

`swarm.ci_lead_time` holds one row per merged pull request whose first branch
run is known, timed at its merge: `pr`, `branch`, `first_run_ms`, `merged_ms`,
`minutes`, `heads` and `runs`.

Runner minutes per merged pull request over two days, and the lead time
percentiles:

```sql
SELECT sum(estimated_minutes) / sum(merged) FROM swarm.ci_cost FINAL
WHERE scope = 'window' AND ts > now() - INTERVAL 2 DAY;

SELECT quantile(0.5)(minutes), quantile(0.9)(minutes) FROM swarm.ci_lead_time FINAL
WHERE ts > now() - INTERVAL 2 DAY;
```

## The 4 November baseline

`tests/ci_metrics/fixtures/agent-scale-2026-11-04.json.gz` holds the report's
raw pull: 5,532 runs created from 2026-11-02T15:35Z to 2026-11-04T15:36Z, the
64,040 distinct jobs pulled for 4,432 of them, and the 300 pull requests merged
up to the end of the pull. Over it the tool gives:

| Measure | Value |
|---------|-------|
| First CI run on the branch to merged | p50 54.0 min, p90 360.5 min, 167 pull requests |
| Heads per merged branch | p50 3, p90 9, max 27 |
| Runner minutes per merged pull request | 716.6 (121,113 estimated minutes over 169 merged) |
| Pull request tier per merged pull request | 339.7 |
| Cancelled minutes | 29,285, a quarter of the read minutes |

The lead time and heads match the report. The report's 550 runner minutes per
merged pull request does not reproduce from these files: its run counts (344
pull request Tests runs, 621 preflights, 94 merge groups, 83 dev pushes) match a
36 hour subset of the pull, which it divided by the 167 pull requests whose
first branch run fell inside the 48 hours, and its minutes per run counted the
jobs pulled twice by its two job pulls. The same files give 455, 844, 138 and
115 runs. The tool divides by the 169 pull requests merged inside the pull; over
the report's 167 the same minutes give 725.

The fixture keeps the run fields, the jobs deduplicated by id and the merged pull
requests of these raw files (SHA256):

| File | SHA256 |
|------|--------|
| `runs.ndjson` | `2d34e5e1e8269b2089be3a49804888ea0b3edd21516274a1972cd02f8e3657a4` |
| `runs_hourly.ndjson` | `55793a2cea4a13ecb8e118259c0453ffcdfc5cf3a2ac25ac8c0e1f4c1a4c7771` |
| `jobs.ndjson` | `fa308bcfe0ab51dcaf1d1195ec87c5a5bff0f818c9fbc9def86ba8d50e0ad95f` |
| `jobs_rest.ndjson` | `b5f5af4d36541d78557cdfe537777421f1ddbd7aeacc08256b280148790fd3f9` |
| `prs_merged.json` | `fc23b4d573d6d9fb710a2748c7640d65c4a8a05e2b177ee0bdbdd39575e39981` |

49 lines of `jobs_rest.ndjson` that parallel writers interleaved do not parse
and are left out, as the report's scripts left them out.
