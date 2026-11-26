from pathlib import Path

import pytest

from scripts import codex_router
from scripts.routing import native_default

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    ("agent", "variable", "filename"),
    [("claude", "CLAUDE_CONFIG_DIR", ".credentials.json"), ("codex", "CODEX_HOME", "auth.json")],
)
def test_native_default_isolates_local_login_from_master_and_routed_credentials(
    agent: str, variable: str, filename: str, tmp_path: Path
) -> None:
    operator = tmp_path / f".{agent}"
    operator.mkdir()
    (operator / filename).write_bytes(b"local-login-fixture")
    master = tmp_path / "master"
    master.mkdir()
    (master / filename).write_bytes(b"master-login-fixture")
    (master / "settings.json").write_text("{}")
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "state"),
        variable: str(master),
        "AH_CC_TOKEN_alpha": "unused",
        "AH_CX_TOKEN_beta": "unused",
        "CLAUDE_CODE_OAUTH_TOKEN": "unused",
        "OPENAI_API_KEY": "unused",
        "ANTHROPIC_API_KEY": "unused",
        "AH_INTERACTIVE_master": "1",
    }
    child = native_default.prepare(agent, environ)
    home = Path(child[variable])
    assert home != master and home != operator
    assert (home / filename).read_bytes() == b"local-login-fixture"
    assert not (home / filename).is_symlink()
    assert (home / "settings.json").read_text() == "{}"
    assert (operator / filename).read_bytes() == b"local-login-fixture"
    assert (master / filename).read_bytes() == b"master-login-fixture"
    assert not any(name.startswith(("AH_CC_TOKEN_", "AH_CX_TOKEN_", "AH_INTERACTIVE_")) for name in child)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
    assert "OPENAI_API_KEY" not in child and "ANTHROPIC_API_KEY" not in child


@pytest.mark.feature("quick-native-authentication")
def test_codex_native_default_bypasses_automatic_account_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_route(*args: object, **kwargs: object) -> None:
        pytest.fail("native default enumerated routed accounts")

    monkeypatch.setattr(codex_router, "_route", refuse_route)
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run: True)
    from scripts import deps_preflight

    monkeypatch.setattr(deps_preflight, "refresh_codex_wrapper", lambda name: None)
    operator = tmp_path / ".codex"
    operator.mkdir()
    (operator / "auth.json").write_bytes(b"local-login-fixture")
    calls: list[dict[str, str]] = []

    def execute(binary: str, args: list[str], environ: dict[str, str]) -> None:
        calls.append(environ)

    assert (
        codex_router.main(
            ["--route", "default"],
            {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "state")},
            execvpe=execute,
        )
        == 0
    )
    assert len(calls) == 1
    assert Path(calls[0]["CODEX_HOME"]) != operator
