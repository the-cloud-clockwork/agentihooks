from pathlib import Path

import pytest

from scripts import codex_router, deps_preflight
from scripts.native_settings import native_settings
from scripts.targets import codex_target
from scripts.targets._common import _install_module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _env_layer(path: Path, value: str) -> None:
    _write(path / ".codex" / "config.overrides.toml", f'[_agentihooks.env]\nAGENTIHOOKS_CLASSIFIER_URL = "{value}"\n')


@pytest.fixture
def installed(tmp_path, monkeypatch):
    install = _install_module()
    monkeypatch.setattr(install, "install_root", lambda: install.AGENTIHOOKS_ROOT)
    monkeypatch.setattr(install, "_detect_venv", lambda: tmp_path / "venv" / "bin" / "python")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    bundle = tmp_path / "bundle"
    _write(bundle / "profiles" / "wr-base" / "profile.yml", "name: wr-base\n")
    _write(bundle / "profiles" / "wr-role" / "profile.yml", "name: wr-role\nextends: [wr-base]\n")
    _env_layer(bundle / "profiles" / "wr-base", "http://old:4000")
    install._save_state({"bundle": {"path": str(bundle)}, "targets": {"global": {"codex": {"profile": "wr-role"}}}})
    home = codex_target.codex_home()
    home.mkdir(parents=True, exist_ok=True)
    native = native_settings("codex", install._resolve_profile_chain("wr-role"), None, say=lambda *_: None)
    codex_target.CodexAdapter().write_settings(native)
    return {"bundle": bundle, "home": home, "install": install}


def _untouched(home: Path) -> dict[str, bytes]:
    return {name: (home / name).read_bytes() for name in ("config.toml", "hooks.json", ".agentihooks-managed.json")}


def test_a_bundle_env_change_rerenders_only_the_wrapper(installed):
    home = installed["home"]
    wrapper = home / "agentihooks-hook.sh"
    assert "http://old:4000" in wrapper.read_text()
    before = _untouched(home)
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")

    assert codex_target.refresh_wrapper() is True

    text = wrapper.read_text()
    assert 'export AGENTIHOOKS_CLASSIFIER_URL="${AGENTIHOOKS_CLASSIFIER_URL:=http://new:4000}"\n' in text
    assert "http://old:4000" not in text
    assert wrapper.stat().st_mode & 0o777 == 0o755
    assert _untouched(home) == before
    assert codex_target.refresh_wrapper() is False


def test_the_refreshed_wrapper_matches_what_init_writes(installed):
    _env_layer(installed["bundle"] / "profiles" / "wr-role", "http://role:4000")
    codex_target.refresh_wrapper()
    refreshed = (installed["home"] / "agentihooks-hook.sh").read_text()
    install = installed["install"]
    native = native_settings("codex", install._resolve_profile_chain("wr-role"), None, say=lambda *_: None)
    codex_target.CodexAdapter().write_settings(native)
    assert (installed["home"] / "agentihooks-hook.sh").read_text() == refreshed
    assert "http://role:4000" in refreshed


def test_the_settings_profile_layer_reaches_the_wrapper(installed):
    install = installed["install"]
    _write(installed["bundle"] / "profiles" / "wr-settings" / "profile.yml", "name: wr-settings\n")
    _env_layer(installed["bundle"] / "profiles" / "wr-settings", "http://settings:4000")
    state = install._load_state()
    state["targets"]["global"]["codex"]["settings_profile"] = "wr-settings"
    install._save_state(state)
    assert codex_target.refresh_wrapper() is True
    assert "http://settings:4000" in (installed["home"] / "agentihooks-hook.sh").read_text()


def test_no_codex_install_record_writes_nothing(installed):
    install = installed["install"]
    install._save_state({"bundle": {"path": str(installed["bundle"])}})
    wrapper = installed["home"] / "agentihooks-hook.sh"
    old = wrapper.read_text()
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is False
    assert wrapper.read_text() == old


def test_no_wrapper_yet_writes_nothing(installed):
    wrapper = installed["home"] / "agentihooks-hook.sh"
    wrapper.unlink()
    assert codex_target.refresh_wrapper() is False
    assert not wrapper.exists()


def test_a_chain_with_no_resolvable_profile_writes_nothing(installed):
    install = installed["install"]
    install._save_state(
        {"bundle": {"path": str(installed["bundle"])}, "targets": {"global": {"codex": {"profile": "wr-gone"}}}}
    )
    wrapper = installed["home"] / "agentihooks-hook.sh"
    old = wrapper.read_text()
    assert codex_target.refresh_wrapper() is False
    assert wrapper.read_text() == old


def test_a_missing_settings_profile_writes_nothing(installed):
    install = installed["install"]
    state = install._load_state()
    state["targets"]["global"]["codex"]["settings_profile"] = "wr-gone"
    install._save_state(state)
    wrapper = installed["home"] / "agentihooks-hook.sh"
    old = wrapper.read_text()
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is False
    assert wrapper.read_text() == old


def test_a_session_in_a_rendered_home_refreshes_the_global_wrapper(installed, monkeypatch):
    from scripts.profiles import render

    rendered = render.rendered_root() / "wr-role" / "codex"
    rendered.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(rendered))
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is True
    assert "http://new:4000" in (installed["home"] / "agentihooks-hook.sh").read_text()
    assert not (rendered / "agentihooks-hook.sh").exists()
    assert [p.name for p in installed["home"].iterdir() if ".tmp-" in p.name] == []


def test_a_launch_from_another_python_keeps_the_wrapper_interpreter(installed, monkeypatch, tmp_path):
    install = installed["install"]
    monkeypatch.setattr(install, "_detect_venv", lambda: tmp_path / "other" / "bin" / "python")
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is True
    last = (installed["home"] / "agentihooks-hook.sh").read_text().splitlines()[-1]
    assert last == f"AGENTIHOOKS_TARGET=codex exec {tmp_path / 'venv' / 'bin' / 'python'} -m hooks"


def test_an_unrecognised_wrapper_is_left_alone(installed):
    wrapper = installed["home"] / "agentihooks-hook.sh"
    wrapper.write_text("#!/usr/bin/env bash\nexec python -m my hooks\n")
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is False
    assert wrapper.read_text() == "#!/usr/bin/env bash\nexec python -m my hooks\n"


@pytest.mark.parametrize(
    "text, python",
    [
        ("", None),
        ("AGENTIHOOKS_TARGET=codex exec /py -m hooks", "/py"),
        ("AGENTIHOOKS_TARGET=codex exec /py -m hooks\n", "/py"),
        ("AGENTIHOOKS_TARGET=codex exec /py -m hooks\n\n", None),
        ("set -e\nAGENTIHOOKS_TARGET=codex exec /py -m hooks\n", "/py"),
        ("AGENTIHOOKS_TARGET=codex exec /py -m hooks\nset -e\n", None),
        ("AGENTIHOOKS_TARGET=codex exec /py -m\n", None),
        ("one two three four five\n", None),
    ],
)
def test_the_wrapper_interpreter_is_read_only_from_its_exec_line(text, python):
    assert codex_target._wrapper_python(text) == python


def test_the_wrapper_text_is_the_exact_managed_script(installed, tmp_path):
    install = installed["install"]
    text = codex_target.wrapper_text({"A": "b c", "D": "e"}, "/opt/py thon")
    assert text == (
        "#!/usr/bin/env bash\n"
        "# managed-by: agentihooks — regenerate with: agentihooks init --target codex\n"
        "set -euo pipefail\n"
        f"cd {install.AGENTIHOOKS_ROOT}\n"
        "export A=\"${A:='b c'}\"\n"
        'export D="${D:=e}"\n'
        "AGENTIHOOKS_TARGET=codex exec '/opt/py thon' -m hooks\n"
    )


def test_a_refresh_prints_nothing(installed, capsys):
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    capsys.readouterr()
    assert codex_target.refresh_wrapper() is True
    assert capsys.readouterr().out == ""


def test_a_run_from_another_checkout_leaves_the_wrapper(installed, monkeypatch, tmp_path):
    install = installed["install"]
    monkeypatch.setattr(install, "install_root", lambda: tmp_path / "elsewhere")
    wrapper = installed["home"] / "agentihooks-hook.sh"
    old = wrapper.read_text()
    _env_layer(installed["bundle"] / "profiles" / "wr-base", "http://new:4000")
    assert codex_target.refresh_wrapper() is False
    assert wrapper.read_text() == old


def test_deps_ensure_refreshes_the_wrapper_before_its_stamp_check(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_target, "refresh_wrapper", lambda: calls.append("refresh") or True)
    monkeypatch.setattr("scripts.skill_links.repair", lambda: None)
    assert deps_preflight.ensure(quiet=True) == 0
    assert calls == ["refresh"]


def test_codex_launch_refreshes_the_wrapper_before_exec(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(codex_target, "refresh_wrapper", lambda: order.append("refresh") or False)
    monkeypatch.setattr(codex_router, "_route", lambda *args: (codex_router.CodexAccount("default"), "open", 0, 2))
    monkeypatch.setattr(codex_router, "credential", lambda account, environ: account)
    monkeypatch.setattr(codex_router.shutil, "which", lambda name: f"/usr/bin/{name}")
    environ = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    rc = codex_router.main([], environ=environ, execvpe=lambda *args: order.append("exec"))
    assert rc == 0
    assert order == ["refresh", "exec"]


def test_a_failed_refresh_never_stops_a_codex_launch(monkeypatch, tmp_path, capsys):
    def broken():
        raise ValueError("linked bundle is missing")

    launched = []
    monkeypatch.setattr(codex_target, "refresh_wrapper", broken)
    monkeypatch.setattr(codex_router, "_route", lambda *args: (codex_router.CodexAccount("default"), "open", 0, 2))
    monkeypatch.setattr(codex_router, "credential", lambda account, environ: account)
    monkeypatch.setattr(codex_router.shutil, "which", lambda name: f"/usr/bin/{name}")
    environ = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    assert codex_router.main([], environ=environ, execvpe=lambda *args: launched.append(1)) == 0
    assert launched == [1]
    assert "[agentihooks codex] hook wrapper refresh skipped: linked bundle is missing\n" in capsys.readouterr().err


def test_a_failed_refresh_never_stops_deps_ensure(monkeypatch, capsys):
    def broken():
        raise ValueError("Profile 'wr-role' not found")

    monkeypatch.setattr(codex_target, "refresh_wrapper", broken)
    monkeypatch.setattr("scripts.skill_links.repair", lambda: None)
    assert deps_preflight.ensure(quiet=True) == 0
    assert "[agentihooks deps] hook wrapper refresh skipped: Profile 'wr-role' not found\n" in capsys.readouterr().err
