import tomllib
from pathlib import Path

import pytest

from scripts.swarm import runtime
from scripts.swarm_ledger.api.client import export_file


@pytest.mark.feature("codex-planner-export-grant")
def test_planner_keeps_existing_write_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: Path("/process"))
    options = runtime.codex_plan_mode({"HOME": "/launch", "LEDGER_DIR": "/ledger"})
    planner = tomllib.loads(options[1])["permissions"]["planner"]
    assert planner["extends"] == ":read-only"
    assert planner["network"] == {"enabled": True}
    assert {
        root: planner["filesystem"][root] for root in ("/ledger", "/launch/.agentihooks/swarm", "/launch/scratchpad")
    } == {
        "/ledger": "write",
        "/launch/.agentihooks/swarm": "write",
        "/launch/scratchpad": "write",
    }
    assert options[::2] == ["-c", "-c"]
    assert options[3] == 'default_permissions="planner"'


@pytest.mark.feature("codex-planner-export-grant")
@pytest.mark.parametrize(
    ("override", "exports"),
    [
        (None, "/process/.agentihooks/ledger-exports"),
        ("", "/process/.agentihooks/ledger-exports"),
        ("/state", "/state/ledger-exports"),
        ("~/state", "~/state/ledger-exports"),
        ("relative", "relative/ledger-exports"),
        ('/state"quoted', '/state"quoted/ledger-exports'),
    ],
    ids=["default", "empty", "override", "tilde", "relative", "quoted"],
)
def test_planner_grants_the_client_export_directory(
    monkeypatch: pytest.MonkeyPatch, override: str | None, exports: str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: Path("/process"))
    environ = {"HOME": "/launch", "LEDGER_DIR": "/ledger"}
    if override is not None:
        environ["AGENTIHOOKS_HOME"] = override
    options = runtime.codex_plan_mode(environ)
    assert options[::2] == ["-c", "-c"]
    assert options[3] == 'default_permissions="planner"'
    assert str(export_file("proof", environ).parent) == exports
    assert tomllib.loads(options[1]) == {
        "permissions": {
            "planner": {
                "extends": ":read-only",
                "network": {"enabled": True},
                "filesystem": {
                    "/ledger": "write",
                    "/launch/.agentihooks/swarm": "write",
                    "/launch/scratchpad": "write",
                    exports: "write",
                },
            }
        }
    }
