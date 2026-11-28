from pathlib import Path

import pytest

from scripts import operator_env
from scripts.quick import authentication
from scripts.quick.guard import QuickError

pytestmark = pytest.mark.unit


@pytest.fixture
def authentication_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    def fill(active: dict[str, str]) -> None:
        active.update({"AH_CC_TOKEN_alpha": "claude-example", "AH_CX_TOKEN_beta": "codex-example"})

    monkeypatch.setattr(operator_env, "fill", fill)
    return {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "state")}


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("routed", [("routed", ""), ("routed", "")]),
        ("default", [("default", ""), ("default", "")]),
        ("named:alpha,named:beta", [("named", "alpha"), ("named", "beta")]),
        ("named:AH_CC_TOKEN_alpha,named:AH_CX_TOKEN_beta", [("named", "alpha"), ("named", "beta")]),
        ("default,named:beta", [("default", ""), ("named", "beta")]),
        ("named:alpha,routed", [("named", "alpha"), ("routed", "")]),
    ],
)
def test_common_and_per_engineer_native_authentication_resolve_safe_references(
    value: str, expected: list[tuple[str, str]], authentication_env: dict[str, str]
) -> None:
    assert authentication.selections(value, ["claude", "codex"], authentication_env) == expected
    assert "AH_CC_TOKEN_alpha" not in authentication_env


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    ("value", "harnesses", "message"),
    [
        ("default,routed,default", ["claude", "codex"], "authentication needs one choice or 2 engineer choices"),
        ("named:", ["claude"], "authentication must be routed, default or named:reference"),
        ("interactive", ["codex"], "authentication must be routed, default or named:reference"),
        ("", ["claude"], "authentication must be routed, default or named:reference"),
        ("named:missing", ["claude"], "named Claude authentication reference is unavailable"),
        ("named:missing", ["codex"], "named Codex authentication reference is unavailable"),
        ("named:beta", ["claude"], "named Claude authentication reference is unavailable"),
        ("named:alpha", ["codex"], "named Codex authentication reference is unavailable"),
    ],
)
def test_invalid_native_authentication_reports_the_exact_refusal(
    value: str, harnesses: list[str], message: str, authentication_env: dict[str, str]
) -> None:
    with pytest.raises(QuickError) as error:
        authentication.selections(value, harnesses, authentication_env)
    assert str(error.value) == message
