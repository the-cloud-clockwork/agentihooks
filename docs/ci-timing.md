# Tests workflow timing

Last ten runs of the Tests workflow on dev (35380163028, 35379942561, 35379468297, 35156774129, 35156548701, 35156299806, 35155214526, 35138446531, 35138276256, 35137837913). Seconds per step, mean and max over those runs.

| Job | Step | Mean | Max | Slowest run |
|---|---|---|---|---|
| unit (3.12) | Run tests | 39.6 | 41 | 35155214526 |
| unit (3.11) | Run tests | 34.1 | 41 | 35156774129 |
| unit (3.11) | Install dependencies | 19.0 | 34 | 35138446531 |
| unit (3.12) | Install dependencies | 18.1 | 26 | 35156548701 |
| lint | Install ruff | 2.0 | 4 | 35137837913 |

Slowest step: Run tests in the unit job. The two matrix jobs already run in parallel, and lint finishes in under 10 seconds. Dependency install is the largest remaining cost and is recorded as ledger task ci-pip-cache.

Run tests took 55 to 84 seconds per matrix job on a single process. The Tests workflow now runs pytest with `-n auto` (pytest-xdist); locally four workers take the suite from 82 to 34 seconds. Re-measure this table after the first runs on dev.

The unit job now runs each Python version as four shards on four runners (pytest-split, `--splitting-algorithm least_duration`), each shard still on `-n auto`. Shards are balanced from `.test_durations`; refresh it with `python -m pytest tests/ -n 4 --store-durations` when the split drifts. Locally the four shards take 8.6 to 9.9 seconds each with two workers.
