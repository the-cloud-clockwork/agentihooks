---
title: Tick OTEL metrics
parent: Reference
nav_order: 80
---

# Tick OTEL metrics

Every swarm tick pass and every step inside it exports OpenTelemetry metrics
through the agentihooks OTEL module, `hooks.observability.otel`. A collector
turns them into Prometheus series, and the Swarm Tick dashboard in Grafana
shows pass p95, the gap since the last pass and the slowest steps. The Redis
records behind the tick report (`tick:passes`, `tick:steps`) are written as
before; the metrics are an extra copy of the same timings.

## Metrics

`scripts.tick.metrics` defines the names. Durations are histograms in seconds,
failures are counters and the pass end is a gauge.

| OTEL name | Kind | Labels | Prometheus series |
|---|---|---|---|
| `agentihooks.swarm.tick.pass.duration` | histogram, seconds | `swarm`, `outcome` | `agentihooks_swarm_tick_pass_duration_seconds_bucket`, `_sum`, `_count` |
| `agentihooks.swarm.tick.pass.failures` | counter | `swarm`, `outcome` | `agentihooks_swarm_tick_pass_failures_total` |
| `agentihooks.swarm.tick.pass.end` | gauge, Unix seconds | `swarm` | `agentihooks_swarm_tick_pass_end` |
| `agentihooks.swarm.tick.step.duration` | histogram, seconds | `swarm`, `step`, `lane`, `outcome` | `agentihooks_swarm_tick_step_duration_seconds_bucket`, `_sum`, `_count` |
| `agentihooks.swarm.tick.step.failures` | counter | `swarm`, `step`, `lane`, `outcome` | `agentihooks_swarm_tick_step_failures_total` |

Prometheus also adds `job="agentihooks"`, taken from the `service.name`
resource attribute (`OTEL_HOOKS_SERVICE_NAME`, default `agentihooks`).

### Labels

| Label | Value |
|---|---|
| `swarm` | The swarm slug the pass ran for. |
| `step` | The step's recorded name: an engine step's own name such as `metrics record` or `config load`, otherwise the qualified function name, for example `scripts.gates.progress.checks_pass`. The whole pass also records one step named `run_tick`. |
| `lane` | The lane `timing.within` was given, such as `tick`, `slow` or `closing`; `none` when the step has no lane. |
| `outcome` | `success`, `error`, `stopped`, `waiting` or `abandoned` for a step; `success`, `error` or `stopped` for a pass. |

### Buckets

Both histograms share `hooks.observability.otel.SECONDS_BUCKETS`, 0.05 seconds to
900 seconds:

```text
0.05 0.1 0.25 0.5 1 2.5 5 10 20 30 45 60 90 120 180 240 300 384 600 900
```

A value above 900 seconds lands in the `+Inf` bucket, so a quantile above
900 seconds reads as 900.

## When each metric is recorded

`scripts.swarm.timing` calls the two functions in `scripts.tick.metrics`.

| Event | Call | What it records |
|---|---|---|
| A step finishes inside a tick (`timing.step`, and through it `timing.within`, `timing.call` and `timing.timed`) | `step_finished(slug, record)` | `step.duration` with the step's wall time; `step.failures` by one when the outcome is anything but `success` |
| A pass finishes (`timing.pass_record`) | `pass_finished(slug, record)` | `pass.duration` with the pass wall time, `pass.end` with the pass end time, `pass.failures` by one when the outcome is not `success`, then `otel.flush()` |

- A step runs inside a tick only within `timing.tick(slug)`. A step outside
  one records nothing.
- A pass records only when `timing.instrument_tick` holds a `RedisStore`.
  `run_tick` called with any other store, such as a test fake, records no pass
  metrics.
- `pass_finished` runs before the pass is saved to Redis, and it runs for a
  pass that raised as well as one that returned.
- `step.failures` counts every outcome other than `success`, `waiting` and
  `abandoned` included. The pass's own `failed_steps` list leaves those two
  out, so the failure counter can be higher than the failed steps in the tick
  report.

Each tick pass is a short process. The OTLP HTTP metric exporter,
`hooks.observability.otel_delta.HttpMetricExporter`, sends counters and
histograms as deltas, so each process adds only its own share and the collector
adds them up. Gauges are sent as they are. The flush at the end of a pass ships
the points without waiting for the 60 second export interval.

## Configuration

The tick reads the same settings as every other agentihooks OTEL signal.
`hooks.config` loads `~/.agentihooks/.env` and `~/.agentihooks/*.env` at import,
so the tick process sees values set there.

| Variable | Default | Description |
|---|---|---|
| `AGENTIHOOKS_OTLP_ENDPOINT` | unset | Collector base URL. With HTTP the exporter posts to `<endpoint>/v1/metrics`. Falls back to `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `AGENTIHOOKS_OTLP_PROTOCOL` | `grpc` | `http/protobuf` for the delta exporter. Falls back to `OTEL_EXPORTER_OTLP_PROTOCOL`. |
| `OTEL_HOOKS_ENABLED` | `true` | `false` turns every agentihooks OTEL signal off, tick metrics included. |
| `OTEL_HOOKS_SERVICE_NAME` | `agentihooks` | The `service.name` resource attribute, shown in Prometheus as `job`. |

Example for a tick host sending to the observability collector:

```bash
cat >> ~/.agentihooks/.env <<'EOF'
AGENTIHOOKS_OTLP_ENDPOINT=http://192.0.2.10:4318
AGENTIHOOKS_OTLP_PROTOCOL=http/protobuf
EOF
```

With no endpoint set, or without the OpenTelemetry SDK installed, every call is
a no op and the tick runs as before. Recording never blocks the tick: values go
on the OTEL worker queue, and a full queue drops the value and counts it as
dropped.

Check the settings without printing them:

```bash
for v in AGENTIHOOKS_OTLP_ENDPOINT AGENTIHOOKS_OTLP_PROTOCOL OTEL_HOOKS_ENABLED; do
  test -n "$(printenv "$v")" && echo "$v set" || echo "$v unset"
done
```

## Collector path in antoncore

The antoncore observability stack carries the metrics to Prometheus:

1. The main collector, `observability_otel_collector`, receives OTLP on port
   4318. Its `metrics/swarm_tick` pipeline keeps only metrics whose name starts
   with `agentihooks.swarm.tick.` and forwards them to the metrics collector.
2. The metrics collector, `observability_otel_metrics`, drops
   `service.instance.id` and every `agentihooks.correlation.*` attribute so
   each label set is one series, turns the deltas into cumulative values
   (`deltatocumulative`, 15 minute staleness) and serves Prometheus on port
   8889. A series with no new point for 24 hours expires.
3. Prometheus scrapes port 8889 through an additional scrape job in the
   `kube-prometheus-stack` application.

## Swarm Tick dashboard

The dashboard sits in the Grafana `Swarm` folder with the uid `swarm-tick`, at
`<grafana>/d/swarm-tick/swarm-tick`. It refreshes every minute over the last six
hours by default. The `swarm` variable lists every swarm that reported a pass:
`label_values(agentihooks_swarm_tick_pass_duration_seconds_count, swarm)`.

| Panel | Query |
|---|---|
| Pass p95 | `histogram_quantile(0.95, sum by (le) (increase(agentihooks_swarm_tick_pass_duration_seconds_bucket{swarm=~"$swarm"}[$__range])))` |
| Gap since last pass | `max(time() - agentihooks_swarm_tick_pass_end{swarm=~"$swarm"})` |
| Passes | `sum(increase(agentihooks_swarm_tick_pass_duration_seconds_count{swarm=~"$swarm"}[$__range]))` |
| Failed passes and steps | Pass failures plus step failures over the range |
| Pass wall time | p50 and p95 per swarm from the pass histogram |
| Gap since last pass, over time | `time() - max by (swarm) (agentihooks_swarm_tick_pass_end{swarm=~"$swarm"})` |
| Slowest steps by p95 | `topk(10, histogram_quantile(0.95, sum by (le, step, lane) (increase(agentihooks_swarm_tick_step_duration_seconds_bucket{swarm=~"$swarm"}[$__range]))))` |
| Step and pass failures | Failures per minute by step, plus whole pass failures |
| Step time by lane | `sum by (lane) (rate(agentihooks_swarm_tick_step_duration_seconds_sum{swarm=~"$swarm"}[$__rate_interval])) * 60` |

## Example queries

Pass p95 for one swarm over the last hour:

```promql
histogram_quantile(0.95,
  sum by (le) (increase(agentihooks_swarm_tick_pass_duration_seconds_bucket{swarm="my-swarm"}[1h])))
```

Seconds since each swarm's last pass:

```promql
time() - max by (swarm) (agentihooks_swarm_tick_pass_end)
```

Share of the pass each lane took over the last hour:

```promql
sum by (lane) (increase(agentihooks_swarm_tick_step_duration_seconds_sum{swarm="my-swarm"}[1h]))
/ ignoring(lane) group_left
sum(increase(agentihooks_swarm_tick_pass_duration_seconds_sum{swarm="my-swarm"}[1h]))
```

The steps that failed most over the last day:

```promql
topk(5, sum by (step, outcome) (increase(agentihooks_swarm_tick_step_failures_total[1d])))
```

## Verify

List the tick series the metrics collector serves:

```bash
curl -s http://192.0.2.10:8889/metrics | grep -o '^agentihooks_swarm_tick_[a-z_]*' | sort -u
```

A swarm whose passes all succeeded shows seven names:

```text
agentihooks_swarm_tick_pass_duration_seconds_bucket
agentihooks_swarm_tick_pass_duration_seconds_count
agentihooks_swarm_tick_pass_duration_seconds_sum
agentihooks_swarm_tick_pass_end
agentihooks_swarm_tick_step_duration_seconds_bucket
agentihooks_swarm_tick_step_duration_seconds_count
agentihooks_swarm_tick_step_duration_seconds_sum
```

The two `_failures_total` series appear after the first pass or step that did
not succeed, because a counter is created on its first increment.

## Limitations

- Delta temporality applies only to the OTLP HTTP exporter. With
  `AGENTIHOOKS_OTLP_PROTOCOL=grpc` counters and histograms are sent as
  cumulative values from each short process, and the collector cannot add them
  up. Use `http/protobuf` for tick metrics.
- `agentihooks.swarm.errors`, the only other counter, is also sent as deltas
  over HTTP.
- A flush that fails starts a 60 second cooldown during which later flushes do
  not wait; points from passes in that window can be lost if the collector
  stays down.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No `agentihooks_swarm_tick_*` series at all | The tick process has no collector endpoint, or `OTEL_HOOKS_ENABLED=false`. | Set `AGENTIHOOKS_OTLP_ENDPOINT` and `AGENTIHOOKS_OTLP_PROTOCOL=http/protobuf` in `~/.agentihooks/.env`, then wait for the next pass. |
| Step series present, pass series missing | The pass ran with a store that is not a `RedisStore`. | Run the tick through `agentihooks swarm <slug> tick` or the tick service, which pass a `RedisStore`. |
| Counts jump or fall back on every pass | The exporter sends cumulative values, which happens with gRPC. | Switch to `http/protobuf`. |
| Gap since last pass reads no data | No pass for 24 hours, so the `pass_end` series expired. | Check the tick service; the panel fills on the next pass. |
| Failed passes and steps is higher than the failed steps in the tick report | `step.failures` also counts `waiting` and `abandoned` steps. | Group by `outcome` to separate them. |
| One swarm shows several series per label set | The metrics collector does not drop `service.instance.id`. | Deploy the antoncore observability stack with `otelcol-metrics.yaml`. |
