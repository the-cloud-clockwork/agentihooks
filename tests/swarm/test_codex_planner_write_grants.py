import json
import tempfile
import tomllib
from pathlib import Path

import pytest

from scripts.hive import daemon
from scripts.profiles import connectors
from scripts.swarm import brain_binding, runtime
from scripts.targets.codex_target import codex_mcp_entry
from tests.swarm.test_runtime import _passed, _spawn_seen

BOUND = {
    "status": brain_binding.BOUND,
    "mcp": "https://brain.example.test/mcp",
    "mcp_header": "X-API-Key",
    "mcp_scheme": "",
    "mcp_env": "SWARM_BRAIN_MCP_KEY",
}


def _granted(options: list[str]) -> dict[str, str]:
    return tomllib.loads(options[1])["permissions"]["planner"]["filesystem"]


def _primary(root: Path) -> Path:
    checkout = root / "dev" / "repo"
    (checkout / ".git").mkdir(parents=True)
    return checkout


@pytest.mark.feature("codex-planner-write-grants")
def test_a_planner_writes_its_checkout_git_metadata_but_not_its_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    checkout = _primary(tmp_path)
    granted = _granted(runtime.codex_plan_mode({"HOME": str(tmp_path / "home")}, str(checkout)))
    assert granted[str((checkout / ".git").resolve())] == "write"
    assert [root for root in granted if checkout.is_relative_to(root)] == []


@pytest.mark.feature("codex-planner-write-grants")
def test_a_planner_writes_the_common_git_dir_of_a_linked_worktree_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    primary = _primary(tmp_path)
    admin = primary / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    linked = tmp_path / "worktrees" / "repo" / "wt"
    linked.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {admin}\n")
    granted = _granted(runtime.codex_plan_mode({"HOME": str(tmp_path / "home")}, str(linked)))
    assert granted[str((primary / ".git").resolve())] == "write"
    assert str(linked / ".git") not in granted


@pytest.mark.feature("codex-planner-write-grants")
@pytest.mark.parametrize(
    ("environ", "root"),
    [
        ({"HOME": "/h"}, "/h/dev/worktrees"),
        ({"HOME": "/h", "WORKTREE_ROOT": "/w"}, "/w"),
        ({"HOME": "/h", "WORKTREE_ROOT": ""}, "/h/dev/worktrees"),
    ],
    ids=["default", "override", "empty"],
)
def test_a_planner_writes_the_worktree_root_wt_sh_uses(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], root: str
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", "/t")
    granted = _granted(runtime.codex_plan_mode(environ, "/repo"))
    assert granted[root] == "write"
    assert "/repo" not in granted


@pytest.mark.feature("codex-planner-write-grants")
def test_a_planner_writes_the_launch_temp_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", "/launch-tmp")
    assert _granted(runtime.codex_plan_mode({"HOME": "/h"}, "/repo"))["/launch-tmp"] == "write"


@pytest.mark.feature("codex-planner-write-grants")
def test_spawn_grants_a_codex_planner_the_git_dir_of_its_local_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    checkout = _primary(tmp_path / "hive")
    monkeypatch.setattr(daemon, "local_checkout", lambda repo: str(checkout))
    passed = _passed(_spawn_seen(tmp_path, {"plan": {"agent": "codex"}}, lane="plan")["argv"])
    grant = next(arg for arg in passed if arg.startswith("permissions.planner="))
    assert f'{json.dumps(str((checkout / ".git").resolve()))}="write"' in grant
    assert f"{json.dumps(str(checkout))}=" not in grant


@pytest.mark.feature("codex-planner-write-grants")
def test_the_managed_brain_auto_approves_its_tools_for_codex_only() -> None:
    servers = brain_binding.apply({}, {brain_binding.VARIABLE: json.dumps(BOUND)})
    assert servers[brain_binding.SERVER]["default_tools_approval_mode"] == "approve"
    entry, reason = codex_mcp_entry(brain_binding.SERVER, servers[brain_binding.SERVER])
    assert reason == ""
    assert entry is not None
    assert entry["default_tools_approval_mode"] == "approve"
    mounted, _, _ = connectors.claude(servers, "claude.json")
    assert "default_tools_approval_mode" not in mounted[brain_binding.SERVER]
    assert mounted[brain_binding.SERVER]["url"] == BOUND["mcp"]
