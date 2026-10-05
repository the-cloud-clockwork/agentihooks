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

Each unit shard now collects only its own test files. `--shard N/M` (`tests/conftest.py`) assigns every `tests/**/test_*.py` file to one shard, balanced on its summed `.test_durations`, and ignores the rest at collection, so no xdist worker imports and collects the whole suite to keep a quarter of it. pytest-split stays only for `--store-durations`. Locally on four pinned CPUs with cold bytecode, the slowest shard went from 6.6 to 4.5 seconds.

The shard split also weighs each test file's source size (`SECONDS_PER_SOURCE_BYTE` in `tests/shards.py`): every worker of a shard rewrites the asserts of every file it collects, while the stored durations are split between the workers. The two installer test files, the largest in the suite, no longer share a shard. Locally with cold bytecode the slowest shard's collection went from 0.78–0.81 to 0.65–0.67 seconds over three runs.

Before its xdist workers start, the controller of a sharded run forks one child per worker (`warm_imports` in `tests/shards.py`, started from `pytest_configure` in `tests/conftest.py`). Each child imports every Nth test module of the shard and exits, which leaves the assert-rewritten bytecode of the test files and the compiled bytecode of what they import in `__pycache__`. The workers then load that bytecode instead of each rewriting and compiling the whole shard on the fresh runner. The Run tests step sets `PYTHONUNBUFFERED=1` so its log stamps the session header, the collected item count and the first result when they happen. Mean time from the step start to the first test result across the eight shards went from 3.43 and 3.09 seconds (run 37247388703, two attempts) to 2.53 and 2.29 seconds (run 37248154020, two attempts); collection after the header went from 1.6–2.5 seconds to 0.5–1.6.
