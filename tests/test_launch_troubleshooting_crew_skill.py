import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parents[1] / "profiles/package/skills/launch-troubleshooting-crew"
NOW = 1794614400.0
CID = "crew-11140000"


@pytest.fixture
def crew(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("crew", SKILL / "scripts/crew.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@demo")
    return module


class Ledger:
    def __init__(self, done: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.done = done or set()

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[-2:] == ["get", "freezes"]:
            rows = [{"verb": "focus", "target": "phases/p1"}, {"verb": "freeze", "target": "tasks/t9"}]
            rows.append({"verb": "focus", "target": "tasks/t2"})
            return json.dumps(rows)
        if command[-2:] == ["status", "--json"]:
            lanes = {"eng": {"model": "sonnet", "effort": "high"}}
            return json.dumps({"config": {"lanes": lanes, "effort_max": "xhigh"}})
        if command[-2] == "get":
            task = command[-1].removeprefix("tasks/")
            return json.dumps({"id": task, "state": "done" if task in self.done else "claimed"})
        return "{}"

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if "get" not in c and "--json" not in c]


def run(crew: ModuleType, ledger: Ledger, *argv: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert crew.main(list(argv), runner=ledger, now=NOW) == 0
    out = capsys.readouterr().out
    return json.loads(out[out.index("\n{") + 1 :] if not out.startswith("{") else out)


def test_skill_passes_the_skill_standard() -> None:
    _, front, body = (SKILL / "SKILL.md").read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert meta["name"] == SKILL.name
    assert 0 < len(meta["description"]) <= 1024
    assert not re.search(r"[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL / "evals/evals.json").read_text())
    assert len(evals) >= 3
    assert all(e["query"] and e["expected_behavior"] for e in evals)


@pytest.mark.feature("launch-troubleshooting-crew")
def test_plan_is_the_operators_default_layout_and_runs_nothing(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    plan = run(
        crew, ledger, "plan", "demo", "--problem", "launches stall", "--hypothesis", "hub refuses", capsys=capsys
    )
    assert ledger.calls == []
    scout, fix, critics, harden = plan["stages"]
    assert (scout["count"], scout["model"], scout["effort"]) == (5, "opus", "max")
    assert list(scout["tasks"]) == [f"{CID}-scout-{n}" for n in range(1, 6)]
    assert scout["tasks"][f"{CID}-scout-1"] == "hub refuses"
    assert scout["tasks"][f"{CID}-scout-5"].startswith("open: ")
    assert (fix["count"], fix["model"], fix["effort"]) == (5, "opus", "xhigh")
    assert fix["tasks"] == [f"{CID}-fix-{n}" for n in range(1, 6)]
    assert (critics["count"], critics["model"], critics["effort"], critics["as"]) == (
        3,
        "fable",
        "xhigh",
        "master sub agents",
    )
    assert harden == {"stage": "harden", "task": f"{CID}-harden", "locks": list(crew.HARDENING)}
    assert plan["cron"] == {
        "cron": "*/30 * * * *",
        "recurring": True,
        "prompt": f"Troubleshooting crew on demo, the operator's order: {crew.ORDER}",
    }


@pytest.mark.feature("launch-troubleshooting-crew")
def test_every_stage_count_model_and_effort_is_overridable(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    argv = ["plan", "demo", "--problem", "p", "--scouts", "8", "--scouts-model", "sonnet", "--fixers", "2"]
    argv += ["--fixers-effort", "high", "--critics", "1", "--critics-model", "opus", "--no-harden"]
    plan = run(crew, Ledger(), *argv, capsys=capsys)
    scout, fix, critics = plan["stages"]
    assert (len(scout["tasks"]), scout["model"], scout["effort"]) == (8, "sonnet", "max")
    assert (fix["tasks"], fix["effort"]) == ([f"{CID}-fix-1", f"{CID}-fix-2"], "high")
    assert (critics["count"], critics["model"]) == (1, "opus")


def test_a_stage_with_no_agents_is_refused(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="at least one agent"):
        crew.main(["plan", "demo", "--problem", "p", "--critics", "0"], runner=Ledger(), now=NOW)


@pytest.mark.feature("launch-troubleshooting-crew")
def test_scout_stops_other_work_sets_the_lane_and_records_the_order(
    crew: ModuleType, capsys: pytest.CaptureFixture
) -> None:
    ledger = Ledger()
    result = run(
        crew, ledger, "scout", "demo", "--problem", "launches stall", "--quote", "send the scouts", capsys=capsys
    )
    writes = ledger.writes()
    assert writes[0] == ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=max", "effort-max=max"]
    adds = [c for c in writes if c[:7] == ["agentihooks", "ledger", "--slug", "demo", "--as", "master@demo", "task"]]
    assert [c[8] for c in adds] == [f"{CID}-scout-{n}" for n in range(1, 6)]
    assert all(c[c.index("--kind") + 1] == "troubleshoot" and c[c.index("--lane") + 1] == "eng" for c in adds)
    assert all("write no code" in c[c.index("--description") + 1] for c in adds)
    focus = [c for c in writes if c[3] == "focus"]
    assert [c[4] for c in focus] == [f"tasks/{CID}-scout-{n}" for n in range(1, 6)]
    assert focus[0][5] == "--only" and all(c[5] == "--bottom" for c in focus[1:])
    assert all(c[-2:] == ["--quote", "send the scouts"] for c in focus)
    assert not any(c[3] in ("unfreeze", "freeze") or "delete" in c for c in writes)
    canon = writes[-1]
    assert canon[3] == "learned" and canon[-2:] == ["--maturity", "canon"]
    assert crew.ORDER in canon[4] and " because " in canon[4]
    assert result["earlier_focus"] == ["phases/p1", "tasks/t2"]
    assert result["cron"]["prompt"].endswith(crew.ORDER)
    assert result["restore"].endswith("crew.py restore demo")
    saved = json.loads(crew.state_path("demo").read_text())
    assert saved["earlier_lane"] == {"model": "sonnet", "effort": "high", "effort_max": "xhigh"}


def test_scout_at_an_effort_below_max_keeps_the_effort_ceiling(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    run(crew, ledger, "scout", "demo", "--problem", "p", "--scouts-effort", "xhigh", capsys=capsys)
    assert ledger.writes()[0] == ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=xhigh"]


def test_a_dry_run_scout_writes_nothing(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    run(crew, ledger, "scout", "demo", "--problem", "p", "--dry-run", capsys=capsys)
    assert ledger.calls == [] and not crew.state_path("demo").exists()


@pytest.mark.feature("launch-troubleshooting-crew")
def test_fixers_wait_until_every_scout_is_done(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    run(crew, Ledger(), "scout", "demo", "--problem", "p", capsys=capsys)
    ledger = Ledger(done={f"{CID}-scout-{n}" for n in range(1, 5)})
    with pytest.raises(SystemExit, match=f"{CID}-scout-5 are not done"):
        crew.main(["fix", "demo", "--cause", "hub drops offers"], runner=ledger, now=NOW)
    assert ledger.writes() == []


def test_fixers_need_a_proven_cause(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="--cause"):
        crew.main(["fix", "demo"], runner=Ledger(), now=NOW)


@pytest.mark.feature("launch-troubleshooting-crew")
def test_fixers_are_added_for_the_proven_causes_and_focused(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    run(crew, Ledger(), "scout", "demo", "--problem", "p", capsys=capsys)
    ledger = Ledger(done={f"{CID}-scout-{n}" for n in range(1, 6)})
    run(crew, ledger, "fix", "demo", "--cause", "hub drops offers", capsys=capsys)
    writes = ledger.writes()
    assert writes[0] == ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=xhigh"]
    adds = [c for c in writes if "add" in c]
    assert [c[8] for c in adds] == [f"{CID}-fix-{n}" for n in range(1, 6)]
    assert all("hub drops offers" in c[c.index("--description") + 1] for c in adds)
    focus = [c for c in writes if c[3] == "focus"]
    assert [c[5] for c in focus] == ["--bottom"] * 5
    critics = run(crew, Ledger(), "critics", "demo", capsys=capsys)["critics"]
    assert [(c["name"], c["model"], c["effort"]) for c in critics] == [
        (f"critic-{n}", "fable", "xhigh") for n in (1, 2, 3)
    ]
    assert all("Edit nothing" in c["prompt"] and f"{CID}-fix-5" in c["prompt"] for c in critics)


@pytest.mark.feature("launch-troubleshooting-crew")
def test_restore_brings_back_the_earlier_focus_and_lane(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    run(crew, Ledger(), "scout", "demo", "--problem", "p", capsys=capsys)
    run(crew, Ledger(), "harden", "demo", capsys=capsys)
    ledger = Ledger()
    run(crew, ledger, "restore", "demo", "--quote", "crew is over", capsys=capsys)
    writes = ledger.writes()
    unfrozen = [c[4] for c in writes if c[3] == "unfreeze"]
    assert unfrozen == [f"tasks/{CID}-scout-{n}" for n in range(1, 6)] + [f"tasks/{CID}-harden"]
    assert [c[4] for c in writes if c[3] == "focus"] == ["phases/p1", "tasks/t2"]
    assert writes[-1] == [
        "agentihooks",
        "swarm",
        "demo",
        "set",
        "eng-model=sonnet",
        "eng-effort=high",
        "effort-max=xhigh",
    ]


def test_later_stages_refuse_without_a_launched_crew(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="run the scout stage first"):
        crew.main(["restore", "demo"], runner=Ledger(), now=NOW)
