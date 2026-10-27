import pytest

from scripts.workflows import schema, store
from scripts.workflows.runner import Runner, StageResult

BASE = {
    "workflow": {
        "name": "tiny",
        "intent": "Build it.",
        "head": "build",
        "exit": "verdict.pass",
        "max_iterations": 2,
        "on_exhausted": "escalate",
    },
    "stages": {
        "build": {"profile": "engineer", "input": ["case"], "next": {"pass": "verdict"}},
        "verdict": {"profile": "judge", "input": ["case", "build"], "next": {"pass": "exit", "retry": "build"}},
    },
}


def definition() -> schema.Workflow:
    return schema.validate(BASE)


def never(text, outputs):
    raise AssertionError(f"exit check called for {text}")


class ScriptedLauncher:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def launch(self, stage, inputs, folder):
        self.calls.append((stage.id, dict(inputs), folder))
        result = self.results[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def new_run(tmp_path, case="fix the thing"):
    folder = tmp_path / "run-1"
    store.create(folder, definition(), case)
    return folder


def test_step_runs_one_stage_keeps_the_run_fields_and_saves(tmp_path):
    folder = new_run(tmp_path)
    launcher = ScriptedLauncher(StageResult("pass", {"diff": 1}))
    run = Runner(definition(), launcher, never)

    state = run.step(folder)

    assert state["stage"] == "verdict"
    assert state["status"] == "running"
    assert state["outputs"] == {"build": {"diff": 1}}
    assert state["workflow"] == "tiny"
    assert state["run"] == "run-1"
    assert state["case"] == "fix the thing"
    assert store.load(folder) == state


def test_run_reaches_passed_calling_the_launcher_once_per_stage(tmp_path):
    folder = new_run(tmp_path)
    launcher = ScriptedLauncher(
        StageResult("pass", {"diff": 1}),
        StageResult("pass", {"verdict": "ship it"}),
    )
    run = Runner(definition(), launcher, never)

    state = run.run(folder)

    assert state["status"] == "passed"
    assert state["stage"] is None
    assert state["outputs"] == {"build": {"diff": 1}, "verdict": {"verdict": "ship it"}}
    assert [call[0] for call in launcher.calls] == ["build", "verdict"]
    assert store.load(folder) == state


def test_run_on_an_already_finished_folder_does_not_call_the_launcher_again(tmp_path):
    folder = new_run(tmp_path)
    launcher = ScriptedLauncher(
        StageResult("pass", {"diff": 1}),
        StageResult("pass", {"verdict": "ship it"}),
    )
    run = Runner(definition(), launcher, never)
    run.run(folder)

    second = run.run(folder)

    assert second["status"] == "passed"
    assert len(launcher.calls) == 2


def test_a_retry_loop_ends_exhausted_with_the_on_exhausted_action(tmp_path):
    folder = new_run(tmp_path)
    launcher = ScriptedLauncher(
        StageResult("pass", {"diff": 1}),
        StageResult("retry", {"notes": "round 1"}),
        StageResult("pass", {"diff": 2}),
        StageResult("retry", {"notes": "round 2"}),
    )
    run = Runner(definition(), launcher, never)

    state = run.run(folder)

    assert state["status"] == "exhausted"
    assert state["action"] == "escalate"
    assert state["iterations"] == 2
    assert len(launcher.calls) == 4


def test_inputs_combine_case_and_the_listed_earlier_outputs(tmp_path):
    folder = new_run(tmp_path)
    launcher = ScriptedLauncher(
        StageResult("pass", {"diff": 1}),
        StageResult("pass", {"verdict": "ok"}),
    )
    run = Runner(definition(), launcher, never)

    run.run(folder)

    assert launcher.calls[0][1] == {"case": "fix the thing"}
    assert launcher.calls[1][1] == {"case": "fix the thing", "build": {"diff": 1}}
    assert launcher.calls[0][2] == folder


def test_resume_after_a_crash_continues_from_the_saved_stage(tmp_path):
    folder = new_run(tmp_path)
    crashy = ScriptedLauncher(StageResult("pass", {"diff": 1}), RuntimeError("boom"))
    run = Runner(definition(), crashy, never)

    with pytest.raises(RuntimeError) as crash:
        run.run(folder)
    assert str(crash.value) == "boom"

    saved = store.load(folder)
    assert saved["stage"] == "verdict"
    assert saved["status"] == "running"
    assert saved["outputs"] == {"build": {"diff": 1}}

    resumed = Runner(definition(), ScriptedLauncher(StageResult("pass", {"verdict": "ship it"})), never)
    state = resumed.run(folder)

    assert state["status"] == "passed"
    assert state["outputs"] == {"build": {"diff": 1}, "verdict": {"verdict": "ship it"}}
