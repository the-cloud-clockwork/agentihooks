---
title: CI cost and lead time metrics
parent: Reference
nav_order: 92
---

# CI cost and lead time metrics

`python -m scripts.ci_metrics` measures one hour of agentihooks CI and writes the
result to the ClickHouse tables `swarm.ci_cost` and `swarm.ci_lead_time` through
the metrics outbox. An hourly CronJob in the `agentihooks-swarm` namespace runs
it from the swarm image, so every later CI task reads its before and after
numbers from the same rows.

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
| `--prometheus` | required | Prometheus base URL for the pending runner average |
| `--lag-hours` | 2 | Measure the hour that ended this many hours before the current hour, so its runs have finished |
| `--until` | none | Measure the hour that ends at this UTC time instead |

The run needs `AGENTIHOOKS_METRICS_URL`, `AGENTIHOOKS_METRICS_USER` and
`AGENTIHOOKS_METRICS_PASSWORD` (the swarm metrics secret) and a GitHub token for
`gh`. The CronJob takes the token from the GitHub App credentials through
`scripts.github_app_cli exec`, which mints one installation token for the whole
run. A run refuses without the sink settings and fails when the sink takes fewer
rows than the run built. It prints the window and the row counts on one JSON
line and the headline numbers on a second.

Each run ships through a fresh outbox that is removed when the run ends, and the
outbox gives a flush five seconds. A sink that refuses or answers slowly fails
the run, and its hour has no rows until it is measured again:

```bash
kubectl -n agentihooks-swarm create job --from=cronjob/agentihooks-swarm-ci-metrics ci-metrics-rerun
```

reruns the lagged hour, and a run with `--until` set to the end of the missed
hour measures an older one. Rows of a window measured twice replace each other.

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

49 lines of `jobs.ndjson` that two parallel writers interleaved do not parse
and are left out, as the report's scripts left them out.
