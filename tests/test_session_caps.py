import json
import re

import pytest

from scripts import agent_choice, agents_quota, codex_router, session_caps
from scripts import claude_quota_balancer as balancer
from scripts.session_caps import SessionCaps
from tests.test_claude_quota_balancer import _stream, _three
from tests.test_codex_router import _accounts, _quota

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def client(monkeypatch):
    import fakeredis

    found = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(session_caps, "_client", lambda: found)
    return found


def test_a_set_cap_is_read_back_per_harness_and_cleared_by_default(client):
    session_caps.set_cap("luna", 7)
    session_caps.set_cap("luna", 2, harness="codex")
    assert session_caps.stored() == {"luna": 7}
    assert session_caps.stored("codex") == {"luna": 2}
    session_caps.set_cap("luna", None)
    assert session_caps.stored() == {}
    assert session_caps.stored("codex") == {"luna": 2}


@pytest.mark.parametrize(
    ("account", "cap", "harness"),
    [("luna", 0, "claude"), ("luna", 51, "claude"), ("", 3, "claude"), ("a:b", 3, "claude"), ("luna", 3, "gpt")],
)
def test_a_cap_outside_its_bounds_is_refused(client, account, cap, harness):
    with pytest.raises(ValueError):
        session_caps.set_cap(account, cap, harness=harness)
    assert client.hgetall(session_caps.KEY) == {}


def test_an_unreachable_store_reads_as_no_stored_caps_and_says_so(monkeypatch, capsys):
    import redis

    class Down:
        def hgetall(self, key):
            raise redis.ConnectionError("refused")

    monkeypatch.setattr(session_caps, "_client", Down)
    assert session_caps.stored() == {}
    assert session_caps.caps(3).of("luna") == 3
    assert "every account takes the default cap" in capsys.readouterr().err


def test_a_stored_cap_lets_placement_open_an_account_past_the_default(monkeypatch, tmp_path):
    env = _three(monkeypatch)
    sessions = {"BEST": 3, "MID": 1}
    plain = balancer.select_credential(env, cache_file=tmp_path / "c.json", sessions=sessions, caps=SessionCaps(3))
    raised = balancer.select_credential(
        env, cache_file=tmp_path / "c.json", sessions=sessions, caps=SessionCaps(3, {"BEST": 7})
    )
    assert plain.result.account == "MID"
    assert (raised.result.account, raised.placement, raised.max_sessions) == ("BEST", "open", 7)
    assert "sessions=3/7" in balancer.format_selection(raised)


def test_a_stored_cap_below_the_default_closes_that_account(monkeypatch, tmp_path):
    env = _three(monkeypatch)
    decision = balancer.select_credential(
        env, cache_file=tmp_path / "c.json", sessions={"BEST": 1}, caps=SessionCaps(3, {"BEST": 1})
    )
    assert decision.result.account == "MID"


def test_the_balance_table_shows_each_account_against_its_own_cap():
    alpha = balancer.parse_probe("alpha", _stream(0.10, 0.20), 100)
    beta = balancer.parse_probe("beta", _stream(0.30, 0.40), 100)
    table = balancer.render_table(
        [alpha, beta], now=0, sessions={"alpha": 4, "beta": 1}, caps=SessionCaps(3, {"alpha": 7})
    )
    rows = {line.split()[1]: line for line in table.splitlines()[2:4]}
    assert "4/7" in rows["alpha"]
    assert "1/3" in rows["beta"]


def test_balance_reads_the_stored_caps(monkeypatch, capsys, client):
    from scripts import install

    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")
    result = balancer.parse_probe("ALPHA", _stream(0.10, 0.20), 100)
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [credential])
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([result], "cached"))
    monkeypatch.setattr(agents_quota, "codex_table", lambda: "")
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: {"ALPHA": 4})
    session_caps.set_cap("ALPHA", 7)
    install.cmd_balance(include_fable=False, refresh=False, timeout=10)
    assert "4/7" in capsys.readouterr().out


def test_launch_placement_passes_the_stored_caps(monkeypatch, client, tmp_path):
    from scripts import install

    seen = {}

    def select(environ, **kwargs):
        seen.update(kwargs)
        raise balancer.RoutingError("stop here")

    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr(balancer, "select_credential", select)
    monkeypatch.setattr(balancer, "_cache_path", lambda environ: tmp_path / "cache.json")
    monkeypatch.setattr("hooks.context.account_sessions.max_sessions", lambda environ=None: 4)
    session_caps.set_cap("luna", 6)
    with pytest.raises(SystemExit):
        install.cmd_claude([])
    assert seen["caps"] == SessionCaps(4, {"luna": 6})


def test_the_codex_router_reads_a_cap_per_account():
    quotas = {"default": _quota(80.0), "alpha": _quota(30.0), "beta": _quota(10.0)}
    sessions = {"beta": 3}
    assert codex_router.select(_accounts(), quotas, sessions, cap=3)[0].name == "alpha"
    assert codex_router.select(_accounts(), quotas, sessions, cap=3, caps={"beta": 5})[0].name == "beta"


def test_agent_choice_counts_a_raised_codex_cap_as_room(monkeypatch, client):
    from hooks.context import account_sessions

    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 3})
    assert agent_choice.at_cap("codex", {}) is True
    session_caps.set_cap("default", 4, harness="codex")
    assert agent_choice.at_cap("codex", {}) is False


def test_page_quota_rows_carry_their_account_cap(monkeypatch, client):
    agents_quota._page_cache.clear()
    rows = [
        {"agent": "claude", "account": "luna", "sessions": 4},
        {"agent": "codex", "account": "luna", "sessions": 1},
    ]
    monkeypatch.setattr(agents_quota, "_page_quota", lambda now: {"cap": 3, "probed_at": None, "rows": rows})
    session_caps.set_cap("luna", 7)
    first = agents_quota.page_quota(now=100.0)
    assert [(r["agent"], r["cap"]) for r in first["rows"]] == [("claude", 7), ("codex", 3)]
    session_caps.set_cap("luna", 2, harness="codex")
    again = agents_quota.page_quota(now=101.0)
    assert [r["cap"] for r in again["rows"]] == [7, 2]
    assert json.dumps(again)
    agents_quota._page_cache.clear()


def test_the_swarm_command_sets_and_clears_an_account_cap(client, capsys):
    from scripts.swarm import cli

    args = cli.build_parser().parse_args(["rig", "session-cap", "luna", "7"])
    cli.cmd_session_cap(None, args)
    assert json.loads(capsys.readouterr().out) == {"account": "luna", "harness": "claude", "cap": 7}
    assert session_caps.stored() == {"luna": 7}
    cli.cmd_session_cap(None, cli.build_parser().parse_args(["rig", "session-cap", "luna", "default"]))
    assert json.loads(capsys.readouterr().out)["cap"] == "default"
    assert session_caps.stored() == {}
    cli.cmd_session_cap(None, cli.build_parser().parse_args(["rig", "session-cap", "luna", "2", "--harness", "codex"]))
    assert session_caps.stored("codex") == {"luna": 2}


@pytest.mark.parametrize("cap", ["0", "51", "seven"])
def test_the_swarm_command_refuses_a_cap_out_of_range(client, cap):
    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError, match="from 1 to 50"):
        cli.cmd_session_cap(None, cli.build_parser().parse_args(["rig", "session-cap", "luna", cap]))
    assert session_caps.stored() == {}


def test_the_page_control_runs_the_swarm_command():
    from scripts.swarm_ledger import ledger_server as server

    body = {"action": "session_cap", "account": "luna", "cap": 7}
    assert server.control_argv(body) == ["session-cap", "luna", "7", "--harness", "claude"]
    assert server.control_argv({**body, "harness": "codex"})[-1] == "codex"


@pytest.mark.parametrize(
    "change", [{"account": "-x"}, {"account": "a b"}, {"cap": 0}, {"cap": "7"}, {"cap": 51}, {"harness": "gpt"}]
)
def test_the_page_control_refuses_a_bad_session_cap(change):
    from scripts.swarm_ledger import ledger_server as server

    with pytest.raises(ValueError):
        server.control_argv({"action": "session_cap", "account": "luna", "cap": 7, **change})


def test_the_v1_control_schema_takes_a_session_cap():
    from scripts.swarm_ledger.api import admin, schemas

    schemas.validate(admin.CONTROL, {"action": "session_cap", "account": "luna", "cap": 7, "harness": "codex"})


@pytest.mark.parametrize(
    ("account", "cap", "harness", "message"),
    [
        ("luna", 3, "gpt", "harness must be one of claude, codex"),
        ("-x", 3, "claude", "account must be an exact account name"),
        ("luna", 0, "claude", "a session cap is a whole number from 1 to 50"),
    ],
)
def test_check_names_what_is_wrong(account, cap, harness, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        session_caps.check(account, cap, harness)


def test_check_accepts_both_bounds():
    assert session_caps.check("luna", 1, "claude") == "claude:luna"
    assert session_caps.check("luna", session_caps.MAX_CAP, "codex") == "codex:luna"


def test_the_swarm_command_refuses_an_unknown_harness():
    from scripts.swarm import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rig", "session-cap", "luna", "3", "--harness", "gpt"])


def test_the_page_control_names_a_missing_cap():
    from scripts.swarm_ledger import ledger_server as server

    with pytest.raises(ValueError, match="^session_cap needs a cap$"):
        server.control_argv({"action": "session_cap", "account": "luna"})


def test_an_account_with_no_live_session_is_open_under_a_cap_of_one(monkeypatch, tmp_path):
    env = _three(monkeypatch)
    decision = balancer.select_credential(env, cache_file=tmp_path / "c.json", sessions={}, caps=SessionCaps(1))
    assert (decision.placement, decision.sessions, decision.max_sessions) == ("open", 0, 1)


def test_the_balance_table_counts_a_missing_account_as_zero_and_marks_an_unknown_cap():
    alpha = balancer.parse_probe("alpha", _stream(0.10, 0.20), 100)
    capped = balancer.render_table([alpha], now=0, sessions={}, caps=SessionCaps(3))
    unknown = balancer.render_table([alpha], now=0, sessions={"alpha": 1})
    assert capped.splitlines()[2].split()[3] == "0/3"
    assert unknown.splitlines()[2].split()[3] == "1/?"


def test_the_codex_router_opens_an_account_with_no_live_session_under_a_cap_of_one():
    quotas = {"default": _quota(80.0), "alpha": _quota(30.0), "beta": _quota(10.0)}
    assert codex_router.select(_accounts(), quotas, {}, cap=1)[1] == "open"


def test_agent_choice_counts_a_codex_account_with_no_live_session_as_room(monkeypatch, client):
    from hooks.context import account_sessions

    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: True)
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 1})
    env = {"AH_CX_TOKEN_alpha": "cx-a", "AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "1"}
    assert agent_choice.at_cap("codex", env) is False


def test_agent_choice_reads_each_claude_account_cap(monkeypatch, client):
    from hooks.context import account_sessions

    luna = balancer.parse_probe("luna", _stream(0.10, 0.20), 100)
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(0.0, luna)])
    env = {"AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "3"}
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    assert agent_choice.at_cap("claude", {"AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "1"}) is False
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"luna": 3})
    assert agent_choice.at_cap("claude", env) is True
    session_caps.set_cap("luna", 5)
    assert agent_choice.at_cap("claude", env) is False


def test_the_codex_balance_rows_show_each_account_against_its_codex_cap(monkeypatch):
    seen = []
    rows = [agents_quota.QuotaRow("codex", "alpha", "NORMAL", 2, None, 60.0, 3600, "session-log 1m ago")]
    monkeypatch.setattr(agents_quota, "_codex", lambda now: seen.append(now) or rows)
    monkeypatch.setattr(session_caps, "stored", lambda harness="claude": {"alpha": 5} if harness == "codex" else {})
    monkeypatch.setattr("hooks.context.account_sessions.max_sessions", lambda environ=None: 3)
    line = agents_quota.codex_table().splitlines()[1].split()
    assert line[3] == "2/5"
    assert isinstance(seen[0], float) and seen[0] > 0
    assert line[7] == "0m"


def _route_env(monkeypatch, sessions, caps):
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: sessions)
    monkeypatch.setattr(session_caps, "stored", lambda harness="claude": caps if harness == "codex" else {})


def test_the_default_codex_launch_reports_its_own_cap(monkeypatch):
    env = {"AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "3"}
    _route_env(monkeypatch, {"default": 2}, {"default": 4})
    assert codex_router._route(env, "", None) == (codex_router.CodexAccount("default"), "open", 2, 4)
    _route_env(monkeypatch, {}, {})
    assert codex_router._route(env, "", None) == (codex_router.CodexAccount("default"), "open", 0, 3)


def test_a_routed_codex_launch_places_by_and_reports_the_account_cap(monkeypatch):
    env = {"AH_CX_TOKEN_alpha": "cx-a", "AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT": "3"}
    alpha, asked = codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"), []
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ, run: [alpha])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: asked.append((pool, environ)) or {})
    _route_env(monkeypatch, {"alpha": 3}, {"alpha": 5})
    assert codex_router._route(env, "", None) == (alpha, "open", 3, 5)
    assert asked == [([alpha], env)]
    _route_env(monkeypatch, {}, {})
    assert codex_router._route(env, "", None) == (alpha, "open", 0, 3)


def _balance(monkeypatch, sessions, stored):
    from scripts import install

    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")
    result = balancer.parse_probe("ALPHA", _stream(0.10, 0.20), 100)
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [credential])
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([result], "cached"))
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(balancer, "ancestor_oauth_token", lambda: "")
    monkeypatch.setattr(
        balancer, "identify_session_account", lambda *args: balancer.SessionAccount("ALPHA", "environment")
    )
    monkeypatch.setattr(agents_quota, "codex_table", lambda: "")
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: sessions)
    monkeypatch.setattr("hooks.context.account_sessions.max_sessions", lambda environ=None: 4)
    monkeypatch.setattr(session_caps, "stored", lambda harness="claude": stored)
    return install


def test_balance_shows_the_default_cap_for_an_account_without_one(monkeypatch, capsys):
    install = _balance(monkeypatch, {"ALPHA": 1}, {})
    install.cmd_balance(include_fable=True, refresh=False, timeout=10)
    out = capsys.readouterr().out
    assert "1/4" in out
    assert "FABLE LEFT" in out


def test_current_balance_shows_the_account_against_its_own_cap(monkeypatch, capsys):
    install = _balance(monkeypatch, {"ALPHA": 4}, {"ALPHA": 7})
    assert install.cmd_balance(include_fable=False, refresh=False, timeout=10, current=True) == 0
    out = capsys.readouterr().out
    assert "current=ALPHA" in out
    assert "4/7" in out
