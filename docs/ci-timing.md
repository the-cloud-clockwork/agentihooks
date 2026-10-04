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

The unit job now runs each Python version as four shards on four runners (pytest-split, `--splitting-algorithm least_duration`), each shard still on `-n auto`. Shards are balanced from `.test_durations`; refresh it with `python -m tests.refresh_durations`, which runs the suite five times and stores each test's median. A single `--store-durations` run is too noisy: against two held-out runs it split the shards 4.4 to 9.6 seconds of test time, the five-run median 7.7 to 8.9. A test fails once a tenth of the suite has no stored duration. Locally the four shards take 8.6 to 9.9 seconds each with two workers.

Coverage was most of each shard's Run tests time. Only the 3.12 shards measure it now, with `COVERAGE_CORE=sysmon`; the 3.11 shards run without it. Run tests per shard went from 26 to 35 seconds (run 37239540788) to 13 to 18 seconds on 3.11 and 16 to 26 seconds on 3.12 (run 37239888769).

The Tests workflow no longer measures coverage on any shard; Sonar measures it in its own workflow. Before the change the slowest 3.12 shard ran 21 to 26 seconds against 15 to 22 seconds for the slowest 3.11 shard (runs 37241498657, 37241419135, 37241352383).

A dev push no longer repeats the suite its pull request already passed. Each passing pull request run records the tree it tested as a `tests-passed-<tree>` artifact; on a dev push the first step of every unit shard and of lint looks that artifact up for the pushed tree and skips the job's remaining steps when it exists. A separate lookup job delayed every shard of a dev push by 7 to 10 seconds (runs 37242559882, 37242057744, 37241643241). A squash merge onto a dev that moved since the pull request run has a different tree and runs the full suite.
