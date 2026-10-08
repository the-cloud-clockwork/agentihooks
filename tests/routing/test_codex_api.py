import os
import secrets
import shutil
import subprocess

import pytest

from hooks.context.account_sessions import API_ACCOUNT, API_MARKER
from scripts import codex_router as router
from scripts.routing import codex_api, envs
from scripts.routing.codex_api import CodexApiSource
from scripts.routing.slots import API, Slot
from tests.routing.fake_responses import FakeResponses

SENTINEL = "sentinel-value-7f3a"
NOW = 1_900_000_000
SUBSCRIPTION = {
    "HOME": "/home/u",
    "AH_CX_TOKEN_alpha": "cx-a",
    "AH_CC_TOKEN_ncgma": "cc",
    "CODEX_ACCESS_TOKEN": "stale",
}
API_NAMES = {
    "CODEX_API_KEY": SENTINEL,
    "OPENAI_API_KEY": SENTINEL,
    "OPENAI_BASE_URL": "https://gateway.example/v1",
    API_MARKER: "1",
}


def _override(key, value):
    return ["-c", f'model_providers.agentihooks-api.{key}="{value}"']


@pytest.mark.parametrize(
    ("environ", "key", "base", "label"),
    [
        ({"CODEX_API_KEY": SENTINEL}, "CODEX_API_KEY", "", "openai-key"),
        ({"OPENAI_API_KEY": SENTINEL}, "OPENAI_API_KEY", "", "openai-key"),
        ({"CODEX_API_KEY": SENTINEL, "OPENAI_API_KEY": SENTINEL}, "CODEX_API_KEY", "", "openai-key"),
        ({"CODEX_API_KEY": "", "OPENAI_API_KEY": SENTINEL}, "OPENAI_API_KEY", "", "openai-key"),
        (
            {"OPENAI_API_KEY": SENTINEL, "OPENAI_BASE_URL": "https://g.example/v1"},
            "OPENAI_API_KEY",
            "https://g.example/v1",
            "gateway",
        ),
        (
            {
                "CODEX_API_KEY": SENTINEL,
                "OPENAI_BASE_URL": "https://o.example",
                "AH_CX_API_BASE_URL": "https://a.example",
            },
            "CODEX_API_KEY",
            "https://a.example",
            "gateway",
        ),
        (
            {"CODEX_API_KEY": SENTINEL, "AH_CX_API_BASE_URL": "", "OPENAI_BASE_URL": "https://o.example"},
            "CODEX_API_KEY",
            "https://o.example",
            "gateway",
        ),
        ({"OPENAI_BASE_URL": "https://o.example"}, "", "https://o.example", ""),
        ({"CODEX_API_KEY": ""}, "", "", ""),
        ({"AH_CX_TOKEN_alpha": "cx-a"}, "", "", ""),
    ],
)
def test_an_api_key_names_the_slot_and_a_base_url_marks_a_gateway(environ, key, base, label):
    assert codex_api.key_name(environ) == key
    assert codex_api.base_url(environ) == base
    assert codex_api.provider(environ) == label


def test_an_api_key_offers_one_api_slot_without_a_value():
    environ = {"OPENAI_API_KEY": SENTINEL, "OPENAI_BASE_URL": "https://gateway.example/v1"}
    slots = CodexApiSource({"api": 2, "default": 1}).slots(environ, NOW)
    assert slots == [Slot("codex", "api", 0, 2, kind="api", provider="gateway")]
    assert CodexApiSource().slots({"CODEX_API_KEY": SENTINEL}, NOW) == [
        Slot("codex", "api", 0, 0, kind="api", provider="openai-key")
    ]
    assert SENTINEL not in repr(slots)
    assert CodexApiSource({"api": 1}).slots(SUBSCRIPTION, NOW) == []


def test_the_api_argv_names_the_key_variable_and_never_its_value():
    account = router.api_account({**SUBSCRIPTION, "CODEX_API_KEY": SENTINEL})
    assert account == router.CodexAccount(API_ACCOUNT, key_env="CODEX_API_KEY")
    argv = router.command(account, "/usr/bin/codex", ["exec", "hi"])
    assert argv == [
        "/usr/bin/codex",
        "--no-daemon",
        "-c",
        'model_provider="agentihooks-api"',
        *_override("name", "agentihooks-api"),
        *_override("base_url", "https://api.openai.com/v1"),
        *_override("env_key", "CODEX_API_KEY"),
        *_override("wire_api", "responses"),
        "exec",
        "hi",
    ]
    assert SENTINEL not in " ".join(argv)
    gateway = router.api_account({"OPENAI_API_KEY": SENTINEL, "AH_CX_API_BASE_URL": "http://127.0.0.1:9/v1"})
    assert router.command(gateway, "codex", [])[6:10] == [
        *_override("base_url", "http://127.0.0.1:9/v1"),
        *_override("env_key", "OPENAI_API_KEY"),
    ]


def test_an_api_route_without_a_key_names_the_missing_variables():
    with pytest.raises(router.RoutingError) as refused:
        router.api_account(SUBSCRIPTION)
    assert str(refused.value) == "Codex account 'api' needs CODEX_API_KEY or OPENAI_API_KEY"


@pytest.mark.parametrize(
    ("url", "carries"),
    [
        ("https://gw.example/v1", False),
        ("http://127.0.0.1:9/v1", False),
        ("", False),
        ("https://user@gw.example/v1", True),
        ("https://:pw@gw.example/v1", True),
        ("https://gw.example/v1?key=x", True),
        ("http://[bad", True),
    ],
)
def test_a_base_url_with_userinfo_or_a_query_carries_credentials(url, carries):
    assert codex_api.carries_credentials(url) is carries


def test_an_api_route_refuses_a_base_url_that_would_put_credentials_in_argv():
    environ = {"CODEX_API_KEY": SENTINEL, "AH_CX_API_BASE_URL": f"https://u:{SENTINEL}@gw.example/v1"}
    with pytest.raises(router.RoutingError) as refused:
        router.api_account(environ)
    assert str(refused.value) == "AH_CX_API_BASE_URL must not carry credentials or a query"
    environ = {"CODEX_API_KEY": SENTINEL, "OPENAI_BASE_URL": f"https://gw.example/v1?token={SENTINEL}"}
    with pytest.raises(router.RoutingError) as refused:
        router.api_account(environ)
    assert str(refused.value) == "OPENAI_BASE_URL must not carry credentials or a query"
    assert codex_api.base_url_name({"OPENAI_BASE_URL": "https://o", "AH_CX_API_BASE_URL": ""}) == "OPENAI_BASE_URL"
    assert codex_api.base_url_name({}) == ""


def test_the_api_slug_is_reserved_from_token_accounts():
    environ = {"AH_CX_TOKEN_api": "cx-api", "AH_CX_TOKEN_alpha": "cx-a", "AH_CX_TOKEN_": "bare", "AH_CX_TOKEN_beta": ""}
    assert router.token_accounts(environ) == [router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")]


def test_an_api_child_carries_no_subscription_token_and_the_route_marker():
    environ = {**SUBSCRIPTION, "AH_CX_TOKEN_": "bare", "CODEX_API_KEY": SENTINEL, "OPENAI_BASE_URL": "https://g/v1"}
    expected = {"HOME": "/home/u", "CODEX_API_KEY": SENTINEL, "OPENAI_BASE_URL": "https://g/v1", API_MARKER: "1"}
    assert router.child_environment(router.api_account(environ), environ) == expected
    assert CodexApiSource().child_env(Slot("codex", API_ACCOUNT, 0, 0, kind=API), environ) == expected
    assert API_MARKER not in environ


def test_subscription_and_default_children_carry_no_api_credential():
    environ = {**SUBSCRIPTION, **API_NAMES}
    token = router.child_environment(router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"), environ)
    assert token == {"HOME": "/home/u", "AH_CX_TOKEN_alpha": "cx-a", "CODEX_ACCESS_TOKEN": "cx-a"}
    assert router.child_environment(router.CodexAccount("default"), environ) == {"HOME": "/home/u"}
    assert envs.codex_subscription_child({"AH_CX_API_BASE_URL": "https://a", **API_NAMES}) == {
        "AH_CX_API_BASE_URL": "https://a"
    }


def test_the_login_status_check_runs_without_api_credentials():
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert router.default_signed_in({**SUBSCRIPTION, **API_NAMES}, run) is True
    assert seen == {"HOME": "/home/u"}


def _profile_home(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5"\n')
    shared = tmp_path / "shared-auth.json"
    shared.write_text('{"auth_mode": "chatgpt"}\n')
    (home / "auth.json").symlink_to(shared)
    return home


def _bytes(home):
    return {name: (home / name).read_bytes() for name in ("config.toml", "auth.json")}


def _route_api(monkeypatch, tmp_path, environ, args, execvpe):
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {"api": 1})

    def no_run(argv, **kwargs):
        raise AssertionError("an api route checks no login and probes no quota")

    report = tmp_path / "launch.route"
    rc = router.main(["--agentihooks-report", str(report), "--route", "api", *args], environ, execvpe, no_run)
    return rc, report


def test_an_api_launch_routes_forced_and_writes_nothing_to_the_profile_home(monkeypatch, tmp_path, capsys):
    home = _profile_home(tmp_path)
    before = _bytes(home)
    seen = {}

    def execvpe(path, cmd, env):
        seen.update(path=path, cmd=cmd, env=env)

    monkeypatch.setattr(router.shutil, "which", lambda name: f"/usr/bin/{name}")
    environ = {**SUBSCRIPTION, "HOME": str(tmp_path), "CODEX_HOME": str(home), "CODEX_API_KEY": SENTINEL}
    rc, report = _route_api(monkeypatch, tmp_path, environ, ["-m", "o3"], execvpe)
    assert rc == 0
    assert seen["cmd"][:2] == ["/usr/bin/codex", "--no-daemon"] and seen["cmd"][-2:] == ["-m", "o3"]
    assert seen["env"][API_MARKER] == "1" and "CODEX_ACCESS_TOKEN" not in seen["env"]
    assert report.read_text() == "status=routed\naccount=api\nplacement=forced\n"
    out = capsys.readouterr()
    assert "account=api sessions=1/? placement=forced" in out.out
    assert SENTINEL not in out.out + out.err + " ".join(seen["cmd"])
    assert _bytes(home) == before and (home / "auth.json").is_symlink()


@pytest.mark.skipif(shutil.which("codex") is None, reason="needs the codex CLI")
def test_an_api_child_runs_codex_exec_against_a_fake_responses_endpoint(monkeypatch, tmp_path):
    home = _profile_home(tmp_path)
    before = _bytes(home)
    value = secrets.token_hex(8)
    runs = []

    def execvpe(path, cmd, env):
        runs.append(
            subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=tmp_path
            )
        )

    args = ["exec", "--json", "--skip-git-repo-check", "Reply OK"]
    with FakeResponses() as endpoint:
        environ = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "CODEX_HOME": str(home),
            "CODEX_API_KEY": value,
            "AH_CX_API_BASE_URL": endpoint.base_url,
        }
        rc, _ = _route_api(monkeypatch, tmp_path, environ, args, execvpe)
    assert rc == 0 and runs[0].returncode == 0, runs[0].stderr
    assert '"type":"agent_message","text":"OK"' in runs[0].stdout
    assert endpoint.requests == [("POST", "/v1/responses", f"Bearer {value}")]
    assert _bytes(home) == before and (home / "auth.json").is_symlink()
