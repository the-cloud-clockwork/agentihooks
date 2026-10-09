import pytest

from scripts.claude_quota_balancer import RowMarks
from scripts.routing import master_account, place
from scripts.routing.master_account import MasterAccount
from scripts.routing.settings import FileSettings, RedisSettings
from scripts.routing.slots import INTERACTIVE, SUBSCRIPTION

ENV = {"AH_CC_TOKEN_luna": "t", "AH_CX_TOKEN_work": "t"}


@pytest.fixture
def store(tmp_path):
    return FileSettings(tmp_path / "routing-settings.json")


def test_a_claude_slug_with_a_token_is_a_subscription_master(store):
    master_account.declare(store, {"claude": ("luna", "max")}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"claude": MasterAccount("claude", "luna", "max", SUBSCRIPTION)}
    assert master_account.declared(store, ENV)["claude"].marker == "MASTER max"
    assert [(entry["key"], entry["value"], entry["at"]) for entry in store.history()] == [
        ("master-account-claude", "luna", 1.0),
        ("master-tier-claude", "max", 1.0),
    ]


def test_a_claude_slug_without_a_token_is_an_interactive_master(store):
    master_account.declare(store, {"claude": ("home", None)}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"claude": MasterAccount("claude", "home", "", INTERACTIVE)}
    assert master_account.declared(store, ENV)["claude"].marker == "MASTER"


def test_codex_default_is_the_interactive_login_and_a_token_slug_a_subscription(store):
    master_account.declare(store, {"codex": ("default", "pro")}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"codex": MasterAccount("codex", "default", "pro", INTERACTIVE)}
    master_account.declare(store, {"codex": ("work", None)}, ENV, "operator", 2.0)
    assert master_account.declared(store, ENV) == {"codex": MasterAccount("codex", "work", "", SUBSCRIPTION)}
    assert store.get("master-tier-codex") is None


def test_an_unknown_codex_token_slug_is_refused_and_nothing_is_written(store):
    with pytest.raises(ValueError, match="unknown codex token slug gone: no AH_CX_TOKEN_gone is set"):
        master_account.declare(store, {"claude": ("luna", None), "codex": ("gone", None)}, ENV, "operator", 1.0)
    assert store.history() == []


def test_clear_removes_the_named_harnesses_only(store):
    master_account.declare(store, {"claude": ("luna", "max"), "codex": ("default", None)}, ENV, "operator", 1.0)
    master_account.clear(store, ["claude"], "operator", 2.0)
    assert set(master_account.declared(store, ENV)) == {"codex"}
    assert store.get("master-tier-claude") is None
    assert [(entry["key"], entry["at"]) for entry in store.history()[-2:]] == [
        ("master-account-claude", 2.0),
        ("master-tier-claude", 2.0),
    ]
    master_account.clear(store, list(master_account.HARNESSES), "operator", 3.0)
    assert master_account.declared(store, ENV) == {}


@pytest.mark.xdist_group("fakeredis")
def test_load_reads_the_store_the_routing_client_opens(monkeypatch):
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    RedisSettings(client).set("master-account-claude", "luna", "operator", 1.0)
    seen = []
    monkeypatch.setattr(place, "_client", lambda environ: seen.append(environ) or client)

    assert master_account.load(ENV) == {"claude": MasterAccount("claude", "luna", "", SUBSCRIPTION)}
    assert seen == [ENV]


def test_row_marks_carry_the_current_account_and_the_claude_master(monkeypatch):
    masters = {"claude": MasterAccount("claude", "home", "", INTERACTIVE), "codex": MasterAccount("codex", "default")}
    monkeypatch.setattr(master_account, "load", lambda environ: masters if environ is ENV else {})

    assert master_account.row_marks("luna", ENV) == RowMarks("luna", masters["claude"])
    assert master_account.row_marks(environ=ENV) == RowMarks("", masters["claude"])
