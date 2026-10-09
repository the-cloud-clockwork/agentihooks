import pytest

from scripts.routing import master_account
from scripts.routing.master_account import MasterAccount
from scripts.routing.settings import FileSettings
from scripts.routing.slots import INTERACTIVE, SUBSCRIPTION

ENV = {"AH_CC_TOKEN_luna": "t", "AH_CX_TOKEN_work": "t"}


@pytest.fixture
def store(tmp_path):
    return FileSettings(tmp_path / "routing-settings.json")


def test_a_claude_slug_with_a_token_is_a_subscription_master(store):
    master_account.declare(store, {"claude": ("luna", "max")}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"claude": MasterAccount("claude", "luna", "max", SUBSCRIPTION)}
    assert master_account.declared(store, ENV)["claude"].marker == "MASTER max"


def test_a_claude_slug_without_a_token_is_an_interactive_master(store):
    master_account.declare(store, {"claude": ("home", "")}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"claude": MasterAccount("claude", "home", "", INTERACTIVE)}
    assert master_account.declared(store, ENV)["claude"].marker == "MASTER"


def test_codex_default_is_the_interactive_login_and_a_token_slug_a_subscription(store):
    master_account.declare(store, {"codex": ("default", "pro")}, ENV, "operator", 1.0)
    assert master_account.declared(store, ENV) == {"codex": MasterAccount("codex", "default", "pro", INTERACTIVE)}
    master_account.declare(store, {"codex": ("work", "")}, ENV, "operator", 2.0)
    assert master_account.declared(store, ENV) == {"codex": MasterAccount("codex", "work", "", SUBSCRIPTION)}
    assert store.get("master-tier-codex") is None


def test_an_unknown_codex_token_slug_is_refused_and_nothing_is_written(store):
    with pytest.raises(ValueError, match="unknown codex token slug gone: no AH_CX_TOKEN_gone is set"):
        master_account.declare(store, {"claude": ("luna", ""), "codex": ("gone", "")}, ENV, "operator", 1.0)
    assert store.history() == []


def test_clear_removes_the_named_harnesses_only(store):
    master_account.declare(store, {"claude": ("luna", "max"), "codex": ("default", "")}, ENV, "operator", 1.0)
    master_account.clear(store, ["claude"], "operator", 2.0)
    assert set(master_account.declared(store, ENV)) == {"codex"}
    assert store.get("master-tier-claude") is None
    master_account.clear(store, list(master_account.HARNESSES), "operator", 3.0)
    assert master_account.declared(store, ENV) == {}
