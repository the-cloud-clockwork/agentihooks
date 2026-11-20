from pathlib import Path

import pytest
import yaml

from scripts import ci_hive_nats_grants as grants
from tests import test_ci_hive_nats_grants as fixtures

pytestmark = pytest.mark.feature("lab-scheduler-nats-grants")

STREAM = "AGENTIHOOKS-CI-LAB-HEADS"
DURABLE = "ci-lab-scheduler"
USER = "swarm-ci-lab-scheduler"
SOURCES = {"scripts/events/nats.py": "LAB_STREAM", "scripts/ci_lab/scheduler.py": "DURABLE"}


def _sources(root: Path, stream: str = STREAM, durable: str = DURABLE) -> None:
    for (source, name), value in zip(SOURCES.items(), (stream, durable), strict=True):
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name} = {value!r}\nraise RuntimeError('candidate code must not execute')\n")


def _subjects(stream: str = STREAM, durable: str = DURABLE) -> list[str]:
    return [
        f"$JS.API.CONSUMER.INFO.{stream}.{durable}",
        f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{durable}",
        f"$JS.ACK.{stream}.{durable}.>",
    ]


def _matching(tmp_path: Path, subjects: list[str] | None = None) -> tuple[Path, Path]:
    agentihooks = fixtures._agentihooks(tmp_path, {"allow": fixtures.PUBLISH}, {"allow": fixtures.SUBSCRIBE})
    antoncore = fixtures._operator_antoncore(
        tmp_path, {"publish": {"allow": fixtures.PUBLISH}, "subscribe": {"allow": fixtures.SUBSCRIBE}}
    )
    _sources(agentihooks)
    path = antoncore / grants.SERVICE_USERS
    values = yaml.safe_load(path.read_text())
    values["users"].append(
        {
            "user": USER,
            "openbao": "k8s/lab",
            "permissions": {
                "publish": {"allow": _subjects() if subjects is None else subjects},
                "subscribe": {"allow": ["_INBOX.>"]},
            },
        }
    )
    path.write_text(yaml.safe_dump(values))
    return agentihooks, antoncore


def _run(capsys: pytest.CaptureFixture[str], agentihooks: Path, antoncore: Path) -> tuple[int, str]:
    status = grants.main(["--agentihooks", str(agentihooks), "--antoncore", str(antoncore)])
    return status, capsys.readouterr().out


def test_scheduler_grants_accept_matching_source_without_executing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, output = _run(capsys, *_matching(tmp_path, [*_subjects(), "ci.lab.>", "$JS.ACK.other.relay.>"]))
    assert status == 0
    assert "lab scheduler" in output
    assert "3 publish and 1 subscribe" in output


@pytest.mark.parametrize("missing", range(3), ids=["info", "next", "ack"])
def test_scheduler_grants_reject_each_missing_durable_subject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: int
) -> None:
    subjects = _subjects()
    omitted = subjects.pop(missing)
    status, output = _run(capsys, *_matching(tmp_path, subjects))
    assert status == 1
    assert omitted in output
    assert "scheduler" in output


@pytest.mark.parametrize("renamed", ["stream", "durable"])
@pytest.mark.parametrize("matching", [False, True], ids=["stale-grant", "matching-grant"])
def test_scheduler_grants_follow_candidate_stream_and_durable_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], renamed: str, matching: bool
) -> None:
    stream = "RENAMED-LAB-HEADS" if renamed == "stream" else STREAM
    durable = "renamed-scheduler" if renamed == "durable" else DURABLE
    agentihooks, antoncore = _matching(tmp_path, _subjects(stream, durable) if matching else None)
    _sources(agentihooks, stream, durable)
    status, output = _run(capsys, agentihooks, antoncore)
    assert status == (0 if matching else 1)
    if matching:
        assert "lab scheduler" in output
    else:
        assert all(subject in output for subject in _subjects(stream, durable))


def test_scheduler_grants_require_the_scheduler_user(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    path = antoncore / grants.SERVICE_USERS
    values = yaml.safe_load(path.read_text())
    values["users"] = [user for user in values["users"] if user["user"] != USER]
    path.write_text(yaml.safe_dump(values))
    status, output = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert f"has no {USER} user" in output


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("body", [None, "not valid Python!", "OTHER = 'unused'", "{name} = 7", "{name} = ''"])
def test_scheduler_grants_fail_closed_on_unreadable_source_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, body: str | None
) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    path = agentihooks / source
    if body is None:
        path.unlink()
    else:
        path.write_text(body.replace("{name}", SOURCES[source]))
    status, output = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert "cannot be read" in output


@pytest.mark.parametrize("direction,rule", [("subscribe", "allow"), ("publish", "deny"), ("subscribe", "deny")])
def test_scheduler_grants_reject_missing_inbox_or_denied_permissions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], direction: str, rule: str
) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    path = antoncore / grants.SERVICE_USERS
    values = yaml.safe_load(path.read_text())
    scheduler = next(user for user in values["users"] if user["user"] == USER)
    scheduler["permissions"][direction][rule] = [] if rule == "allow" else [">"]
    path.write_text(yaml.safe_dump(values))
    status, output = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert direction in output
