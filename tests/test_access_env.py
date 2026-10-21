import io
import json
import os
import shlex
import subprocess
import sys
from urllib.error import HTTPError, URLError

import pytest

from scripts import access_env, init_agent, install
from scripts.cli_delegates import delegated_cli

pytestmark = pytest.mark.unit

ID_NAME, PART_NAME = "SWARM_ACCESS_CLIENT_ID", "SWARM_ACCESS_CLIENT_SECRET"
ID_FIELD, PART_FIELD = "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"
BAO = {"OPENBAO_URL": "https://bao.example/", "OPENBAO_TOKEN": "b0"}
RECORD = {ID_FIELD: "i1.access", PART_FIELD: "p 'x'", "OTHER": "kept-out"}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(seen, record=None, error=None):
    def fake(request, timeout):
        seen.append((request.full_url, request.get_header("X-vault-token"), timeout))
        if error:
            raise error
        return _Response(json.dumps({"data": {"data": record, "metadata": {"version": 3}}}).encode())

    return fake


def test_values_read_the_default_record_and_name_the_swarm_variables():
    seen = []

    found = access_env.values(BAO, _opener(seen, RECORD))

    assert found == {ID_NAME: "i1.access", PART_NAME: "p 'x'"}
    assert seen == [("https://bao.example/v1/secret/data/cloudflare-access-service-token", "b0", 20)]


def test_the_record_path_setting_moves_the_read():
    seen = []

    access_env.values({**BAO, "AGENTIHOOKS_ACCESS_TOKEN_PATH": "ops/access"}, _opener(seen, RECORD))

    assert seen[0][0] == "https://bao.example/v1/secret/data/ops/access"


@pytest.mark.parametrize("field", [ID_FIELD, PART_FIELD])
@pytest.mark.parametrize("value", ["", None, 7])
def test_a_record_without_both_fields_gives_nothing(field, value):
    record = {**RECORD, field: value}

    assert access_env.values(BAO, _opener([], record)) == {}


@pytest.mark.parametrize("unset", ["OPENBAO_URL", "OPENBAO_TOKEN"])
def test_without_openbao_nothing_is_read(unset):
    seen = []
    environ = {key: value for key, value in BAO.items() if key != unset}

    assert access_env.values(environ, _opener(seen, RECORD)) == {}
    assert seen == []


def test_an_unreachable_openbao_gives_nothing_and_says_so(capsys):
    assert access_env.values(BAO, _opener([], error=URLError("down"))) == {}
    assert capsys.readouterr().err == "agentihooks access: OpenBao request failed: down\n"


def test_a_missing_record_gives_nothing_quietly(capsys):
    missing = HTTPError("https://bao.example", 404, "not found", {}, io.BytesIO(b'{"errors": []}'))

    assert access_env.values(BAO, _opener([], error=missing)) == {}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("asked", "waited"), [(20, 3), (1, 1)])
def test_the_default_opener_caps_the_wait(monkeypatch, asked, waited):
    seen = []
    monkeypatch.setattr(access_env, "urlopen", lambda request, timeout: seen.append((request, timeout)) or "opened")

    assert access_env._opener("request", timeout=asked) == "opened"
    assert seen == [("request", waited)]


def test_env_prints_one_quoted_export_per_variable(monkeypatch, capsys):
    monkeypatch.setattr(
        access_env, "values", lambda environ: {PART_NAME: "s 'q'", ID_NAME: "i"} if environ is os.environ else {}
    )

    assert delegated_cli(["access"]) is access_env.main
    assert access_env.main(["env"]) == 0
    out = capsys.readouterr().out
    assert out == f"export {ID_NAME}=i\nexport {PART_NAME}='s '\"'\"'q'\"'\"''\n"
    shell = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f'eval "$1"; printf %s "${PART_NAME}"', "_", out],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shell.stdout == "s 'q'"


@pytest.mark.parametrize("argv", [[], ["show"], ["env", "extra"]])
def test_anything_but_env_prints_usage(argv, capsys):
    assert access_env.main(argv) == 2
    assert capsys.readouterr().err == "usage: agentihooks access env\n"


def _launcher(tmp_path, environ):
    env = {"XDG_RUNTIME_DIR": str(tmp_path / "run"), "SHELL": "/bin/bash", "HOME": str(tmp_path), **environ}
    launcher, _ = init_agent._write_launcher(tmp_path, "grace", "", [], env, init_agent.AgentSpec())
    return launcher.read_text()


def test_the_launcher_reads_the_access_token_at_start_and_holds_no_value(tmp_path):
    state = tmp_path / ".agentihooks"
    state.mkdir()
    (state / ".env").write_text("OPENBAO_URL=https://bao.example\n")

    text = _launcher(tmp_path, {PART_NAME: "held-one", PART_FIELD: "held-two"})

    command = shlex.join([sys.executable, "-m", "scripts.access_env", "env"])
    line = f'eval "$({command})"\n'
    assert init_agent._access_export() == line
    assert line in text.splitlines(keepends=True)
    assert text.index("set -a; for f in") < text.index(line) < text.index("sleep 3")
    assert "held-one" not in text and "held-two" not in text


def test_the_shell_block_loads_the_access_token_after_the_accounts(tmp_path, monkeypatch):
    state = tmp_path / ".agentihooks"
    state.mkdir()
    bashrc = tmp_path / ".bashrc"
    monkeypatch.setattr(install, "_BASHRC", bashrc)
    monkeypatch.setattr(install, "_ENV_FILE_DST", state / ".env")

    install._update_bashrc_block()

    loader = (
        'command -v agentihooks >/dev/null && eval "$(agentihooks accounts env)" && eval "$(agentihooks access env)"\n'
    )
    assert bashrc.read_text().count(loader) == 1
