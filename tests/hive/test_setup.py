import json
import os
import subprocess

import pytest

import scripts.hive.setup as setup
from scripts.hive import auth, bundle_origin, cli
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

pytestmark = pytest.mark.unit
RECORD = "/v1/secret/data/k8s/agentihooks-swarm-workstation-hive-settings"
BUNDLE_URL = "https://github.com/the-cloud-clockwork/agentihooks-bundle.git"

LIVE_KEYS = (
    "AGENTIHOOKS_AICODE_TOKEN_REFRESH",
    "AGENTIHOOKS_BASE_CHANNELS",
    "AGENTIHOOKS_CLASSIFIER_LITELLM_KEY",
    "AGENTIHOOKS_CLASSIFIER_URL",
    "AGENTIHOOKS_HANDOFF_WEEK_PCT",
    "AGENTIHOOKS_LANGFUSE_ENABLED",
    "AGENTIHOOKS_MASTER_CLAUDE_LOGIN",
    "AGENTIHOOKS_METRICS_PASSWORD",
    "AGENTIHOOKS_METRICS_URL",
    "AGENTIHOOKS_METRICS_USER",
    "AGENTIHOOKS_OTLP_ENDPOINT",
    "AGENTIHOOKS_OTLP_PROTOCOL",
    "AGENTIHOOKS_PUSH_TOKEN",
    "AGENTIHOOKS_PUSH_URL",
    "AGENTIHOOKS_RESERVE_ACCOUNTS",
    "AGENTIHOOKS_SECRETS_MODE",
    "BRAIN_CHANNEL",
    "BRAIN_ENABLED",
    "BRAIN_HOT_ARCS_TOP_N",
    "BRAIN_HTTP_TIMEOUT",
    "BRAIN_PAYLOAD_MAX_BYTES",
    "BRAIN_REFRESH_TOOL_CALLS",
    "BRAIN_SOURCE_TYPE",
    "BRAIN_WRITER_ENABLED",
    "BRAIN_WRITER_MAX_MARKERS",
    "GITHUB_API_BASE",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "LANGFUSE_HOST",
    "LANGFUSE_MCP_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LITELLM_MASTER_KEY",
    "LITELLM_MCP_GATEWAY_KEY",
    "LITELLM_URL",
    "MCP_ATLASSIAN_PROXY_API_KEY",
    "MCP_CONNECTOR_FACTORY_PROXY_API_KEY",
    "MCP_HYGIENE_ENABLED",
    "MCP_KEY_GATEWAY",
    "MCP_OAUTH_VIRTUAL_KEY",
    "MCP_PROTOCOL_NEGOTIATION",
    "MCP_SONAR_PROXY_API_KEY",
    "OPENBAO_TOKEN",
    "OPENBAO_URL",
    "REDIS_URL",
)

LEFT_OUT = (
    ("AGENTIHOOKS_VAULT_PATH", "/home/operator/vault", "host path"),
    ("AGENTIHOOKS_SCRATCH_ROOT", "~/scratchpad", "host path"),
    ("AGENTIHOOKS_SWARM_REDIS_URL", "redis://127.0.0.1:6379/0", "host local address"),
    ("BRAIN_URL", "http://localhost:8080", "host local address"),
    ("AGENTIHOOKS_LEDGER_URL", "http://0.0.0.0:8765", "host local address"),
    ("AGENTIHOOKS_OLLAMA_URL", "[::1]:11434", "host local address"),
    ("AGENTIHOOKS_LINK_LOCAL_URL", "http://169.254.169.254/latest", "host local address"),
    ("AH_CC_TOKEN_operator", "fixture-claude", "account"),
    ("AH_CX_TOKEN_work", "fixture-codex", "account"),
    ("SWARM_" + "SECRET", "fixture-tick", "tick only"),
    ("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "fixture-ledger-agent", "tick only"),
)
REASONS = {name: reason for name, _, reason in LEFT_OUT}
WRITTEN = sorted([*LIVE_KEYS, "HIVE_BUNDLE_URL", "HIVE_BUNDLE_REF"])
MEMBER = "fixture-member"


def live_value(name):
    return f"fixture-value-of-{name.lower()}"


def bundle(tmp_path):
    checkout = tmp_path / "bundle"
    subprocess.run(["git", "init", "--initial-branch=dev", str(checkout)], check=True, capture_output=True)
    remote = "git@github.com:the-cloud-clockwork/agentihooks-bundle.git"
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", remote], check=True)
    return checkout


def workstation(tmp_path, linked=True):
    home = tmp_path / "state"
    home.mkdir()
    if linked:
        (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle(tmp_path))}}))
    names = [*LIVE_KEYS, *REASONS]
    half = len(names) // 2
    (home / ".env").write_text("".join(f"{name}=file-value\n" for name in names[:half]))
    (home / "companion.env").write_text("".join(f"export {name}='file-value'\n" for name in names[half:]))
    (home / "hive.env").write_text(f"AGENTIHOOKS_HIVE_ID=member\nAGENTIHOOKS_HIVE_LEDGER_CREDENTIAL={MEMBER}\n")
    (home / "hive-container.env").write_text("AGENTIHOOKS_HIVE_URL=https://hive.example.test\n")
    environ = {name: live_value(name) for name in LIVE_KEYS} | {name: value for name, value, _ in LEFT_OUT}
    environ |= {
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_HIVE_ID": "member",
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": MEMBER,
        "AGENTIHOOKS_HIVE_URL": "https://hive.example.test",
        "PATH": "/usr/bin",
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": BAO_TOKEN,
    }
    return home, environ


def printed_values(report):
    values = [*(live_value(name) for name in LIVE_KEYS), *(value for _, value, _ in LEFT_OUT), BAO_TOKEN, MEMBER]
    return [value for value in values if value in report]


def left_out_text():
    return ", ".join(f"{name} ({reason})" for name, reason in sorted(REASONS.items()))


def test_entries_keep_the_live_record_keys_and_name_each_left_out_key_with_its_reason(tmp_path):
    home, environ = workstation(tmp_path)
    kept, left_out = setup.entries(environ, home)
    assert sorted(kept) == WRITTEN
    assert kept["HIVE_BUNDLE_URL"] == BUNDLE_URL
    assert kept["HIVE_BUNDLE_REF"] == "dev"
    assert kept["LITELLM_URL"] == "fixture-value-of-litellm_url"
    assert kept["OPENBAO_URL"] == BAO_URL
    assert left_out == REASONS


def test_a_name_the_environment_does_not_hold_is_skipped(tmp_path):
    home, environ = workstation(tmp_path)
    del environ["LITELLM_URL"]
    kept, left_out = setup.entries(environ, home)
    assert "LITELLM_URL" not in kept
    assert "LITELLM_URL" not in left_out


@pytest.mark.parametrize(
    "value",
    ["true", "brain,amygdala", "https://litellm.example.test", "redis://10.0.0.5:6379/0", "http://[bad", ""],
)
def test_values_that_are_not_host_paths_or_local_addresses_are_kept(tmp_path, value):
    home, environ = workstation(tmp_path)
    environ["LITELLM_URL"] = value
    kept, _ = setup.entries(environ, home)
    assert kept["LITELLM_URL"] == value


@pytest.mark.parametrize("host", ["localhost", "api.localhost", "127.0.0.2", "0.0.0.0", "[::1]", "169.254.1.1"])
def test_each_local_address_form_is_left_out(tmp_path, host):
    home, environ = workstation(tmp_path)
    environ["LITELLM_URL"] = f"http://{host}:4000"
    _, left_out = setup.entries(environ, home)
    assert left_out["LITELLM_URL"] == "host local address"


def test_publish_merges_the_full_record_and_prints_only_key_names(tmp_path):
    home, environ = workstation(tmp_path)
    fake = FakeBao({"KEPT_BY_HAND": "fixture-kept"}, version=2, path=RECORD)
    report = setup.publish(environ, bundle_origin.SETTINGS_PATH, False, fake)
    assert report == (
        f"OpenBao secret/k8s/agentihooks-swarm-workstation-hive-settings: wrote {', '.join(WRITTEN)} "
        f"({len(WRITTEN) + 1} keys total); left out {left_out_text()}; a bundle is linked"
    )
    assert BUNDLE_URL not in report
    assert fake.data == {"KEPT_BY_HAND": "fixture-kept"} | {name: live_value(name) for name in LIVE_KEYS} | {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": BAO_TOKEN,
        "HIVE_BUNDLE_URL": BUNDLE_URL,
        "HIVE_BUNDLE_REF": "dev",
    }
    assert fake.version == 3
    assert printed_values(report) == []


def test_a_dry_run_prints_only_key_names_and_writes_nothing(tmp_path):
    home, environ = workstation(tmp_path, linked=False)
    fake = FakeBao({"KEPT_BY_HAND": "fixture-kept"}, version=2, path=RECORD)
    report = setup.publish(environ, bundle_origin.SETTINGS_PATH, True, fake)
    assert report == (
        "dry run, nothing written to OpenBao secret/k8s/agentihooks-swarm-workstation-hive-settings: would write "
        f"{', '.join(WRITTEN)}; left out {left_out_text()}; no bundle is linked"
    )
    assert fake.data == {"KEPT_BY_HAND": "fixture-kept"}
    assert fake.version == 2
    assert printed_values(report) == []


def test_a_dry_run_needs_no_openbao_credentials(tmp_path):
    home, environ = workstation(tmp_path)
    del environ["OPENBAO_TOKEN"]
    assert setup.publish(environ, "elsewhere", True).startswith(
        "dry run, nothing written to OpenBao secret/elsewhere: would write "
    )


def test_a_dry_run_with_every_key_kept_leaves_out_nothing(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    (home / ".env").write_text("LITELLM_URL=file-value\n")
    report = setup.publish({"AGENTIHOOKS_HOME": str(home), "LITELLM_URL": "https://litellm.example.test"}, "x", True)
    assert report == (
        "dry run, nothing written to OpenBao secret/x: would write HIVE_BUNDLE_REF, HIVE_BUNDLE_URL, LITELLM_URL; "
        "left out nothing; no bundle is linked"
    )


def test_publish_without_openbao_credentials_is_refused(tmp_path):
    home, environ = workstation(tmp_path)
    del environ["OPENBAO_TOKEN"]
    with pytest.raises(auth.HiveError) as refused:
        setup.publish(environ, "elsewhere", False, FakeBao())
    assert str(refused.value) == "OPENBAO_URL and OPENBAO_TOKEN must both be set"


def test_a_failed_write_is_refused_with_the_openbao_status(tmp_path):
    home, environ = workstation(tmp_path)
    fake = FakeBao({}, path=RECORD, fail_writes=True)
    with pytest.raises(auth.HiveError) as refused:
        setup.publish(environ, bundle_origin.SETTINGS_PATH, False, fake)
    assert str(refused.value) == "OpenBao HTTP 503: sealed"


def test_the_cli_writes_the_named_record_from_the_agent_environment(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup, "publish", lambda env, path, dry: calls.append((env, path, dry)) or "written")
    assert cli.main(["setup"]) == 0
    assert cli.main(["setup", "--path", "k8s/other", "--dry-run"]) == 0
    assert calls == [
        (os.environ, "k8s/agentihooks-swarm-workstation-hive-settings", False),
        (os.environ, "k8s/other", True),
    ]
    assert all(env is os.environ for env, _, _ in calls)
    assert capsys.readouterr().out == "written\nwritten\n"


def test_the_cli_help_names_what_setup_writes():
    sub = next(action for action in cli._parser()._actions if action.dest == "command")
    helps = {choice.dest: choice.help for choice in sub._choices_actions}
    assert helps["setup"] == "Write the workstation agent settings and the bundle origin into the hive settings record"
    options = {action.dest: action.help for action in sub.choices["setup"]._actions}
    assert options["path"] == "OpenBao record under the secret mount"
    assert options["dry_run"] == "Print the key names it would write and leave out, and write nothing"


def test_the_cli_reports_a_refusal(monkeypatch, capsys):
    def refuse(env, path, dry):
        raise auth.HiveError("sealed")

    monkeypatch.setattr(setup, "publish", refuse)
    assert cli.main(["setup"]) == 1
    assert capsys.readouterr().err == "hive setup refused: sealed\n"
