---
title: Inbox reconnect alarm and the mcp SDK
parent: Reference
nav_order: 31
---

# Inbox reconnect alarm and the mcp SDK

The inbox channel reconnect tests bound each recovery loop with a five second
`SIGALRM`. An alarm that fires while Python is still importing a module leaves
that import half done for the rest of the pytest worker. This reference explains
how that broke unrelated tests, the fixture contract that prevents it, the probe
tests that hold the contract and how to diagnose a failure. It is for
contributors who change the inbox tests, the `mcp-sdk` worker group or the code
those tests import.

[PR #4607](https://github.com/the-cloud-clockwork/agentihooks/pull/4607) added
the `mcp_sdk` fixture, listed it in the reconnect module's marks and added its
probe test.
[PR #4985](https://github.com/the-cloud-clockwork/agentihooks/pull/4985) made
`recovery_deadline` depend on `mcp_sdk` and added `fakeredis` and
`tests.inbox.test_channel_server` to the preload.
[PR #5244](https://github.com/the-cloud-clockwork/agentihooks/pull/5244) added the
hive scope and the inbox pool. All three
change tests only; the inbox channel, its reconnect backoff and the mcp server
code are unchanged.

## What failed

The Doctor raised the flaky tests finding for
`tests/test_profile_browser.py::test_a_list_without_a_matching_cache_launches_once_and_caches_the_tools`
on head `dfff55a0a2`: in run
[39158132038](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39158132038),
job `unit (3.12, 1)`, attempt 1 failed and attempt 2 passed on the same commit.

The browser test itself was not at fault. In attempt 1 the first channel
reconnect case on that worker took 5.37 seconds and was rerun, then both browser
cases that patch `mcp.server.stdio.stdio_server` failed with:

```text
'module' object at mcp.server.stdio has no attribute 'stdio'
```

In attempt 2 the same reconnect case took 3.6 seconds and every test passed.

## Root cause

CI runs the unit suite with `--dist loadgroup` and `--reruns 2`. Under
`loadgroup`, every module that carries `pytest.mark.xdist_group("mcp-sdk")` runs
on the same pytest worker, and `--reruns 2` retries a failed test up to twice on
that worker:

| Module | Why it is in the group |
|---|---|
| `tests/inbox/test_channel_reconnect.py` | Runs the inbox channel under the reconnect alarm |
| `tests/test_profile_browser.py` | Patches `mcp.server.stdio.stdio_server` |
| `tests/test_profile_connectors.py`, `tests/test_mcp_registry.py`, `tests/test_mcp_lazy.py`, `tests/test_mcp_session.py`, `tests/test_serena_router.py` | Import or patch the mcp SDK |
| Others marked `xdist_group("mcp-sdk")` | Run `grep -rl 'xdist_group("mcp-sdk")' tests` for the current list |

The sequence on a cold worker:

1. The first reconnect case arms the `recovery_deadline` alarm. Its test helpers
   in `tests/inbox/test_channel_server.py` import `mcp.types` and
   `mcp.shared.message` inside their functions, so the mcp SDK is imported for
   the first time under the alarm. `scripts/inbox/channel.py` loads at collection
   but imports mcp only inside its functions.
2. The cold import runs past five seconds. `mcp.server.stdio` has already loaded.
3. The alarm raises `AssertionError` inside the fastmcp server import. Python
   removes the partly imported `mcp.server` from `sys.modules` but keeps
   `mcp.server.stdio`.
4. The rerun imports `mcp.server` again. Because `mcp.server.stdio` is already in
   `sys.modules`, the new `mcp.server` never gets its `stdio` attribute.
5. Every later `monkeypatch.setattr("mcp.server.stdio.stdio_server", ...)` on that
   worker resolves `mcp.server.stdio` through the attribute and fails.

A retry on a new worker passes because the import finishes inside the window.
A longer deadline would hide the import cost on most runs; it would not remove
the half imported state when the import is slow enough.

## The fixture contract

`tests/inbox/conftest.py` loads every module a reconnect case would otherwise
import for the first time under the alarm, before the alarm is armed:

```python
@pytest.fixture
def mcp_sdk():
    import fakeredis  # noqa: F401
    import mcp.server.lowlevel  # noqa: F401
    import mcp.server.stdio  # noqa: F401
    import mcp.shared.message  # noqa: F401

    import scripts.hive.scope  # noqa: F401
    import scripts.inbox.pool  # noqa: F401
    import tests.inbox.test_channel_server  # noqa: F401

    yield


@pytest.fixture
def recovery_deadline(mcp_sdk):
    ...
    signal.setitimer(signal.ITIMER_REAL, 5)
```

The guard is the `mcp_sdk` parameter of `recovery_deadline`: pytest sets up
`mcp_sdk` before the fixture body arms the alarm, whatever order a module lists
them in. Every test that uses `recovery_deadline` gets the preload this way:

| Module | Uses the alarm |
|---|---|
| `tests/inbox/test_channel_reconnect.py` | Every case, through `pytestmark` |
| `tests/inbox/test_waker_reconnect.py` | Every case, through `pytestmark` |
| `tests/inbox/test_codex_pump.py` | Every case, through the autouse `watchdog` fixture |
| `tests/inbox/test_realtime.py` | The waker wake pass on notify case |

The reconnect module also names both fixtures in its marks:

```python
pytestmark = [pytest.mark.xdist_group("mcp-sdk"), pytest.mark.usefixtures("mcp_sdk", "recovery_deadline")]
```

`mcp_sdk` is function scoped and runs for every such test. Only the first test
on a worker pays the cold import; later tests find the modules in `sys.modules`.
A cold import stays outside the alarm, so the five second deadline measures only
the recovery loop it guards.

| Setting | Value | Where |
|---|---|---|
| Recovery deadline | 5 seconds, `ITIMER_REAL` | `recovery_deadline` fixture |
| Deadline failure | `AssertionError: recovery loop exceeded its deadline` | the alarm handler |
| Worker group | `mcp-sdk` | module `pytestmark` |

These are test constants. No environment variable or operator setting changes
them.

## Probe tests

`tests/inbox/test_channel_reconnect_alarm.py` runs one reconnect case under real
pytest in a fresh interpreter, so the SDK is cold as on a new CI worker. A
recording plugin wraps `signal.setitimer` and notes, each time the alarm is
armed, whether `mcp.server` has its `stdio` attribute, whether
`mcp.shared.message` is loaded and whether `fakeredis` is loaded. The probe
prints one JSON line with the pytest exit code and those records.

| Test | Planted condition | Expected report |
|---|---|---|
| `test_the_channel_reconnect_alarm_is_armed_only_after_the_sdk_is_loaded` | None | `{"code": 0, "armed": [{"stdio": true, "message": true, "fakeredis": true}]}` |
| `test_a_slow_cold_fakeredis_import_is_not_charged_to_the_reconnect_alarm` | `PLANTED_DELAY=6`: the first `fakeredis` import sleeps six seconds | The same report: the delay lands in the preload, before the alarm |
| `test_the_subscriber_case_imports_nothing_while_its_reconnect_alarm_is_armed` | `ARMED_DELAY=6`: the first import started while the alarm is armed sleeps six seconds and is recorded | The same report with `"late": []`: no import starts under the alarm |

`PLANTED_DELAY` and `ARMED_DELAY` are read only by the probe subprocess. The probe
strips `PYTEST_ADDOPTS` so CI options such as reruns do not reach the inner run.

Only the subscriber case runs with `ARMED_DELAY`, so only that case reports late
imports. The pending items case is checked for SDK state when the alarm is armed;
the other reconnect cases and the waker, Codex pump and realtime modules are
covered by the shared fixture but not probed.

## Change the reconnect tests safely

Adding a reconnect case, or code that a reconnect case reaches for the first time:

1. Push the change and read the subscriber probe in CI. A non empty `late` list
   names the first module the subscriber case imported while the alarm was
   armed. For a module the subscriber case never reaches, add a probe test that
   passes that case's node id to `_probe` with `ARMED_DELAY="6"`.
2. Add that module to `mcp_sdk` as a plain import.
3. Push again and confirm `late` is empty.

Adding a test module to the `mcp-sdk` group needs no preload of its own when it
arms no alarm. A new test that arms its own timer should use `recovery_deadline`
so it inherits the preload.

Do not raise the deadline or remove the `mcp_sdk` parameter from
`recovery_deadline`. Either lets a cold import run under the alarm again.

## Verify in CI

Tests run in CI; do not run them on the shared swarm host. Run the focused proof
on a pushed branch:

```bash
gh workflow run proofs.yml \
  --ref my-pushed-branch \
  -f proof=tests \
  -f tests="tests/inbox/test_channel_reconnect_alarm.py tests/inbox/test_channel_reconnect.py"
```

Read each case's outcome and the flake report. A green job after a retry is not a
first attempt pass.

Retained evidence, as recorded in the PR #4607 body. The probe then recorded
only `stdio` and `message`; PR #4985 added the `fakeredis` key.

- Red, with the `mcp_sdk` fixture dropped on the reverted wip commit `85081aa6c`
  ([run 39161684579](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39161684579)):
  the probe printed `{"code": 0, "armed": [{"stdio": false, "message": false}]}`;
  1 failed, 141 passed.
- Green, with the fixture, commit `b15a6ac3b`
  ([run 39161687437](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39161687437)):
  142 passed.

Those counts cover that run's test selection, not the two file command above.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `'module' object at mcp.server.stdio has no attribute 'stdio'` in any `mcp-sdk` group test | An alarm interrupted the cold SDK import earlier on the same worker | Find the first reconnect case on that worker in the job log. Check its duration and whether CI reran it. Then run the subscriber probe to name the late import |
| `AssertionError: recovery loop exceeded its deadline` on the first reconnect case of a worker only | A first import under the alarm took the window | Read the subscriber probe's `late` list and add the module to `mcp_sdk` |
| The same assertion on every attempt and worker | The recovery loop itself is slow or stuck | Debug the reconnect code; the preload is not involved |
| A probe report shows `"stdio": false` or `"message": false` | `recovery_deadline` lost its `mcp_sdk` parameter | Restore `def recovery_deadline(mcp_sdk)` in `tests/inbox/conftest.py` |
| A probe fails to parse JSON | The inner pytest crashed before printing | Read the probe's captured output in the assertion message |

Preserve the exact node id, run, job and attempt when comparing failures. A later
passing attempt does not erase the earlier failure.
