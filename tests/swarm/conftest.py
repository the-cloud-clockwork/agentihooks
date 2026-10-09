import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.gates import intent
from scripts.swarm import host_budget, model_pick, priority_sweep, profile_choice, runtime, slice_screen, trace_plan


@pytest.fixture(autouse=True)
def isolate_classifier(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    def engineer(*args, **kwargs):
        return DecisionResult({"responsibility": Answer("choice", choice="engineer", confidence=1.0)}, "unit-test")

    monkeypatch.setattr(model_pick, "decide", unavailable)
    monkeypatch.setattr(profile_choice, "decide", engineer)
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)
    monkeypatch.setattr(slice_screen, "decide", unavailable)
    monkeypatch.setattr(trace_plan, "decide", unavailable)
    monkeypatch.setattr(priority_sweep, "decide", unavailable)
    monkeypatch.setattr(priority_sweep.ledger_events, "view", lambda url: None)
    monkeypatch.setattr(priority_sweep.ledger_events, "views", lambda urls, cache=None: {})
    monkeypatch.setattr(intent, "decide", unavailable)
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task, run=None: False)
    monkeypatch.setattr(intent, "pr_view", lambda url, run=None: None)
    monkeypatch.setattr(intent, "pr_head", lambda url, run=None: None)


@pytest.fixture(autouse=True)
def roomy_host(monkeypatch, request):
    sample = host_budget.HostSample(load1=0.5, cpus=8, available_mb=64_000, agents=2)
    monkeypatch.setattr(runtime.HerdrRuntime, "host", lambda self: sample)
    if request.module.__name__.rpartition(".")[2] != "test_host_budget":
        monkeypatch.setattr(host_budget, "read_host", lambda: sample)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Task scratch folders for swarm sw under a stand-in scratchpad: each task id maps to its resolved folder."""
    from scripts.swarm import reaper

    root = tmp_path / "scratchpad"
    monkeypatch.setattr(reaper, "SCRATCH", root)

    def home(task):
        path = root / "repo" / f"sw-{task}"
        path.mkdir(parents=True, exist_ok=True)
        return [path.resolve()]

    return home


@pytest.fixture(autouse=True)
def unchecked_launches(request, monkeypatch):
    from scripts.swarm import launch_check

    if not getattr(request.module, "LAUNCH_CHECKED", False):
        monkeypatch.setattr(launch_check, "begin", lambda *args, **kwargs: None)
