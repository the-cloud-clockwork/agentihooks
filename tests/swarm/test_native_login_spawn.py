import json
from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.tick import SpawnError
from tests.swarm.test_runtime import validated


def config(tmp_path):
    return SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )


def test_expired_login_never_starts_a_pane(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=json.dumps({"loggedIn": False, "authMethod": "none"}))

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    with pytest.raises(SpawnError, match="native Claude login is expired or missing") as raised:
        runtime.spawn(config(tmp_path), "master", "agent", {"id": "master", "route_kind": "interactive"})
    assert raised.value.status == "unavailable"
    assert len(calls) == 1
    assert calls[0][1:] == ["auth", "status", "--text"]
    assert not (tmp_path / "proof" / "interactive-claude-status.json").exists()


@pytest.mark.parametrize("launch_ok", [True, False])
def test_status_proof_is_captured_only_after_success(tmp_path, launch_ok):
    def run(argv, **kwargs):
        if argv[1:] == ["auth", "status", "--text"]:
            assert kwargs["env"]["AH_ROUTE_INTERACTIVE_native"] == "1"
            return SimpleNamespace(
                returncode=0,
                stdout="Login method: Claude account\nEmail: private",
            )
        return SimpleNamespace(
            returncode=0 if launch_ok else 1,
            stdout=validated(argv, "status=started\nroute_status=routed\n"),
            stderr="launch failed",
        )

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._terminate = lambda _: None
    task = {"id": "master", "route_kind": "interactive"}
    if launch_ok:
        runtime.spawn(config(tmp_path), "master", "agent", task)
    else:
        with pytest.raises(SpawnError, match="launch failed"):
            runtime.spawn(config(tmp_path), "master", "agent", task)
    proof = tmp_path / "proof" / "interactive-claude-status.json"
    assert proof.exists() == launch_ok
    if launch_ok:
        assert json.loads(proof.read_text()) == {
            "command": "claude auth status --text",
            "status": "Login method: Claude account",
        }


@pytest.mark.parametrize("harness,route", [("claude", "subscription"), ("codex", "interactive")])
def test_other_launches_do_not_record_native_claude_proof(tmp_path, harness, route):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    chosen = config(tmp_path)
    chosen.lanes = {"master": {"agent": harness}}
    runtime.spawn(chosen, "master", "agent", {"id": "master", "route_kind": route})
    assert len(calls) == 1
    assert not (tmp_path / "proof" / "interactive-claude-status.json").exists()
