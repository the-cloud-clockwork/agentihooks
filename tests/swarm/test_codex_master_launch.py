from types import SimpleNamespace

import pytest

from scripts.swarm import live_binding
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import placed_record
from scripts.swarm_v2.runtime import codex
from tests.swarm.profile_fixture import validated


@pytest.mark.parametrize("transport", ["legacy", "app_server"])
def test_tick_codex_master_records_staged_home_before_completing_launch(tmp_path, monkeypatch, transport):
    for harness in ("CLAUDE", "CODEX"):
        for key in ("MODEL", "EFFORT"):
            monkeypatch.delenv(f"AGENTIHOOKS_{harness}_{key}", raising=False)
    name = "master@a1b2c3-0002"
    home = str(tmp_path / ".agentihooks" / "cx" / "account" / "codex")
    executions, launches, ended = {}, [], []
    execution = AgentRecord(
        name, MASTER, MASTER, seat="master@sw", execution_id="admitted", generation=1, transport=transport
    )
    monkeypatch.setattr(codex, "connect", lambda: SimpleNamespace(execution_occupants=lambda slug: executions))

    def run_init(argv, **kwargs):
        output = "status=started\nroute_status=routed\naccount=acct\nmodel=gpt-6.1-sol\neffort=high\n"
        if "init-agent" in argv:
            launches.append(argv)
            if "--defer-canary" in argv:
                output += f"profile_validation=pending\nprofile_report={tmp_path / 'report.json'}\n"
            else:
                executions[execution.seat] = execution
                output = validated(argv, output).replace("/fixture/profile", home)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    runtime = HerdrRuntime(
        home=tmp_path, run=run_init, choose=lambda requested, environ: ("codex", "open"), defer_master_canary=True
    )
    monkeypatch.setattr(runtime, "conversations", lambda: {})
    monkeypatch.setattr(runtime, "_terminate", ended.append)
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
        master_agent="codex",
        codex_inbox_transport=transport,
    )
    placed = runtime.spawn(config, MASTER, name, {"id": MASTER, "seat": "master@sw", "peer": "", "handoff": ""})
    record = placed_record(execution, placed)
    assert record.profile_decision["validation"]["state"] == "validated"
    assert record.profile_decision["validation"]["home"] == home
    assert record.profile_decision["validation"]["pid"] == 123
    assert "--defer-canary" not in launches[0]
    assert ended == []
    assert live_binding.assignment(record)["home"] == home
    assert (
        live_binding.compare(
            record,
            {
                "harness": "codex",
                "home": home,
                "profile": "master",
                "model": "gpt-6.1-sol",
                "effort": "high",
                "account": "acct",
                "hooks": True,
            },
        )
        == {}
    )
    if transport == "app_server":
        assert placed.execution == execution
