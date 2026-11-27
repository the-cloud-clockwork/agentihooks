from collections.abc import Mapping

import pytest

from scripts import claude_quota_balancer, codex_router
from scripts.quick import authentication

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(("harness", "prefix"), [("claude", "AH_CC_TOKEN_"), ("codex", "AH_CX_TOKEN_")])
def test_named_native_selection_keeps_an_explicit_alias_of_the_same_login(harness: str, prefix: str) -> None:
    env = {f"{prefix}alpha": "same-fixture", f"{prefix}zebra": "same-fixture"}
    assert authentication.selections("named:zebra", [harness], env) == [("named", "zebra")]


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("reference", ["zebra", "AH_CC_TOKEN_zebra"])
def test_claude_named_execution_keeps_the_exact_reference_without_automatic_deduplication(reference: str) -> None:
    env = {"AH_CC_TOKEN_alpha": "same-fixture", "AH_CC_TOKEN_zebra": "same-fixture"}
    decision = claude_quota_balancer.forced(env, f"named:{reference}")
    assert decision.account == "zebra"
    assert decision.credential is not None
    assert decision.credential.env_name == "AH_CC_TOKEN_zebra"
    assert (decision.source, decision.result, decision.sessions, decision.max_sessions, decision.kind) == (
        "forced",
        None,
        None,
        None,
        claude_quota_balancer.SUBSCRIPTION,
    )


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("reference", ["zebra", "AH_CX_TOKEN_zebra"])
@pytest.mark.parametrize("live", [0, 4])
def test_codex_named_execution_never_enumerates_or_reads_automatic_tiers(
    reference: str, live: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        pytest.fail("explicit named execution entered automatic routing")

    monkeypatch.setattr(codex_router, "accounts", unavailable)
    monkeypatch.setattr(codex_router.CodexAccountSource, "readings", unavailable)
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: {"zebra": live} if live else {})
    env: Mapping[str, str] = {"AH_CX_TOKEN_alpha": "same-fixture", "AH_CX_TOKEN_zebra": "same-fixture"}
    account, placement, count, cap = codex_router._route(env, f"named:{reference}", unavailable, "zebra")
    assert (account.name, account.env_name, placement, count, cap) == (
        "zebra",
        "AH_CX_TOKEN_zebra",
        "forced",
        live,
        "?",
    )


@pytest.mark.feature("quick-native-authentication")
def test_missing_named_default_reference_never_falls_back_to_codex_local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_router, "accounts", lambda env, run: [codex_router.CodexAccount("default")])
    monkeypatch.setattr(codex_router.CodexAccountSource, "readings", lambda *args: {})
    with pytest.raises(codex_router.RoutingError):
        codex_router._route({}, "named:default", lambda *args, **kwargs: None, "")


@pytest.mark.feature("quick-native-authentication")
def test_missing_claude_named_reference_never_selects_another_account() -> None:
    with pytest.raises(claude_quota_balancer.RoutingError) as error:
        claude_quota_balancer.forced({"AH_CC_TOKEN_alpha": "fixture"}, "named:missing")
    assert str(error.value) == "account suffix 'missing' not found; available: none"


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("remaining", [1, 7200])
def test_codex_named_reference_preserves_expiry_validation(remaining: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import json
    from types import SimpleNamespace

    now = 1000.0
    payload = base64.urlsafe_b64encode(json.dumps({"exp": now + remaining}).encode()).decode().rstrip("=")
    env = {
        "AH_CX_TOKEN_zebra": f"header.{payload}.fixture",
        codex_router.TOKEN_MIN_LIFE_ENV: "1",
    }
    monkeypatch.setattr(codex_router, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: {})
    if remaining == 1:
        with pytest.raises(codex_router.RoutingError) as error:
            codex_router._route(env, "named:zebra", lambda *args, **kwargs: None, "")
        assert str(error.value) == codex_router.EXPIRING.format(env="AH_CX_TOKEN_zebra", hours=1.0, name="zebra")
    else:
        account, placement, count, cap = codex_router._route(env, "named:zebra", lambda *args, **kwargs: None, "")
        assert (account.name, placement, count, cap) == ("zebra", "forced", 0, "?")


@pytest.mark.feature("quick-native-authentication")
def test_empty_codex_named_reference_never_enters_automatic_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("empty named reference entered account selection")

    monkeypatch.setattr(codex_router, "select", refuse)
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: {})
    with pytest.raises(codex_router.RoutingError) as error:
        codex_router._route({"AH_CX_TOKEN_alpha": "fixture"}, "named:", refuse, "")
    assert str(error.value) == "named Codex authentication reference is empty"
