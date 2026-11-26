from pathlib import Path

import pytest

from scripts import init_agent
from scripts.quick import launch, registry

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(
    ("mode", "account", "tail"),
    [
        ("routed", "", ["--routed-only"]),
        ("default", "", ["--auth", "default"]),
        ("named", "alpha", ["--auth", "named:alpha"]),
    ],
)
def test_saved_native_authentication_reaches_the_launcher(
    harness: str, mode: str, account: str, tail: list[str], tmp_path: Path
) -> None:
    seat = registry.Seat(2, "eng-1", "w1:p2", harness, "quick-proof-eng-1", auth=mode, account=account)
    swarm = registry.Swarm("quick-proof", str(tmp_path), (seat,))
    prompt = tmp_path / "prompt.md"
    assert launch.argv(swarm, seat, prompt) == [
        "--host",
        "herdr",
        "--pane",
        "w1:p2",
        "--name",
        "quick-proof-eng-1",
        "--agent",
        harness,
        "--dir",
        str(tmp_path),
        "--prompt-file",
        str(prompt),
        *tail,
    ]


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(("selection", "route"), [("default", "default"), ("named:alpha", "alpha")])
def test_native_init_keeps_explicit_authentication_without_automatic_selection(
    harness: str,
    selection: str,
    route: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse_router(*args: object, **kwargs: object) -> None:
        pytest.fail("explicit native authentication called automatic selection")

    monkeypatch.setattr(init_agent.agent_choice, "choose", refuse_router)
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(init_agent, "_bind_brain", lambda environ: None)
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "state"),
        "AH_CC_TOKEN_alpha": "unused",
        "AH_CX_TOKEN_alpha": "unused",
    }
    assert (
        init_agent.main(
            ["--dry-run", "--host", "herdr", "--agent", harness, "--auth", selection, "--dir", str(tmp_path)],
            environ=environ,
        )
        == 0
    )
    output = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line)
    assert output["agent"] == harness
    assert f"--route {route}" in Path(output["launcher"]).read_text()
