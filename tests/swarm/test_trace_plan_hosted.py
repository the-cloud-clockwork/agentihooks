import json
import secrets

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.swarm import cli, trace_plan
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger, ledger_authority, ledger_route, ledger_server, ledger_workspace, storage
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.unit

live = _authority_live
PLAN = "- walls | doghouse | it shelters the dog\n- a generator | power | it powers a light\n"
CUT = "Cut from the plan: a generator"


@pytest.fixture
def task():
    return "t9"


@pytest.fixture
def plan():
    return PLAN


@pytest.fixture
def hosted(live, monkeypatch, tmp_path, task, plan):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, str(tmp_path), 1, 0))
    url = f"http://{live['host']}"
    store.update(SLUG, api_url=url)
    name = store.next_name(SLUG, "eng")
    store.start_execution(SLUG, AgentRecord(name, "eng", task, seat=f"eng-1@{SLUG}", launched_at=10001))
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
    folder = ledger_workspace.folder(SLUG, task)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.md").write_text(plan)
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


def _cut_plan(kind):
    return f"- walls | doghouse | it shelters the dog\n- a {kind} generator | power | it powers a light\n"


def _filed(url, text):
    with ledger_route.scoped(SLUG, url):
        return [item for item in ledger.request(SLUG)["followups"] if item["text"] == text]


def _added(item_id):
    events = storage.default().events_since(SLUG, 0)
    return [event for event in events if event["target"] == f"followups/{item_id}" and event["kind"] == "added"]


def _verdict(folder):
    return json.loads((folder / "plan-verdict.json").read_text())


def _failing_followups(monkeypatch, failures, applied):
    real = LedgerClient._call
    left = [failures]

    def call(self, slug, ops=None):
        if left[0] and any(op.get("op") == "add_item" for op in ops or ()):
            left[0] -= 1
            if applied:
                real(self, slug, ops)
                raise SwarmError(f"ledger {slug}: the connection closed before the answer")
            raise LedgerRefused(f"ledger {slug}: server refused: 409 conflict")
        return real(self, slug, ops)

    monkeypatch.setattr(LedgerClient, "_call", call)


@pytest.mark.parametrize(("task", "plan"), [("t10", _cut_plan("petrol"))])
def test_a_refused_cut_keeps_the_pass_and_is_filed_once_on_the_next_run(hosted, monkeypatch, capsys):
    name, folder, url = hosted
    text = "Cut from the plan: a petrol generator"
    _failing_followups(monkeypatch, 1, applied=False)

    assert cli.main([SLUG, "trace-plan"]) != 0
    assert _verdict(folder)["verdict"] == "pass"
    assert _verdict(folder)["accepted"] == [] and _filed(url, text) == []

    assert cli.main([SLUG, "trace-plan"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "pass"
    [item] = _filed(url, text)
    assert [event["by"] for event in _added(item["id"])] == [name]
    assert _verdict(folder)["accepted"] == ["a petrol generator | power | it powers a light"]

    assert cli.main([SLUG, "trace-plan"]) == 0
    assert len(_filed(url, text)) == 1


@pytest.mark.parametrize(("task", "plan"), [("t11", _cut_plan("solar"))])
def test_a_cut_accepted_before_a_lost_answer_is_not_filed_twice(hosted, monkeypatch):
    name, folder, url = hosted
    text = "Cut from the plan: a solar generator"
    _failing_followups(monkeypatch, 1, applied=True)

    assert cli.main([SLUG, "trace-plan"]) != 0
    [first] = _filed(url, text)
    assert _verdict(folder)["accepted"] == []

    assert cli.main([SLUG, "trace-plan"]) == 0
    assert [item["id"] for item in _filed(url, text)] == [first["id"]]
    assert len(_added(first["id"])) == 1
    assert _verdict(folder)["accepted"] == ["a solar generator | power | it powers a light"]


@pytest.mark.parametrize(("task", "plan"), [("t12", _cut_plan("wind"))])
def test_repeated_refusals_retry_with_one_identity_until_one_cut_is_filed(hosted, monkeypatch):
    name, folder, url = hosted
    text = "Cut from the plan: a wind generator"
    sent = []
    real = LedgerClient._call

    def recording(self, slug, ops=None):
        sent.extend(op["id"] for op in ops or () if op.get("op") == "add_item")
        return real(self, slug, ops)

    monkeypatch.setattr(LedgerClient, "_call", recording)
    _failing_followups(monkeypatch, 3, applied=False)

    assert [cli.main([SLUG, "trace-plan"]) for _ in range(5)] == [1, 1, 1, 0, 0]
    [item] = _filed(url, text)
    assert len(set(sent)) == 1 and sent[0] == item["id"]
    assert _verdict(folder)["verdict"] == "pass"


def _legacy(folder, key):
    record = _verdict(folder)
    del record["accepted"]
    record["filed"] = [key]
    (folder / "plan-verdict.json").write_text(json.dumps(record))


@pytest.mark.parametrize(("task", "plan"), [("t13", _cut_plan("gas"))])
def test_a_legacy_cut_marked_filed_but_never_accepted_is_filed_once(hosted, monkeypatch):
    name, folder, url = hosted
    text = "Cut from the plan: a gas generator"
    _failing_followups(monkeypatch, 1, applied=False)
    assert cli.main([SLUG, "trace-plan"]) != 0
    _legacy(folder, "a gas generator | power | it powers a light")

    assert cli.main([SLUG, "trace-plan"]) == 0
    assert cli.main([SLUG, "trace-plan"]) == 0
    assert len(_filed(url, text)) == 1
    assert _verdict(folder)["accepted"] == ["a gas generator | power | it powers a light"]


@pytest.mark.parametrize(("task", "plan"), [("t14", _cut_plan("steam"))])
def test_a_legacy_cut_already_on_the_ledger_is_not_filed_again(hosted, monkeypatch):
    name, folder, url = hosted
    text = "Cut from the plan: a steam generator"
    stable = trace_plan.cut_id
    monkeypatch.setattr(trace_plan, "cut_id", lambda who, key: "add_item-0a1b2c3d4e")
    _failing_followups(monkeypatch, 1, applied=True)
    assert cli.main([SLUG, "trace-plan"]) != 0
    _legacy(folder, "a steam generator | power | it powers a light")
    monkeypatch.setattr(trace_plan, "cut_id", stable)

    assert cli.main([SLUG, "trace-plan"]) == 0
    assert [item["id"] for item in _filed(url, text)] == ["add_item-0a1b2c3d4e"]
    assert _verdict(folder)["accepted"] == ["a steam generator | power | it powers a light"]
