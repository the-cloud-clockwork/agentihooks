import json

import pytest

from scripts.targets import codex_hook_trust
from tests.codex_hook_fixtures import CODEX_HOOK_HASHES, CODEX_HOOKS


def test_every_command_entry_gets_the_hash_codex_computes(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps(CODEX_HOOKS))
    key_path = tmp_path / "rendered" / "hooks.json"

    state = codex_hook_trust.trusted_state(hooks, key_path)

    assert state == {f"{key_path}:{key}": {"trusted_hash": value} for key, value in CODEX_HOOK_HASHES.items()}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"hooks": []}',
        '{"hooks": {"Notification": [{"hooks": [{"type": "command", "command": "n"}]}]}}',
        '{"hooks": {"Stop": {"hooks": []}}}',
        '{"hooks": {"Stop": [{"matcher": "m"}]}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t"}]}]}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}}',
        '{"hooks": {"Stop": ["x"]}}',
        '{"hooks": {"Stop": [{"hooks": "x"}]}}',
        '{"hooks": {"Stop": [{"hooks": ["x"]}]}}',
    ],
)
def test_entries_codex_does_not_run_as_commands_get_no_trust(tmp_path, text):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(text)
    assert codex_hook_trust.trusted_state(hooks, hooks) == {}


def test_a_missing_hooks_file_trusts_nothing(tmp_path):
    assert codex_hook_trust.trusted_state(tmp_path / "hooks.json", tmp_path / "hooks.json") == {}


def test_handler_position_names_the_key_after_skipped_groups(tmp_path):
    hooks = tmp_path / "hooks.json"
    stop = CODEX_HOOKS["hooks"]["Stop"]
    hooks.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "m"}, *stop]}}))
    assert codex_hook_trust.trusted_state(hooks, hooks) == {
        f"{hooks}:stop:1:0": {"trusted_hash": CODEX_HOOK_HASHES["stop:0:0"]},
        f"{hooks}:stop:2:0": {"trusted_hash": CODEX_HOOK_HASHES["stop:1:0"]},
    }
