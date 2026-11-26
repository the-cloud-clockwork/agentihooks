from pathlib import Path
from typing import Any

import pytest

from scripts.gates import first_read


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "agentihooks"))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@1")


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "response",
    [
        {"exit_code": 0},
        {"stdout": "brief", "stderr": "", "interrupted": False},
        "Process exited with code 0\nFinal output:\nbrief",
        {"content": [{"type": "text", "text": '{"exit_code": 0, "output": "brief"}'}]},
    ],
)
def test_successful_brief_marks_its_session(response: Any) -> None:
    first_read.mark(
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": response,
        }
    )
    assert first_read.marker_path("one").read_text() == "1"
    assert not first_read.marker_path("two").exists()


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "response",
    [
        {"exit_code": 1},
        {"exit_code": 0, "session_id": 123},
        {"interrupted": True},
        {"isError": True},
        "Process exited with code 1\n",
        {"output": "brief"},
    ],
)
def test_unsuccessful_or_running_brief_does_not_mark(response: Any) -> None:
    first_read.mark(
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": response,
        }
    )
    assert not first_read.marker_path("one").exists()


@pytest.mark.feature("first-read-gate")
def test_unrelated_command_and_standalone_do_not_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "session_id": "one",
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "tool_response": {"exit_code": 0},
    }
    first_read.mark(payload)
    assert not first_read.marker_path("one").exists()
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    payload["tool_input"] = {"command": "agentihooks seat brief"}
    first_read.mark(payload)
    assert not first_read.marker_path("one").exists()


@pytest.mark.feature("first-read-gate")
def test_compaction_clears_only_current_session() -> None:
    for session in ("one", "two"):
        path = first_read.marker_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1")
    first_read.reset({"session_id": "one", "source": "startup"})
    assert first_read.marker_path("one").exists()
    first_read.reset({"session_id": "one", "source": "compact"})
    assert not first_read.marker_path("one").exists()
    assert first_read.marker_path("two").read_text() == "1"


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "response",
    [
        'Script completed\nWall time: 1 seconds\nOutput:\n{"exit_code": 0, "output": "brief"}',
        {"type": "text", "text": '{"exit_code": 0}'},
    ],
)
def test_codex_code_mode_success_marks(response: Any) -> None:
    first_read.mark(
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": 'const r = await tools.exec_command({cmd: "agentihooks seat brief"}); text(r);'},
            "tool_response": response,
        }
    )
    assert first_read.marker_path("one").read_text() == "1"


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"command": "agentihooks seat brief"}, "tool_response": {"exit_code": 0}},
        {"tool_name": "Bash", "tool_input": {"command": "agentihooks seat brief"}, "tool_response": {"exit_code": 0}},
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": None,
        },
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": {"is_error": True},
        },
        {
            "session_id": "one",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": {"content": [{"type": "text", "text": '{"exit_code": 1}'}]},
        },
    ],
)
def test_missing_or_unsuccessful_call_never_marks(payload: dict[str, Any]) -> None:
    first_read.mark(payload)
    assert not first_read.marker_path("one").exists()
    first_read.reset({"source": "compact"})
    assert not first_read.marker_path("one").exists()
