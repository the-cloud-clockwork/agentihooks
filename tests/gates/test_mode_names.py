import pytest

from tests.swarm.test_cli import env, run  # noqa: F401


@pytest.mark.parametrize(
    "mode,stored",
    [
        ("deny", "enforce"),
        ("log only", "observe"),
        ("skip", "off"),
        ("enforce", "enforce"),
        ("observe", "observe"),
        ("off", "off"),
    ],
)
def test_swarm_set_accepts_both_mode_names(env, monkeypatch, mode, stored):  # noqa: F811
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", f"watch-gate={mode}") == 0
    assert store.config("sw").gates == {"watch": stored}


@pytest.mark.parametrize("as_json", [False, True])
def test_status_uses_new_names_in_modes_and_decisions(env, monkeypatch, capsys, as_json):  # noqa: F811
    import json

    from scripts.gates import log
    from scripts.gates.base import Who

    _, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    run("sw", "set", "watch-gate=observe")
    log.append("sw", log.Row.of("watch", "observe", Who(name="agent", task="work"), "Bash", "budget spent"))
    capsys.readouterr()
    assert run("sw", "status", *(["--json"] if as_json else [])) == 0
    output = capsys.readouterr().out
    if as_json:
        report = json.loads(output)
        assert report["gate_modes"]["identity"] == "deny"
        assert report["gate_modes"]["watch"] == "log only"
        assert report["gates"][0]["kind"] == "log only"
    else:
        assert "identity=deny" in output
        assert "watch=log only" in output
        assert "gate  log only  watch" in output
