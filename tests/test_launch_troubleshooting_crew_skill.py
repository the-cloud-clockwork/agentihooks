import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from scripts.swarm import troubleshooting_crew

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parents[1] / "profiles/package/skills/launch-troubleshooting-crew"
NOW = 1794614400.0
CID = "crew-1114000000"
ME = ["agentihooks", "ledger", "--slug", "demo", "--as", "master@demo"]
QUOTE = troubleshooting_crew.LAUNCH_QUOTE
ENTRY = '"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/launch-troubleshooting-crew/scripts/crew.py'


@pytest.fixture
def crew(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@demo")
    return troubleshooting_crew


class Ledger:
    def __init__(
        self,
        done: set[str] | None = None,
        agents: list[dict[str, str]] | None = None,
        gone: set[str] | None = None,
        flagged: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.done = done or set()
        self.agents = agents or []
        self.gone = gone or set()
        self.flagged = flagged or set()

    def __call__(self, command: list[str]) -> str:
        self.calls.append(command)
        if command[-2:] == ["get", "freezes"]:
            rows = [{"verb": "focus", "target": "phases/p1"}, {"verb": "freeze", "target": "tasks/t9"}]
            rows.append({"verb": "focus", "target": "tasks/t2"})
            return json.dumps(rows)
        if command[-2:] == ["status", "--json"]:
            lanes = {"eng": {"agent": "claude", "model": "sonnet", "effort": "high"}}
            return json.dumps({"config": {"lanes": lanes, "effort_max": "xhigh"}, "agents": self.agents})
        if command[-2] == "get":
            if command[-1] in self.gone:
                raise troubleshooting_crew.CommandFailed(f"{command[-1]} does not exist")
            task = command[-1].removeprefix("tasks/")
            row: dict[str, Any] = {"id": task, "state": "done" if task in self.done else "claimed"}
            return json.dumps({**row, "done": True} if task in self.flagged else row)
        return "{}"

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if "get" not in c and "--json" not in c]


def run(crew: ModuleType, ledger: Ledger, *argv: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert crew.main(list(argv), runner=ledger, now=NOW) == 0
    out = capsys.readouterr().out
    start = out.index("{\n  ")
    assert out.endswith("\n}\n")
    return json.loads(out[start:])


def scouted(crew: ModuleType, capsys: pytest.CaptureFixture[str], *flags: str) -> None:
    run(crew, Ledger(), "scout", "demo", "--problem", "p", *flags, capsys=capsys)


def fixed(crew: ModuleType, capsys: pytest.CaptureFixture[str], *flags: str) -> Ledger:
    ledger = Ledger(done={f"{CID}-scout-{n}" for n in range(1, 6)})
    run(crew, ledger, "fix", "demo", "--cause", "c1", "--cause", "c2", *flags, capsys=capsys)
    return ledger


def scout_add(n: int, hypothesis: str) -> list[str]:
    text = troubleshooting_crew.SCOUT_TEXT.format(problem="p", hypothesis=hypothesis)
    return [
        *ME, "task", "add", f"{CID}-scout-{n}", f"Troubleshooting crew {CID}-scout-{n}",
        "--not-duplicate", "one scout of the troubleshooting crew, each with its own hypothesis",
        "--lane", "eng", "--kind", "troubleshoot", "--rank", "urgent", "--description", text,
        "--must", "the hypothesis is confirmed, refuted or unproven with evidence",
        "--check", "the evidence cited on the task", "--judge", "master",
    ]  # fmt: skip


def focus(target: str, flag: str, reason: str = "troubleshooting crew", quote: str = QUOTE) -> list[str]:
    return ["agentihooks", "swarm", "demo", "focus", target, flag, "--reason", reason, "--quote", quote]


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


def test_skill_entry_point_runs_the_crew_module() -> None:
    entry = (SKILL / "scripts/crew.py").read_text()
    assert "from scripts.swarm.troubleshooting_crew import main" in entry
    assert (SKILL / "scripts/crew.py").stat().st_mode & 0o111


@pytest.mark.feature("launch-troubleshooting-crew")
def test_plan_is_the_operators_default_layout_and_runs_nothing(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    plan = run(
        crew, ledger, "plan", "demo", "--problem", "launches stall", "--hypothesis", "hub refuses", capsys=capsys
    )
    assert ledger.calls == []
    assert (plan["slug"], plan["crew"], plan["problem"]) == ("demo", CID, "launches stall")
    scout, fix, critics, harden = plan["stages"]
    spare = "open: find a cause no other scout's hypothesis names"
    tasks = {f"{CID}-scout-1": "hub refuses", **{f"{CID}-scout-{n}": spare for n in range(2, 6)}}
    assert scout == {"stage": "scout", "count": 5, "model": "opus", "effort": "max", "tasks": tasks}
    fixers = [f"{CID}-fix-{n}" for n in range(1, 6)]
    assert fix == {"stage": "fix", "count": 5, "model": "opus", "effort": "xhigh", "tasks": fixers}
    assert critics == {"stage": "critics", "count": 3, "model": "fable", "effort": "xhigh", "as": "master sub agents"}
    locks = ["feature map tests", "Semgrep rules", "unit tests", "smoke tests", "anti regression tests"]
    assert harden == {"stage": "harden", "task": f"{CID}-harden", "locks": locks}
    layout = "scouts 5 at opus max, fixers 5 at opus xhigh, critics 3 at fable xhigh"
    prompt = f"Troubleshooting crew on demo, the operator's order: {crew.ORDER} This run's layout: {layout}."
    assert plan["cron"] == {"cron": "*/30 * * * *", "recurring": True, "prompt": prompt}


@pytest.mark.feature("launch-troubleshooting-crew")
def test_every_stage_count_model_and_effort_is_overridable(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    argv = ["plan", "demo", "--problem", "p", "--scouts", "1", "--scouts-model", "sonnet", "--fixers", "2"]
    argv += ["--fixers-effort", "high", "--critics", "1", "--critics-model", "opus", "--no-harden"]
    plan = run(crew, Ledger(), *argv, "--hypothesis", "a", "--hypothesis", "b", capsys=capsys)
    scout, fix, critics = plan["stages"]
    assert (scout["tasks"], scout["model"], scout["effort"]) == ({f"{CID}-scout-1": "a"}, "sonnet", "max")
    assert (fix["tasks"], fix["effort"]) == ([f"{CID}-fix-1", f"{CID}-fix-2"], "high")
    assert (critics["count"], critics["model"]) == (1, "opus")


def test_a_stage_with_no_agents_or_an_unknown_effort_is_refused(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="^every crew stage needs at least one agent$"):
        crew.main(["plan", "demo", "--problem", "p", "--critics", "0"], runner=Ledger(), now=NOW)
    with pytest.raises(SystemExit) as refused:
        crew.main(["plan", "demo", "--problem", "p", "--scouts-effort", "huge"], runner=Ledger(), now=NOW)
    assert refused.value.code == 2


def test_the_problem_and_the_session_name_are_required(crew: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    for stage in ("plan", "scout"):
        with pytest.raises(SystemExit, match="^state the problem with --problem$"):
            crew.main([stage, "demo"], runner=Ledger(), now=NOW)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    with pytest.raises(SystemExit, match="^name this session with --as or AGENTIHOOKS_AGENT_NAME$"):
        crew.main(["scout", "demo", "--problem", "p"], runner=Ledger(), now=NOW)
    for stage in ("fix", "harden"):
        with pytest.raises(SystemExit, match="^name this session"):
            crew.main([stage, "demo", "--cause", "c"], runner=Ledger(), now=NOW)


def test_parser_names_the_skill_command() -> None:
    parse = troubleshooting_crew.parser()
    assert (parse.prog, parse.description) == ("crew.py", "the operator's troubleshooting crew")
    args = parse.parse_args(["plan", "demo"])
    assert (args.problem, args.hypothesis, args.cause, args.quote) == ("", [], [], QUOTE)
    assert (args.dry_run, args.no_harden, args.scouts, args.fixers_model, args.critics_effort) == (
        False, False, None, None, None,
    )  # fmt: skip


@pytest.mark.feature("launch-troubleshooting-crew")
def test_scout_stops_other_work_sets_the_lane_and_records_the_order(
    crew: ModuleType, capsys: pytest.CaptureFixture
) -> None:
    ledger = Ledger()
    result = run(crew, ledger, "scout", "demo", "--problem", "p", "--hypothesis", "h1", capsys=capsys)
    spare = crew.SPARE
    canon = crew.CANON_TEXT.format(order=crew.ORDER, layout=crew.layout(crew.Crew()))
    assert ledger.writes() == [
        ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=max", "effort-max=max"],
        scout_add(1, "h1"),
        *[scout_add(n, spare) for n in range(2, 6)],
        focus(f"tasks/{CID}-scout-1", "--only"),
        *[focus(f"tasks/{CID}-scout-{n}", "--bottom") for n in range(2, 6)],
        ["agentihooks", "swarm", "demo", "learned", canon, "--maturity", "canon"],
    ]
    assert crew.ORDER in canon and " because " in canon
    assert result["earlier_focus"] == ["phases/p1", "tasks/t2"]
    assert result["scouts"] == [f"{CID}-scout-{n}" for n in range(1, 6)]
    assert result["cron"] == crew.cron("demo", crew.Crew())
    assert result["restore"] == f"python3 {ENTRY} restore demo"
    saved = json.loads(crew.state_path("demo").read_text())
    assert saved["earlier_lane"] == {"agent": "claude", "model": "sonnet", "effort": "high", "effort_max": "xhigh"}
    assert (saved["launched"], saved["crew_id"], saved["problem"]) == (True, CID, "p")
    assert crew.state_path("demo").read_text().startswith('{\n  "crew_id": ')


def test_scout_at_an_effort_below_max_keeps_the_effort_ceiling(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    run(crew, ledger, "scout", "demo", "--problem", "p", "--scouts-effort", "xhigh", capsys=capsys)
    assert ledger.writes()[0] == ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=xhigh"]


def test_a_dry_run_scout_writes_nothing(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    ledger = Ledger()
    assert crew.main(["scout", "demo", "--problem", "p", "--dry-run", "--as", "x"], runner=ledger, now=NOW) == 0
    printed = capsys.readouterr().out.splitlines()
    assert ledger.calls == [] and not crew.state_path("demo").exists()
    assert json.loads(printed[0]) == [
        "agentihooks",
        "swarm",
        "demo",
        "set",
        "eng-model=opus",
        "eng-effort=max",
        "effort-max=max",
    ]


@pytest.mark.feature("launch-troubleshooting-crew")
def test_a_second_scout_adds_scouts_and_keeps_the_earlier_focus_and_lane(
    crew: ModuleType, capsys: pytest.CaptureFixture
) -> None:
    scouted(crew, capsys)
    ledger = Ledger()
    result = run(crew, ledger, "scout", "demo", "--problem", "p", "--scouts", "2", capsys=capsys)
    assert result["scouts"] == [f"{CID}-scout-6", f"{CID}-scout-7"]
    assert not any(c[-2:] in (["get", "freezes"], ["status", "--json"]) for c in ledger.calls)
    assert [c for c in ledger.writes() if c[3] == "focus"] == [
        focus(f"tasks/{CID}-scout-6", "--bottom"),
        focus(f"tasks/{CID}-scout-7", "--bottom"),
    ]
    assert not any(c[3] == "learned" for c in ledger.writes())
    saved = json.loads(crew.state_path("demo").read_text())
    assert saved["scout_tasks"] == [f"{CID}-scout-{n}" for n in range(1, 8)]
    assert saved["earlier_focus"] == ["phases/p1", "tasks/t2"]


@pytest.mark.feature("launch-troubleshooting-crew")
def test_a_rerun_after_a_failed_launch_adds_only_the_missing_scouts(
    crew: ModuleType, capsys: pytest.CaptureFixture
) -> None:
    broken = Ledger()

    def refuse_third(command: list[str]) -> str:
        if f"{CID}-scout-3" in command and "add" in command:
            raise crew.CommandFailed("agentihooks ledger --slug demo failed: server refused")
        return broken(command)

    with pytest.raises(SystemExit, match="^agentihooks ledger --slug demo failed: server refused$"):
        crew.main(["scout", "demo", "--problem", "p"], runner=refuse_third, now=NOW)
    capsys.readouterr()
    saved = json.loads(crew.state_path("demo").read_text())
    assert (saved["scout_tasks"], saved["launched"]) == ([f"{CID}-scout-1", f"{CID}-scout-2"], False)
    ledger = Ledger()
    run(crew, ledger, "scout", "demo", "--problem", "p", capsys=capsys)
    writes = ledger.writes()
    assert [c[8] for c in writes if c[6:8] == ["task", "add"]] == [f"{CID}-scout-{n}" for n in (3, 4, 5)]
    focused = [c for c in writes if c[3] == "focus"]
    assert focused[0] == focus(f"tasks/{CID}-scout-1", "--only") and len(focused) == 5
    assert writes[-1][3] == "learned"
    saved = json.loads(crew.state_path("demo").read_text())
    assert saved["scout_tasks"] == [f"{CID}-scout-{n}" for n in range(1, 6)] and saved["launched"] is True
    assert saved["earlier_focus"] == ["phases/p1", "tasks/t2"]


@pytest.mark.feature("launch-troubleshooting-crew")
def test_launch_asks_agents_on_other_tasks_to_hand_off(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    agents = [
        {"name": "engineer@1", "lane": "eng", "task": "t5"},
        {"name": "cicd@2", "lane": "ci", "task": "t6"},
        {"name": "master@3", "lane": "master", "task": "t7"},
        {"name": "engineer@4", "lane": "eng", "task": ""},
    ]
    ledger = Ledger(agents=agents)
    run(crew, ledger, "scout", "demo", "--problem", "p", capsys=capsys)
    text = (
        f"The operator's troubleshooting crew {CID} stopped all other work on demo. At your next stop write your "
        "Handoff v2 and run agentihooks swarm demo handoff with it, then stop."
    )
    sends = [c for c in ledger.writes() if c[1:3] == ["msg", "send"]]
    assert sends == [["agentihooks", "msg", "send", name, text] for name in ("engineer@1", "cicd@2")]
    stopped = [{"name": "engineer@1", "task": "t5"}, {"name": "cicd@2", "task": "t6"}]
    assert json.loads(crew.state_path("demo").read_text())["stopped_agents"] == stopped
    assert run(crew, Ledger(), "restore", "demo", capsys=capsys)["stopped_agents"] == stopped


@pytest.mark.feature("launch-troubleshooting-crew")
def test_fixers_wait_until_every_scout_is_done(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys)
    ledger = Ledger(done={f"{CID}-scout-{n}" for n in range(1, 4)}, flagged={f"{CID}-scout-4"})
    message = f"^the scouts {CID}-scout-5 are not done: keep scouting until every cause is proven$"
    with pytest.raises(SystemExit, match=message):
        crew.main(["fix", "demo", "--cause", "hub drops offers"], runner=ledger, now=NOW)
    assert ledger.writes() == []


def test_fixers_need_a_proven_cause(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="^name every proven cause with --cause; a hypothesis is never a cause$"):
        crew.main(["fix", "demo"], runner=Ledger(), now=NOW)


def test_later_stages_refuse_without_a_launched_crew(crew: ModuleType) -> None:
    with pytest.raises(SystemExit, match="^no troubleshooting crew on demo: run the scout stage first$"):
        crew.main(["restore", "demo"], runner=Ledger(), now=NOW)


@pytest.mark.feature("launch-troubleshooting-crew")
def test_fixers_are_added_for_the_proven_causes_and_focused(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys, "--fixers", "2")
    writes = fixed(crew, capsys).writes()
    texts = [
        crew.FIXER_TEXT.format(proven="- c1\n- c2", peers=f"{CID}-fix-2"),
        crew.FIXER_TEXT.format(proven="- c1\n- c2", peers=f"{CID}-fix-1"),
    ]
    assert writes == [
        ["agentihooks", "swarm", "demo", "set", "eng-model=opus", "eng-effort=xhigh"],
        *[
            [
                *ME, "task", "add", f"{CID}-fix-{n}", f"Troubleshooting crew {CID}-fix-{n}",
                "--not-duplicate", "one fixer of the troubleshooting crew working with its peers",
                "--lane", "eng", "--kind", "code", "--rank", "urgent", "--description", texts[n - 1],
            ]
            for n in (1, 2)
        ],
        focus(f"tasks/{CID}-fix-1", "--bottom"),
        focus(f"tasks/{CID}-fix-2", "--bottom"),
    ]  # fmt: skip
    saved = json.loads(crew.state_path("demo").read_text())
    assert (saved["fixer_tasks"], saved["causes"]) == ([f"{CID}-fix-1", f"{CID}-fix-2"], ["c1", "c2"])


@pytest.mark.feature("launch-troubleshooting-crew")
def test_later_stages_keep_the_launch_overrides(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys, "--problem", "launches stall", "--fixers", "2", "--critics", "1")
    for stage in ("critics", "harden"):
        with pytest.raises(SystemExit, match="^no fixer tasks yet: run the fix stage once every cause is proven$"):
            crew.main([stage, "demo"], runner=Ledger(), now=NOW)
    fixed(crew, capsys)
    brief = (
        f"Critic for the troubleshooting crew on: launches stall. Proven causes: c1; c2. Review every pull request of "
        f"the tasks {CID}-fix-1, {CID}-fix-2. Edit nothing; send every finding with its evidence to the fixer that "
        "owns it."
    )
    critics = run(crew, Ledger(), "critics", "demo", capsys=capsys)
    assert critics == {
        "crew": CID,
        "critics": [{"name": "critic-1", "model": "fable", "effort": "xhigh", "prompt": brief}],
    }
    widened = run(crew, Ledger(), "critics", "demo", "--critics", "2", "--critics-model", "opus", capsys=capsys)
    assert [(c["name"], c["model"]) for c in widened["critics"]] == [("critic-1", "opus"), ("critic-2", "opus")]
    assert run(crew, Ledger(), "plan", "demo", "--problem", "p", capsys=capsys)["stages"][1]["count"] == 5


def test_harden_adds_the_lock_task(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys, "--fixers", "1")
    fixed(crew, capsys)
    ledger = Ledger()
    assert run(crew, ledger, "harden", "demo", capsys=capsys) == {"crew": CID, "harden": f"{CID}-harden"}
    text = (
        "Lock the fixed behaviour once the fixes are merged and proven live: feature map tests, Semgrep rules, unit "
        "tests, smoke tests, anti regression tests. Causes fixed: c1; c2"
    )
    assert ledger.writes() == [
        [
            *ME, "task", "add", f"{CID}-harden", f"Troubleshooting crew {CID}-harden",
            "--not-duplicate", "the hardening task of one troubleshooting crew",
            "--lane", "eng", "--kind", "code", "--rank", "urgent", "--description", text,
        ],
        focus(f"tasks/{CID}-harden", "--bottom"),
    ]  # fmt: skip


def test_harden_refuses_a_crew_launched_without_hardening(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys, "--no-harden")
    ledger = Ledger()
    with pytest.raises(SystemExit, match="^this crew was launched with --no-harden$"):
        crew.main(["harden", "demo"], runner=ledger, now=NOW)
    assert ledger.calls == []


@pytest.mark.feature("launch-troubleshooting-crew")
def test_restore_brings_back_the_earlier_focus_and_lane(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys, "--fixers", "1")
    fixed(crew, capsys)
    run(crew, Ledger(), "harden", "demo", capsys=capsys)
    ledger = Ledger()
    result = run(crew, ledger, "restore", "demo", "--quote", "crew is over", capsys=capsys)
    ended = ("troubleshooting crew ended", "crew is over")
    crew_tasks = [*[f"{CID}-scout-{n}" for n in range(1, 6)], f"{CID}-fix-1", f"{CID}-harden"]
    words = ["--reason", ended[0], "--quote", ended[1]]
    assert ledger.writes() == [
        [
            "agentihooks",
            "swarm",
            "demo",
            "set",
            "eng-agent=claude",
            "eng-model=sonnet",
            "eng-effort=high",
            "effort-max=xhigh",
        ],
        *[["agentihooks", "swarm", "demo", "unfreeze", f"tasks/{t}", *words] for t in crew_tasks],
        focus("phases/p1", "--bottom", *ended),
        focus("tasks/t2", "--bottom", *ended),
    ]
    assert (result["restored_focus"], result["skipped_focus"]) == (["phases/p1", "tasks/t2"], [])
    assert result["restored_lane"] == {"agent": "claude", "model": "sonnet", "effort": "high", "effort_max": "xhigh"}


@pytest.mark.feature("launch-troubleshooting-crew")
def test_restore_skips_focus_targets_done_or_gone(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys)
    ledger = Ledger(done={"t2"}, gone={"phases/p1"})
    result = run(crew, ledger, "restore", "demo", capsys=capsys)
    assert (result["restored_focus"], result["skipped_focus"]) == ([], ["phases/p1", "tasks/t2"])
    assert not any(c[3] == "focus" for c in ledger.writes())
    flagged = Ledger(flagged={"t2"})
    assert crew.alive("demo", "tasks/t2", flagged) is False and crew.alive("demo", "phases/p1", flagged) is True


@pytest.mark.feature("launch-troubleshooting-crew")
def test_a_scout_after_a_restore_starts_a_new_crew(crew: ModuleType, capsys: pytest.CaptureFixture) -> None:
    scouted(crew, capsys)
    run(crew, Ledger(), "restore", "demo", capsys=capsys)
    assert not crew.state_path("demo").exists()
    assert crew.state_path("demo").with_name(f"troubleshooting-crew-{CID}.json").exists()
    ledger = Ledger()
    result = run(crew, ledger, "scout", "demo", "--problem", "q", capsys=capsys)
    assert any(c[-2:] == ["get", "freezes"] for c in ledger.calls)
    assert result["scouts"][0] == f"{CID}-scout-1"
    assert [c for c in ledger.writes() if c[3] == "focus"][0] == focus(f"tasks/{CID}-scout-1", "--only")


def test_state_lives_under_the_agentihooks_home(crew: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    assert crew.state_path("demo") == Path.home() / ".agentihooks" / "swarm" / "demo" / "troubleshooting-crew.json"


def test_run_returns_output_and_raises_on_a_failure(crew: ModuleType) -> None:
    assert crew.run(["sh", "-c", "echo out"]) == "out\n"
    with pytest.raises(crew.CommandFailed, match="^sh -c echo bad >&2; exit 3 failed: bad$"):
        crew.run(["sh", "-c", "echo bad >&2; exit 3"])
    with pytest.raises(crew.CommandFailed, match="^sh -c echo shown; exit 1 failed: shown$"):
        crew.run(["sh", "-c", "echo shown; exit 1"])
