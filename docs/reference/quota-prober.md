---
title: Quota prober
parent: Reference
nav_order: 71
---

# Quota prober

The tick pass reads account quota from the quota caches and never probes an account itself. The
`agentihooks-quota-prober` user service probes every account once a minute and writes those caches. When the
prober stops, the quota notice step reports how old the newest reading is.

A Claude probe runs `claude -p` once per stale credential and a Codex probe sends a Codex request. Both take
seconds. Inside the pass they held every step that waits on the quota capacity decision, which runs before spawn.
With the probes in their own service, the pass spends no time on them.

## Who probes and who reads

| Caller | Call | Claude | Codex |
| --- | --- | --- | --- |
| Tick pass | `HerdrRuntime.quota_capacity(...)` with its default `refresh=False`, which calls `capacity.accounts(environ, now, refresh=False)` | Reads the cached observations (`balancer.cached_observations`) and the master snapshot; runs no `claude -p` | Reads the last recorded quota through `CodexAccountSource(refresh=False)`; runs only the local login status check, never a Codex quota request |
| Quota prober | `quota_refresh.probe(environ, now)`, which calls `capacity.probe(environ, now)` | Probes every stale credential and writes the Claude cache | Probes stale Codex readings and writes the Codex cache |
| Any other caller of `capacity.accounts` | `refresh` defaults to `True` | Probes stale credentials | Probes stale readings |

`capacity.probe` is `_claude(environ, now, True) + _codex(environ, now, True)`: the same rows a refreshing
`capacity.accounts` builds, without the global aggregate route and the reservation pass. Its job is the cache
write. The tick pass reads that cache on its next run.

A caller that needs a fresh reading inside the pass passes `refresh=True` to `quota_capacity`. No tick step
does.

## The prober service

`agentihooks swarm prober` runs the loop. The swarm timer install writes it as
`~/.config/systemd/user/agentihooks-quota-prober.service`, with `Restart=always` and the environment from
`~/.agentihooks/.env`. The service already answered quota refresh requests from the ledger page. Those requests
still work the same way.

Each round of the loop:

1. Probes when the next probe is due (`quota_refresh.probe_due`). The first round always probes. After a probe the
   next one is due `quota_refresh.PROBE_S` (60) seconds later, measured on the monotonic clock.
2. Waits up to `quota_refresh.WAIT_S` (30) seconds for a wake on the quota refresh channel, published when the
   ledger page asks for a refresh.
3. Answers every pending ledger page quota request (`answer_all`).

A probe can only start at the top of a round, so the real gap between probes is between 60 and about 90 seconds.
Each probe prints one line to the service log:

```text
quota probe read 7 accounts
```

The number counts the Claude and Codex rows the probe built.

### Check the prober

```bash
systemctl --user status agentihooks-quota-prober.service
journalctl --user -u agentihooks-quota-prober.service --since "5 min ago" | grep "quota probe read"
agentihooks quota
```

A healthy service logs a `quota probe read` line about once a minute. The `AGE` column of `agentihooks quota`
shows how old each account's reading is.

Restart the service after a Codex re-login or a token change, because it keeps the environment it started with:

```bash
systemctl --user restart agentihooks-quota-prober.service
```

## Stale readings in the quota notice step

The quota notice step (`quota_notice.refresh`, in the `placement notices` group) adds one action when the newest
`observed_at` across the accounts of the stored capacity decision is older than `quota_notice.STALE_S` (180)
seconds:

```text
quota readings are 999 seconds old; the quota prober has not refreshed them
```

The age is whole seconds from the newest reading to the pass time. One fresh account is enough to keep the line
quiet. Rows with no `observed_at` are ignored, and a decision with no readings at all adds nothing. The step still
sends the account level notices and the quota handoff warning from the readings it has. It does not probe.

The tick report's step actions and the pass log carry the line. Three minutes covers about two missed probes.

## Limits

- The pass places and warns from readings up to one probe gap old, longer when the prober is down. A Claude
  account that ran out inside that gap stays open until the next probe.
- The prober runs on the machine that installed the swarm timers. A `distributed` workstation retires the prober
  unit with the tick ([Deployment modes](deployment-modes.md#modes)).
- `quota_capacity` defaults to cached readings, so code that called it expecting a probe now gets the cache. Pass
  `refresh=True` for a probe.

## Tests

| Test | Proves |
| --- | --- |
| `tests/swarm/test_capacity.py::test_a_pass_read_runs_only_the_local_codex_login_status_check` | `accounts(refresh=False)` runs only the Codex login check; `accounts()` runs `claude -p`, the login check and `codex exec` |
| `tests/swarm/test_capacity.py::test_the_quota_notice_step_runs_no_claude_or_codex_quota_probe` | A whole quota notice step stores a capacity decision while running only the Codex login check |
| `tests/swarm/test_capacity.py::test_the_pass_reader_defaults_to_cached_readings` | `HerdrRuntime.quota_capacity` defaults `refresh` to `False` |
| `tests/swarm/test_capacity.py::test_the_prober_writes_the_claude_cache_the_pass_reads` | A probe writes the Claude cache and a later pass read reports that probe's `observed_at` |
| `tests/swarm/test_capacity.py::test_a_probe_refreshes_claude_and_codex_at_the_given_time` | `capacity.probe` calls the Claude and Codex readers with `refresh=True` at the given time |
| `tests/swarm/test_quota_refresh.py::test_the_prober_probes_once_a_minute_between_wakes` | `probe_due` probes at once, then sixty seconds after each probe |
| `tests/swarm/test_quota_refresh.py::test_main_runs_the_quota_prober_without_a_slug_on_the_hive_command_runner` | `swarm prober` hands the loop a probe over this process's environment and wall clock |
| `tests/swarm/test_quota_refresh.py::test_the_quota_notice_names_a_reading_over_three_minutes_old` | The stale line appears at 181 seconds, not at 180, and never without readings |
| `tests/swarm/test_quota_refresh.py::test_the_quota_notice_step_adds_the_reading_age` | The quota notice step appends the stale line after its other actions |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `quota readings are N seconds old; the quota prober has not refreshed them` in the pass actions | The prober service is stopped, failing or not installed on this host | `systemctl --user status agentihooks-quota-prober.service`; read its journal; reinstall the swarm timers when the unit is missing |
| The prober logs `quota probe read` but the stale line stays | Every probe failed to record a reading, for example every token expired | Run `agentihooks balance` and fix the accounts it reports; see [Refreshing tokens](../pillars/load-balancing.md#refreshing-tokens) |
| An account shows its old state after a re-login | The prober kept its old environment | Restart the prober service |
| Spawns land on an account that ran out a minute ago | The pass read the last probe, taken before the account ran out | Expected inside one probe gap; the next probe closes the account |
| Ledger page quota refresh never answers | The prober service is down; it answers page requests too | Restart the service and check its journal |
