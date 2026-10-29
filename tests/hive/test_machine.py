import subprocess

import pytest

from scripts.hive import auth, cli, machine, registry

PUBLIC = "redis://hive.example:6380/2"
TARGET = "ssh://worker@10.10.30.207:2222"
URL = "https://hive.example:8770"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


class Runner:
    def __init__(self, *results, saved="[]"):
        self.calls = []
        self.listed = []
        self.results = list(results)
        self.saved = saved

    def __call__(self, argv, **kwargs):
        if argv == list(machine.LIST):
            self.listed.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, self.saved, "")
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(argv, result[0], "", result[1])


def _members(admin):
    return [f"{auth.PREFIX}:member:{member}" for member in sorted(auth.members(admin))]


def _add(admin, run):
    return machine.add(admin, "root", TARGET, "anton-hive", machine.Delivery(URL, PUBLIC), run)


def _herdr_call():
    return (
        ["herdr", "machine", "add", "--label", "anton-hive", "--remote-session", "default", TARGET],
        {"input": None, "capture_output": True, "text": True, "timeout": 300, "check": False},
    )


@pytest.mark.parametrize("pairs", [None, ["ui=no"]])
def test_a_headless_or_unregistered_local_hive_adds_nothing(admin, pairs):
    if pairs:
        registry.update(admin, "root", pairs)
    run = Runner()

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, run)

    assert str(refused.value) == "hive root is headless; only a UI hive adds machines"
    assert run.calls == []
    assert _members(admin) == []


def test_a_ui_hive_saves_the_machine_then_delivers_a_minted_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((0, ""), (0, ""))

    grant = _add(admin, run)

    assert run.calls == [
        _herdr_call(),
        (
            ["ssh", "-o", "BatchMode=yes", TARGET, machine.DELIVER],
            {"input": auth.env_text(URL, grant), "capture_output": True, "text": True, "timeout": 60, "check": False},
        ),
    ]
    assert run.listed == [{"input": None, "capture_output": True, "text": True, "timeout": 300, "check": False}]
    assert grant["name"] == "anton-hive"
    assert auth.ledger_member(admin, grant["ledger_credential"]) == grant["id"]
    assert _members(admin) == [f"{auth.PREFIX}:member:{grant['id']}"]


def test_an_already_saved_machine_is_not_added_again_and_still_gets_a_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    saved = '[{"id": "fc2e", "label": "anton-hive", "target": "' + TARGET + '"}, {"target": "ssh://other"}]'
    run = Runner((0, ""), saved=saved)

    grant = _add(admin, run)

    assert [call[0][0] for call in run.calls] == ["ssh"]
    assert _members(admin) == [f"{auth.PREFIX}:member:{grant['id']}"]


def test_another_saved_machine_does_not_stop_the_add(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((0, ""), (0, ""), saved='[{"target": "ssh://other"}]')

    _add(admin, run)

    assert run.calls[0] == _herdr_call()


def test_an_unreadable_machine_list_mints_no_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner(saved="not json")

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, run)

    assert str(refused.value).startswith("herdr machine list answered with output that is not JSON (")
    assert run.calls == []
    assert _members(admin) == []


def test_a_trusted_ledger_ca_is_delivered_after_the_env_file(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((0, ""), (0, ""), (0, ""))

    grant = machine.add(admin, "root", TARGET, "anton-hive", machine.Delivery(URL, PUBLIC, "PEM"), run)

    delivered = {"capture_output": True, "text": True, "timeout": 60, "check": False}
    assert run.calls[1:] == [
        (["ssh", "-o", "BatchMode=yes", TARGET, machine.DELIVER], {"input": auth.env_text(URL, grant), **delivered}),
        (["ssh", "-o", "BatchMode=yes", TARGET, machine.DELIVER_CA], {"input": "PEM", **delivered}),
    ]
    assert machine.DELIVER_CA == (
        'home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && ca="${LEDGER_CA_FILE:-$home/ledger-ca.pem}" '
        '&& mkdir -p "$(dirname "$ca")" && cat > "$ca.new" && mv "$ca.new" "$ca"'
    )


def test_a_failed_ledger_ca_delivery_revokes_the_minted_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((0, ""), (0, ""), (1, "disk full"))

    with pytest.raises(auth.HiveError) as refused:
        machine.add(admin, "root", TARGET, "anton-hive", machine.Delivery(URL, PUBLIC, "PEM"), run)

    assert str(refused.value) == "ssh exited 1: disk full"
    assert _members(admin) == []


def test_the_delivery_writes_a_private_env_file_in_the_remote_home():
    assert machine.DELIVER == (
        'umask 077 && home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && mkdir -p "$home" '
        '&& cat > "$home/hive.env.new" && mv "$home/hive.env.new" "$home/hive.env"'
    )


def test_a_refused_herdr_add_mints_no_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((1, " remote platform detection failed \n"))

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, run)

    assert str(refused.value) == "herdr exited 1: remote platform detection failed"
    assert run.calls == [_herdr_call()]
    assert _members(admin) == []


def test_a_failed_delivery_revokes_the_minted_member(admin):
    registry.update(admin, "root", ["ui=yes"])
    run = Runner((0, ""), (255, "Permission denied (publickey)."))

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, run)

    assert str(refused.value) == "ssh exited 255: Permission denied (publickey)."
    assert _members(admin) == []
    assert admin.acl_users() == ["default"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError("herdr"), "herdr failed (herdr)"),
        (subprocess.TimeoutExpired("herdr", 300), "herdr failed (Command 'herdr' timed out after 300 seconds)"),
    ],
)
def test_a_herdr_that_cannot_run_is_a_refusal(admin, error, message):
    registry.update(admin, "root", ["ui=yes"])

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, Runner(error))

    assert str(refused.value) == message
    assert _members(admin) == []


def test_cli_add_passes_its_settings_and_names_the_member(admin, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(machine, "add", lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "ab12"})
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://own:6379/0")
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: "PEM")

    assert cli.main(["add", TARGET, "--url", URL]) == 0
    assert cli.main(["add", TARGET, "--url", URL, "--redis-url", PUBLIC]) == 0

    named = "swarm-hive-001"
    assert calls == [
        ((admin, "root", TARGET, named, machine.Delivery(URL, "redis://own:6379/0", "PEM")), {}),
        ((admin, "root", TARGET, named, machine.Delivery(URL, PUBLIC, "PEM")), {}),
    ]
    assert capsys.readouterr().out == f"added {TARGET} to herdr as {named}; its hive member is ab12\n" * 2


def test_cli_add_refuses_a_typed_label(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["add", TARGET, "--label", "anton-hive", "--url", URL])

    assert refused.value.code == 2
    assert "unrecognized arguments: --label anton-hive" in capsys.readouterr().err


def test_cli_add_reports_a_refusal(admin, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")

    assert cli.main(["add", TARGET, "--url", URL]) == 1

    assert capsys.readouterr().err == "hive add refused: hive root is headless; only a UI hive adds machines\n"


@pytest.mark.parametrize(
    ("argv", "missing"),
    [
        (["add", TARGET], "--url"),
        (["add", "--url", URL], "target"),
    ],
)
def test_cli_add_needs_its_target_and_url(argv, missing, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(argv)

    assert refused.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(f"the following arguments are required: {missing}")


def test_cli_add_help_names_every_option(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")

    with pytest.raises(SystemExit):
        cli.main(["add", "--help"])

    out = capsys.readouterr().out
    for text in (
        "SSH target herdr machine add dials, such as ssh://worker@host:2222",
        "this hive's endpoint as the machine reaches it, for hive refresh",
        "Redis URL the machine connects to; defaults to this host's",
    ):
        assert f" {text}\n" in out


def test_join_refuses_the_removed_url_and_code_arguments(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["join", URL, "code"])

    assert refused.value.code == 2
    assert "unrecognized arguments: code" in capsys.readouterr().err
