import faulthandler
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from _pytest.faulthandler import fault_handler_stderr_fd_key

from scripts.ci_mutation import runner
from scripts.ci_mutation.runner import mutate_files
from scripts.ci_mutation.selection import selected_mutant_names
from scripts.ci_mutation.stats import SharedStats

# One xdist worker builds the baseline pass once; a module cache behind a function fixture keeps that pass inside
# the function scoped home isolation, which a module fixture would run before.
pytestmark = [pytest.mark.xdist_group("remote_control_mutation"), pytest.mark.timeout(250, method="signal")]
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "scripts/swarm/commands.py"
DEADLINE = 120


@pytest.fixture(autouse=True)
def hung_test_dump_past_both_passes(request):
    # The test that builds the baseline runs both mutmut passes, each already bounded by DEADLINE, which the suite's
    # one minute dump would interrupt.
    if float(request.config.getini("faulthandler_timeout") or 0):
        faulthandler.dump_traceback_later(2 * DEADLINE + 10, file=request.config.stash[fault_handler_stderr_fd_key])


@contextmanager
def mutation_children(monkeypatch):
    children = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        child = popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            children.append(child)
        return child

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", start)
        try:
            yield
        finally:
            for child in reversed(children):
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()


@pytest.fixture(autouse=True)
def clean_mutation_children(monkeypatch):
    with mutation_children(monkeypatch):
        yield


@pytest.fixture(autouse=True)
def untraced_workspace_copy(monkeypatch):
    # Under the suite's coverage tracer the in-process checkout copy takes about 25 seconds against under one
    # untraced; the runner tests cover prepare_workspace itself.
    prepare = runner.prepare_workspace

    def untraced(*args):
        tracer = sys.gettrace()
        sys.settrace(None)
        try:
            prepare(*args)
        finally:
            sys.settrace(tracer)

    monkeypatch.setattr(runner, "prepare_workspace", untraced)


def test_mutation_children_exit_when_the_watchdog_interrupts(monkeypatch):
    child = None
    try:
        with pytest.raises(pytest.fail.Exception, match="watchdog"):
            with mutation_children(monkeypatch):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                pytest.fail("watchdog")
        assert child.poll() is not None
    finally:
        if child is not None:
            child.kill()
            child.wait()


TIMELINE = []


def timed(monkeypatch):
    import scripts.ci_mutation.runner as runner

    start = time.monotonic()
    prepare = runner.prepare_workspace

    def timed_prepare(*args):
        prepare(*args)
        TIMELINE.append(f"{time.monotonic() - start:7.1f} prepare_workspace done")

    def timed_run(command, cwd, timeout, log):
        TIMELINE.append(f"{time.monotonic() - start:7.1f} start {command[2:4]}")
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
        )
        with log.open("w") as out:
            for line in process.stdout:
                out.write(line)
                TIMELINE.append(f"{time.monotonic() - start:7.1f} {line.rstrip()[:160]}")
        return process.wait()

    monkeypatch.setattr(runner, "prepare_workspace", timed_prepare)
    monkeypatch.setattr(runner, "run_process", timed_run)


@pytest.fixture(scope="module")
def baseline_cache():
    return []


@pytest.fixture(scope="module")
def checkout_scratch():
    # Hard links copy the checkout into a workspace; on another device, as the runner's temp folder is, every file is
    # copied, which took up to twenty seconds more per workspace in CI.
    folder = Path(tempfile.mkdtemp(prefix="remote-control-mutation-", dir=ROOT.parent))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def baseline(baseline_cache, checkout_scratch, monkeypatch):
    # A cached pass that recorded an error is rebuilt, so a rerun or the next test retries a load-bound overrun.
    if not baseline_cache or baseline_cache[0][4]:
        baseline_cache.clear()
        line = next(
            number
            for number, text in enumerate((ROOT / SOURCE).read_text().splitlines(), 1)
            if "order = store.redis.lrange" in text
        )
        selected = {SOURCE: ({line}, ["tests/swarm_ledger/test_remote_control_process.py"])}
        folder = Path(tempfile.mkdtemp(prefix="baseline-", dir=checkout_scratch))
        stats = SharedStats(folder / "stats", "remote-control", (0, 1))
        stats.folder.mkdir()
        work = folder / "baseline"
        work.mkdir()
        monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        timed(monkeypatch)
        results, error = mutate_files(ROOT, work, selected, time.monotonic() + DEADLINE, (0, 1), stats)
        baseline_cache.append((selected, stats, work, results, error))
    return baseline_cache[0]


def test_remote_control_statistics_baseline_collects_without_grading(baseline):
    _, _, work, results, error = baseline
    assert error == "", (work / "run.log").read_text()
    assert results == {SOURCE: []}
    pytest.fail("PROFILE\n" + "\n".join(TIMELINE[-400:]))


def test_remote_control_statistics_baseline_feeds_mutant_grading(baseline, checkout_scratch, monkeypatch):
    selected, stats, work, _, error = baseline
    assert error == "", (work / "run.log").read_text()
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    grading = Path(tempfile.mkdtemp(prefix="grading-", dir=checkout_scratch)) / "grading"
    grading.mkdir()
    mutants = selected_mutant_names(SOURCE, (ROOT / SOURCE).read_text(), selected[SOURCE][0])
    timed(monkeypatch)
    results, error = mutate_files(
        ROOT, grading, selected, time.monotonic() + DEADLINE, (0, len(mutants)), SharedStats(stats.folder, stats.head)
    )
    assert error == "", (grading / "run.log").read_text()
    assert len(results[SOURCE]) == 1, results
    assert any(row["status"] == "killed" for row in results[SOURCE]), results
    pytest.fail("PROFILE\n" + "\n".join(TIMELINE[-400:]))
