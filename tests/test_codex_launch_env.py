import os

import pytest

from scripts import codex_router, install

SHELL_KEY = "AGENTIHOOKS_HANDOFF_WEEK_PCT"
FILE_KEY = "AGENTIHOOKS_HANDOFF_5H_PCT"
COMPANION_KEY = "AGENTIHOOKS_WAIT_MIN_WEEK_LEFT"
BARE_KEY = "AGENTIHOOKS_HANDOFF_MIN_LEFT"


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    main_file = tmp_path / "stack.env"
    main_file.write_text(f"{SHELL_KEY}=98\n{FILE_KEY}=99\n{BARE_KEY}\n")
    (tmp_path / "quota.env").write_text(f"{SHELL_KEY}=97\n{COMPANION_KEY}=10\n")
    monkeypatch.setattr(install, "_ENV_FILE_DST", main_file)
    monkeypatch.setenv(SHELL_KEY, "5")
    for key in (FILE_KEY, COMPANION_KEY, BARE_KEY):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def test_codex_launch_keeps_a_shell_set_threshold_and_fills_unset_ones_from_env_files(env_files, monkeypatch):
    seen = {}
    monkeypatch.setattr(codex_router, "main", lambda args: seen.update(os.environ, args=args) or 0)
    monkeypatch.setattr("sys.argv", ["agentihooks", "codex", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        install.main()

    assert exit_info.value.code == 0
    assert seen["args"] == ["--version"]
    assert seen[SHELL_KEY] == "5"
    assert seen[FILE_KEY] == "99"
    assert seen[COMPANION_KEY] == "10"
    assert BARE_KEY not in seen


def test_runtime_env_lets_env_files_override_the_shell_by_default(env_files):
    install._load_claude_runtime_env()

    assert os.environ[SHELL_KEY] == "97"
    assert os.environ[FILE_KEY] == "99"
    assert os.environ[COMPANION_KEY] == "10"
    assert BARE_KEY not in os.environ
