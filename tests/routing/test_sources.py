import dataclasses

import pytest

import scripts.claude_quota_balancer as balancer
import scripts.codex_router as codex_router
import scripts.session_bands as session_bands
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.codex_quota import CodexQuota
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION, Slot

NOW = 1_900_000_000


def test_a_slot_keeps_the_seat_field_order_and_takes_kind_and_weight_by_keyword():
    slot = Slot("claude", "a", 6, 2, 10.0)
    assert (slot.harness, slot.account, slot.cap, slot.sessions, slot.spend_before) == ("claude", "a", 6, 2, 10.0)
    assert (slot.kind, slot.weight, slot.free) == ("subscription", None, 4)
    assert Slot("codex", "b", 1, 3).free == 0
    assert Slot("codex", "b", 1, 0).spend_before is None
    assert Slot("x", "y", 1, 0, kind=API, weight=0.5) == Slot("x", "y", 1, 0, None, kind="api", weight=0.5)
    with pytest.raises(TypeError):
        Slot("x", "y", 1, 0, None, "api")
    with pytest.raises(dataclasses.FrozenInstanceError):
        slot.cap = 1
    assert session_bands.Seat is Slot
    assert (SUBSCRIPTION, INTERACTIVE, API) == ("subscription", "interactive", "api")


def _result(account, five, week=10):
    return ProbeResult(
        account,
        "allowed",
        "NORMAL",
        None if five is None else 100 - five,
        QuotaWindow(five, NOW + 60),
        QuotaWindow(week, NOW + 600),
    )


def test_the_claude_token_source_offers_a_subscription_slot_per_routable_account(monkeypatch):
    calls = []

    def collect(credentials, **kwargs):
        calls.append(([credential.env_name for credential in credentials], kwargs))
        return [_result("a", 10), _result("b", 50), _result("c", None), _result("d", 20)], "cached"

    monkeypatch.setattr(balancer, "collect_results", collect)
    environ = {"AH_CC_TOKEN_a": "va", "AH_CC_TOKEN_b": "vb", "AH_CC_TOKEN_d": "vd"}
    source = balancer.ClaudeTokenSource({"refresh": True}, {"b": 1}, frozenset({"d"}))
    assert source.slots(environ, NOW) == [
        Slot("claude", "a", 6, 0, NOW + 600, kind="subscription"),
        Slot("claude", "b", 4, 1, NOW + 600, kind="subscription"),
    ]
    assert calls == [
        (["AH_CC_TOKEN_a", "AH_CC_TOKEN_b", "AH_CC_TOKEN_d"], {"refresh": True, "environ": environ, "now": NOW})
    ]


def test_the_claude_token_source_forwards_now_only_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(balancer, "collect_results", lambda credentials, **kwargs: calls.append(kwargs) or ([], "live"))
    source = balancer.ClaudeTokenSource()
    assert source.results({"AH_CC_TOKEN_a": "va"}, 5) == ([], "live")
    assert calls == [{"environ": {"AH_CC_TOKEN_a": "va"}, "now": 5}]
    assert source.results({}, 5) == ([], "cached")
    assert len(calls) == 1


def test_the_claude_token_source_reads_the_fable_week_only_when_asked():
    result = ProbeResult("a", "allowed", "NORMAL", 90, QuotaWindow(10), QuotaWindow(10), QuotaWindow(97, NOW + 60))
    assert balancer.ClaudeTokenSource().cap(result, NOW) == 6
    assert balancer.ClaudeTokenSource({"include_fable": True}).cap(result, NOW) == 0


def test_the_claude_token_source_gives_the_child_only_its_own_token():
    environ = {"AH_CC_TOKEN_a": "va", "AH_CC_TOKEN_b": "vb", "ANTHROPIC_API_KEY": "key", "HOME": "/h"}
    child = balancer.ClaudeTokenSource().child_env(Slot("claude", "b", 6, 0), environ)
    assert child == {"HOME": "/h", "CLAUDE_CODE_OAUTH_TOKEN": "vb"}
    with pytest.raises(balancer.RoutingError, match="account suffix 'z' not found; available: a, b"):
        balancer.ClaudeTokenSource().child_env(Slot("claude", "z", 6, 0), environ)


def _quota(week, five=None, observed_at=NOW):
    return CodexQuota(observed_at, "team", QuotaWindow(five, NOW + 60), QuotaWindow(week, NOW + 600))


CODEX_ENV = {"AH_CX_TOKEN_alpha": "cx-a", "AH_CX_TOKEN_beta": "cx-b", "CODEX_ACCESS_TOKEN": "old", "HOME": "/h"}


def test_the_codex_source_marks_the_default_login_interactive_and_tokens_subscription(monkeypatch):
    seen = {}

    def status(argv, **kwargs):
        seen["status"] = argv[-2:]
        return type("Done", (), {"returncode": 0})()

    readings = {"default": _quota(10), "alpha": _quota(97), "beta": _quota(20, 99, NOW - 3600)}
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {a.name: readings[a.name] for a in pool})
    source = codex_router.CodexAccountSource(status, {"default": 2}, refresh=False)
    assert source.slots(CODEX_ENV, NOW) == [
        Slot("codex", "default", 6, 2, NOW + 600, kind="interactive"),
        Slot("codex", "alpha", 0, 0, NOW + 600, kind="subscription"),
    ]
    assert seen["status"] == ["login", "status"]


def test_the_codex_source_refreshes_through_fresh_quotas_with_its_runner(monkeypatch):
    calls = []
    runner = object()
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ, *run: calls.append(("pool", run)) or [])
    monkeypatch.setattr(
        codex_router, "fresh_quotas", lambda pool, environ, now, *run: calls.append(("fresh", now, run)) or {}
    )
    assert codex_router.CodexAccountSource(runner).slots({}, NOW) == []
    assert codex_router.CodexAccountSource().slots({}, NOW) == []
    assert calls == [("pool", (runner,)), ("fresh", NOW, (runner,)), ("pool", ()), ("fresh", NOW, ())]


def test_the_codex_source_gives_the_child_only_the_slot_credentials():
    source = codex_router.CodexAccountSource()
    assert source.child_env(Slot("codex", "beta", 6, 0), CODEX_ENV) == {
        "HOME": "/h",
        "AH_CX_TOKEN_beta": "cx-b",
        "CODEX_ACCESS_TOKEN": "cx-b",
    }
    assert source.child_env(Slot("codex", "default", 6, 0, kind="interactive"), CODEX_ENV) == {"HOME": "/h"}
    with pytest.raises(codex_router.RoutingError) as refused:
        source.child_env(Slot("codex", "default", 6, 0), CODEX_ENV)
    assert str(refused.value) == "Codex account 'default' has no token; available: alpha, beta"
    with pytest.raises(codex_router.RoutingError) as empty:
        source.child_env(Slot("codex", "zulu", 6, 0, kind="api"), {})
    assert str(empty.value) == "Codex account 'zulu' has no token; available: none"
