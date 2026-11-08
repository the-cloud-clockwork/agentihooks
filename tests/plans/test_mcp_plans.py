import json
from pathlib import Path
from typing import Any

import pytest

from hooks.mcp import plans
from scripts.plans import ingest
from tests.plans.test_ingest import MARKED, SEAT, plan_ledger, served

pytestmark = pytest.mark.xdist_group("mcp-sdk")

__all__ = ["plan_ledger", "served"]


@pytest.fixture
def seat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    for key, value in SEAT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("hooks.mcp._session._ENV_FALLBACK_ALLOWED", True)


def test_a_network_transport_refuses_before_any_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hooks.mcp._session._ENV_FALLBACK_ALLOWED", False)
    monkeypatch.setattr(ingest, "ingest", lambda args, env: pytest.fail("ingest ran under a network transport"))
    assert plans.run("", MARKED, "plan-proof", "", True) == {"success": False, "error": plans.NETWORK}


@pytest.mark.parametrize(
    "path,text,planning,error",
    [
        ("", "", "", "pass exactly one of path or text"),
        ("plan.md", MARKED, "", "pass exactly one of path or text"),
        ("", MARKED, "later", "planning is auto, manual or empty"),
        ("-", "", "", "standard input is the MCP channel; pass the plan as text"),
    ],
)
def test_arguments_are_refused_before_any_ingest(
    monkeypatch: pytest.MonkeyPatch, seat: None, path: str, text: str, planning: str, error: str
) -> None:
    monkeypatch.setattr(ingest, "ingest", lambda args, env: pytest.fail("ingest ran on refused arguments"))
    assert plans.run(path, text, "plan-proof", planning, False) == {"success": False, "error": error}


def test_inline_text_reaches_ingest_as_a_file_with_the_command_defaults_and_its_output_on_stderr(
    monkeypatch: pytest.MonkeyPatch, seat: None, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = []

    def fake(args: Any, env: Any) -> dict[str, Any]:
        seen.append((Path(args.path).name, Path(args.path).read_bytes(), args.planning, args.repo))
        print("ingest chatter")
        return {"plan": "p", "phases": []}

    monkeypatch.setattr(ingest, "ingest", fake)
    assert plans.run("", "plan é", "plan-proof", "", True) == {
        "success": True,
        "plan": "p",
        "phases": [],
        "state": "dry run",
    }
    assert seen == [("inline.md", "plan é".encode(), "", "")]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ingest chatter\n"


def test_the_tool_defaults_to_no_path_no_text_own_planning_and_a_real_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(plans, "run", lambda *args: calls.append(args) or {"success": True})
    assert plans.plan_ingest("plan-proof") == '{"success": true}'
    assert calls == [("", "", "plan-proof", "", False)]


def test_inline_text_dry_run_writes_nothing(served: dict[str, Any], seat: None) -> None:
    result = plans.run("", MARKED, "plan-proof", "", True)
    assert served["sent"] == []
    assert result == {
        "success": True,
        "dry_run": True,
        "plan": ingest.plan_of(ingest.normalize(MARKED, "inline.md").markdown),
        "by": "planner",
        "phases": ["alpha", "beta"],
        "ops": 5,
        "state": "dry run",
    }


def test_a_path_applies_once_and_a_rerun_reports_already_ingested(
    served: dict[str, Any], seat: None, tmp_path: Path
) -> None:
    path = tmp_path / "plan.md"
    path.write_text(MARKED, encoding="utf-8")
    applied = plans.run(str(path), "", "plan-proof", "manual", False)
    assert applied["success"] is True
    assert applied["ingested"] is True
    assert applied["phases"] == ["alpha", "beta"]
    assert applied["state"] == "absent"
    assert "swarm" not in applied
    again = plans.run(str(path), "", "plan-proof", "", False)
    assert again == {
        "success": True,
        "plan": applied["plan"],
        "phases": ["alpha", "beta"],
        "lifecycle": again["lifecycle"],
        "ingested": False,
        "note": "already ingested",
        "state": "absent",
    }


def test_a_seat_of_another_swarm_is_refused(
    served: dict[str, Any], seat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
    assert plans.run("", MARKED, "plan-proof", "", False) == {
        "success": False,
        "error": "a seat of swarm other cannot ingest a plan into ledger plan-proof",
    }
    assert served["sent"] == []


def test_a_missing_path_is_refused(served: dict[str, Any], seat: None, tmp_path: Path) -> None:
    result = plans.run(str(tmp_path / "absent.md"), "", "plan-proof", "", True)
    assert result["success"] is False
    assert "absent.md" in result["error"]


def test_the_server_serves_plan_ingest_from_the_plans_category(served: dict[str, Any], seat: None) -> None:
    from hooks.mcp import build_server

    server = build_server(["plans"])
    tool = server._tool_manager._tools["plan_ingest"]
    assert set(server._tool_manager._tools) == {"plan_ingest"}
    reply = json.loads(tool.fn(ledger="plan-proof", text=MARKED, dry_run=True))
    assert (reply["success"], reply["phases"], reply["state"]) == (True, ["alpha", "beta"], "dry run")
