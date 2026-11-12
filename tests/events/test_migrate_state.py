import importlib
import json

import pytest

from scripts.events.config import CREDENTIALS_ENV, URL_ENV
from tests.events.test_nats_migration import buses as buses
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


@pytest.fixture
def command(buses, monkeypatch):
    _, scoped = buses
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv(URL_ENV, scoped._config.url)
    monkeypatch.setenv(CREDENTIALS_ENV, scoped._config.credentials)
    monkeypatch.delenv("AGENTIHOOKS_BUS_BIND", raising=False)
    return importlib.import_module("scripts.events.migrate_state")


def _seed(legacy):
    old = legacy.state("spoke-work")
    first = old.create("owned", b"first")
    second = old.update("owned", b"second", first)
    old.create("foreign", b"never copy")
    return first, second


def _run(command, capsys, arguments):
    code = command.main(arguments)
    return code, json.loads(capsys.readouterr().out)


APPLY = ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned", "--apply", "--quiescent"]


@pytest.mark.feature("legacy-state-migration-command")
def test_dry_run_reports_what_it_would_move_and_writes_nothing(buses, command, monkeypatch, capsys):
    from nats.js.client import JetStreamContext

    legacy, scoped = buses
    _seed(legacy)

    async def refuse_write(*args, **kwargs):
        raise AssertionError("dry run attempted stream provisioning")

    with monkeypatch.context() as read_only:
        read_only.setattr(JetStreamContext, "add_stream", refuse_write)
        read_only.setattr(JetStreamContext, "update_stream", refuse_write)
        code, report = _run(command, capsys, ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned"])
    assert code == 0
    assert report == {
        "applied": False,
        "swarm": "rig",
        "bucket": "spoke-work",
        "keys": ["owned"],
        "unowned": ["foreign"],
        "retained": 2,
        "imported": False,
        "would_move": 2,
        "moved": 0,
        "failures": [],
    }
    with pytest.raises(RuntimeError, match="swarm 'rig' bucket 'spoke-work'"):
        scoped.for_swarm("rig")


@pytest.mark.feature("legacy-state-migration-command")
def test_move_copies_owned_history_and_a_rerun_moves_nothing(buses, command, capsys):
    legacy, scoped = buses
    first, second = _seed(legacy)
    code, moved = _run(command, capsys, APPLY)
    assert code == 0
    assert moved["applied"] is True
    assert (moved["retained"], moved["imported"], moved["would_move"], moved["moved"]) == (2, False, 2, 2)
    assert moved["unowned"] == ["foreign"]
    assert moved["failures"] == []
    target = scoped.for_swarm("rig").state("spoke-work")
    assert [(entry.value, entry.revision) for entry in target.watch("owned")] == [
        (b"first", first),
        (b"second", second),
    ]
    assert target.get("foreign") is None

    code, repeated = _run(command, capsys, APPLY)
    assert code == 0
    assert (repeated["retained"], repeated["imported"], repeated["would_move"], repeated["moved"]) == (2, True, 0, 0)
    code, dry = _run(command, capsys, ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned"])
    assert code == 0
    assert (dry["imported"], dry["would_move"], dry["moved"]) == (True, 0, 0)
    assert [entry.value for entry in target.watch("owned")] == [b"first", b"second"]
    assert legacy.state("spoke-work").get("owned").value == b"second"


@pytest.mark.feature("legacy-state-migration-command")
def test_no_keys_claim_clears_the_hub_skip_without_copying(buses, command, capsys):
    legacy, scoped = buses
    _seed(legacy)
    code, report = _run(
        command, capsys, ["--swarm", "rig", "--bucket", "spoke-work", "--no-keys", "--apply", "--quiescent"]
    )
    assert code == 0
    assert (report["keys"], report["unowned"], report["retained"], report["moved"]) == ([], ["foreign", "owned"], 0, 0)
    target = scoped.for_swarm("rig").state("spoke-work")
    assert target.get("owned") is None
    assert target.get("foreign") is None


@pytest.mark.feature("legacy-state-migration-command")
def test_interrupted_move_reports_failure_and_stays_pending(buses, command, monkeypatch, capsys):
    from scripts.events import nats_migration

    legacy, scoped = buses
    _seed(legacy)
    legacy.state("spoke-work").create("later", b"unowned write")
    copy = nats_migration._copy_state

    def interrupted(root, target, records, plan):
        copy(root, target, records[:1], plan)
        raise RuntimeError("interrupted during copy")

    both = ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned", "--key", "foreign"]
    with monkeypatch.context() as broken:
        broken.setattr(nats_migration, "_copy_state", interrupted)
        code, report = _run(command, capsys, [*both, "--apply", "--quiescent"])
    assert code == 1
    assert report["failures"] == ["interrupted during copy"]
    assert (report["would_move"], report["moved"]) == (3, 1)
    code, dry = _run(command, capsys, both)
    assert code == 0
    assert (dry["imported"], dry["retained"], dry["would_move"]) == (False, 3, 2)
    code, resumed = _run(command, capsys, [*both, "--apply", "--quiescent"])
    assert (code, resumed["would_move"], resumed["moved"], resumed["failures"]) == (0, 2, 2, [])
    assert [entry.value for entry in scoped.for_swarm("rig").state("spoke-work").watch("owned")] == [
        b"first",
        b"second",
    ]


@pytest.mark.parametrize(("bucket", "seeded"), [("absent", False), ("empty", True)])
def test_dry_run_of_a_bucket_without_keys_reports_nothing(buses, command, capsys, bucket, seeded):
    legacy, _ = buses
    if seeded:
        legacy.state(bucket)
    code, report = _run(command, capsys, ["--swarm", "rig", "--bucket", bucket])
    assert code == 0
    assert (report["keys"], report["unowned"], report["retained"], report["would_move"]) == ([], [], 0, 0)


def test_dry_run_claims_each_named_key_once_in_order(buses, command, capsys):
    legacy, _ = buses
    _seed(legacy)
    code, report = _run(
        command,
        capsys,
        ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned", "--key", "foreign", "--key", "owned"],
    )
    assert code == 0
    assert (report["keys"], report["unowned"], report["retained"]) == (["foreign", "owned"], [], 3)


def test_dry_run_reads_an_import_kept_under_the_legacy_stream_name(buses, command, capsys):
    from scripts.events.nats_migration import IMPORT_PLAN
    from scripts.events.resource_names import legacy_stream_name
    from scripts.events.subjects import subject

    legacy, _ = buses
    _seed(legacy)
    legacy._loop.run(
        legacy._js.add_stream(
            name=legacy_stream_name("rig", "STATE_spoke-work"),
            subjects=[f"{subject('rig', 'state', 'spoke-work')}.>"],
            metadata={IMPORT_PLAN: "done"},
        )
    )
    code, report = _run(command, capsys, ["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned"])
    assert code == 0
    assert (report["imported"], report["would_move"]) == (True, 0)


def test_dry_run_needs_no_provisioning(command, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_BUS_BIND", "1")
    code, report = _run(command, capsys, ["--swarm", "rig", "--bucket", "absent"])
    assert code == 0
    assert report["applied"] is False


def test_move_counts_a_dotted_key(buses, command, capsys):
    legacy, scoped = buses
    legacy.state("spoke-work").create("claim.one", b"dotted")
    code, report = _run(
        command, capsys, ["--swarm", "rig", "--bucket", "spoke-work", "--key", "claim.one", "--apply", "--quiescent"]
    )
    assert (code, report["would_move"], report["moved"]) == (0, 1, 1)
    assert scoped.for_swarm("rig").state("spoke-work").get("claim.one").value == b"dotted"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--apply"],
        ["--apply", "--quiescent"],
        ["--apply", "--key", "owned"],
        ["--apply", "--no-keys"],
    ],
)
def test_apply_requires_explicit_ownership_and_quiet_writers(command, arguments, capsys):
    with pytest.raises(SystemExit) as error:
        command.main(["--swarm", "rig", "--bucket", "spoke-work", *arguments])
    assert error.value.code == 2
    assert (
        capsys.readouterr()
        .err.splitlines()[-1]
        .endswith(": error: --apply requires explicit --key or --no-keys ownership and --quiescent")
    )


def test_keys_and_no_keys_are_exclusive(command, capsys):
    with pytest.raises(SystemExit) as error:
        command.main(["--swarm", "rig", "--bucket", "spoke-work", "--key", "owned", "--no-keys"])
    assert error.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize(("backend", "bind"), [("memory", "0"), ("nats", "1")])
def test_apply_requires_privileged_nats(command, monkeypatch, capsys, backend, bind):
    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    monkeypatch.setenv("AGENTIHOOKS_BUS_BIND", bind)
    with pytest.raises(SystemExit) as error:
        command.main(APPLY)
    assert error.value.code == 2
    assert (
        capsys.readouterr()
        .err.splitlines()[-1]
        .endswith(": error: migration requires the NATS backend and privileged provisioning for --apply")
    )


def test_help_explains_ownership_and_quiet_window(command, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["migrate-state"])
    with pytest.raises(SystemExit) as error:
        command.main(["--help"])
    assert error.value.code == 0
    assert " ".join(capsys.readouterr().out.split()) == (
        "usage: migrate-state [-h] --swarm SWARM --bucket BUCKET [--key KEY | --no-keys] [--apply] [--quiescent] "
        "Inventory or migrate one swarm's legacy state bucket to its scoped state stream options: "
        "-h, --help show this help message and exit "
        "--swarm SWARM The swarm that owns the migrated keys "
        "--bucket BUCKET The legacy key value bucket to migrate "
        "--key KEY A legacy key this swarm owns; repeat for more "
        "--no-keys Claim that this swarm owns no legacy key "
        "--apply Move the owned keys; default is a read only dry run "
        "--quiescent Confirm legacy writers of the bucket are quiet"
    )
