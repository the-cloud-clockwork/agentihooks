import json
import secrets

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.swarm import cli, trace_plan
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger, ledger_authority, ledger_route, ledger_server, ledger_workspace, storage
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.unit

live = _authority_live
PLAN = "- walls | doghouse | it shelters the dog\n- a generator | power | it powers a light\n"
CUT = "Cut from the plan: a generator"


@pytest.fixture
def hosted(live, monkeypatch, tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, str(tmp_path), 1, 0))
    url = f"http://{live['host']}"
    store.update(SLUG, api_url=url)
    name = store.next_name(SLUG, "eng")
    store.start_execution(SLUG, AgentRecord(name, "eng", "t9", seat=f"eng-1@{SLUG}", launched_at=10001))
    secret = secrets.token_hex(32)
    monkeypatch.setattr(ledger_server.authority, "environ", {"SWARM_AUTH_MODE": "edge", "SWARM_SECRET": secret})
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda *_: store.redis)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, name))
    for unset in ("AGENTIHOOKS_CONTROLLER_CREDENTIAL", "SWARM_URL", "SWARM_SECRET"):
        monkeypatch.delenv(unset, raising=False)
    asked = {
        "piece_0": Answer(type="noul", noul=0.9),
        "piece_1": Answer(type="noul", noul=0.1),
        "size": Answer(type="score", score=1.0, confidence=0.9),
    }
    monkeypatch.setattr(trace_plan, "decide", lambda state, q, **k: DecisionResult(asked, "m"))
    folder = ledger_workspace.folder(SLUG, "t9")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.md").write_text(PLAN)
    return name, folder, url


def test_trace_plan_on_a_hosted_ledger_files_the_cut_piece_as_the_pinned_agent(hosted, capsys):
    name, folder, url = hosted

    assert cli.main([SLUG, "trace-plan"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert (report["task"], report["verdict"], report["cut"]) == ("t9", "pass", ["a generator"])
    assert json.loads((folder / "plan-verdict.json").read_text())["verdict"] == "pass"
    with ledger_route.scoped(SLUG, url):
        filed = [item for item in ledger.request(SLUG)["followups"] if item["text"] == CUT]
    assert len(filed) == 1
    events = storage.default().events_since(SLUG, 0)
    added = [event for event in events if event["target"] == f"followups/{filed[0]['id']}"]
    assert [(event["by"], event["kind"]) for event in added] == [(name, "added")]
