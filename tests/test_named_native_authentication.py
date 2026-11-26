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
def test_claude_named_execution_keeps_the_exact_reference_without_automatic_deduplication() -> None:
    env = {"AH_CC_TOKEN_alpha": "same-fixture", "AH_CC_TOKEN_zebra": "same-fixture"}
    decision = claude_quota_balancer.forced(env, "named:zebra")
    assert decision.account == "zebra"
    assert decision.credential is not None
    assert decision.credential.env_name == "AH_CC_TOKEN_zebra"


@pytest.mark.feature("quick-native-authentication")
def test_codex_named_execution_never_enumerates_or_reads_automatic_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        pytest.fail("explicit named execution entered automatic routing")

    monkeypatch.setattr(codex_router, "accounts", unavailable)
    monkeypatch.setattr(codex_router.CodexAccountSource, "readings", unavailable)
    env: Mapping[str, str] = {"AH_CX_TOKEN_alpha": "same-fixture", "AH_CX_TOKEN_zebra": "same-fixture"}
    account, placement, _, cap = codex_router._route(env, "named:zebra", unavailable, "zebra")
    assert (account.name, account.env_name, placement, cap) == ("zebra", "AH_CX_TOKEN_zebra", "forced", "?")


@pytest.mark.feature("quick-native-authentication")
def test_missing_named_default_reference_never_falls_back_to_codex_local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_router, "accounts", lambda env, run: [codex_router.CodexAccount("default")])
    monkeypatch.setattr(codex_router.CodexAccountSource, "readings", lambda *args: {})
    with pytest.raises(codex_router.RoutingError):
        codex_router._route({}, "named:default", lambda *args, **kwargs: None, "")
