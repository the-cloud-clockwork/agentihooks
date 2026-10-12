import functools
import http.client
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.swarm_v2 import filesystem, worker_home, workspaces
from scripts.swarm_v2.broadcast_bridge import API_URL, GRANT_NAME
from scripts.swarm_v2.checkpoints import restore
from scripts.swarm_v2.supervision import Launch, LaunchRefused
from scripts.swarm_v2.worker import start

pytestmark = pytest.mark.unit

EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
GRANT = "sv2.grant-fixture"
CONTROL = "http://controller.swarm.invalid:8780"
AUTHORITY = {
    "execution_id": EXECUTION,
    "generation": 3,
    "task_id": "t1",
    "seat_id": "eng-1@fixture",
    "swarm_id": "fixture",
    "grant_id": "lgr-" + "7" * 32,
}
RECORD = {
    "schema_version": 1,
    "execution_id": EXECUTION,
    "generation": 3,
    "authority": AUTHORITY,
    "harness": "claude",
    "agent": ["claude"],
    "control_url": CONTROL,
    "profile": "engineer",
}
ANSWER = {
    "execution_id": EXECUTION,
    "task_id": "t1",
    "task_generation": 1,
    "grant_id": AUTHORITY["grant_id"],
    "registered_at": "2026-10-11T00:00:00Z",
}


def material(tmp_path: Path, record: object = RECORD, grant: str | None = GRANT) -> Path:
    folder = tmp_path / "launch"
    folder.mkdir()
    path = folder / "launch.json"
    path.write_text(record if isinstance(record, str) else json.dumps(record))
    if grant is not None:
        (folder / GRANT_NAME).write_text(grant + "\n")
    return path


class Controller:
    def __init__(self, *answers: tuple[int, dict]) -> None:
        self.answers, self.calls, self.sleeps = list(answers), [], []

    def send(self, url: str, grant: str, body: dict) -> tuple[int, dict]:
        self.calls.append((url, grant, body))
        return self.answers.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class Boot:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request: worker_home.Request) -> dict:
        self.requests.append(request)
        (request.root / request.attempt / "run").mkdir(parents=True)
        return {}


def shipped(tmp_path: Path) -> Path:
    templates = tmp_path / "templates"
    (templates / "engineer").mkdir(parents=True, exist_ok=True)
    return templates


def image_templates(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    templates = tmp_path / "image-templates"
    for line in (root / "docker" / "swarm-node" / "Dockerfile").read_text().splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and words[-1] == f"{start.TEMPLATES}/":
            shutil.copytree(root / words[1], templates, dirs_exist_ok=True)
    return templates


def prepare(tmp_path, launch, controller, boot=None):
    boot = boot or Boot()
    start.prepare(tmp_path / "attempts" / EXECUTION, launch, controller.send, controller.sleep, boot, shipped(tmp_path))
    return boot


def refusal(tmp_path, launch, controller=None) -> str:
    controller = controller or Controller((200, ANSWER))
    boot = Boot()
    with pytest.raises(LaunchRefused) as refused:
        prepare(tmp_path, launch, controller, boot)
    assert boot.requests == []
    assert not (tmp_path / "attempts").exists()
    return str(refused.value)


def test_a_prepared_attempt_holds_the_registration_record_only_the_worker_can_read(tmp_path):
    controller = Controller((200, ANSWER))

    boot = prepare(tmp_path, material(tmp_path), controller)

    attempt = tmp_path / "attempts" / EXECUTION
    record = attempt / "registration.json"
    assert json.loads(record.read_text()) == AUTHORITY
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "attempts").stat().st_mode) == 0o700
    assert (attempt / "run" / GRANT_NAME).read_text() == GRANT
    assert not (attempt / "registration.json.tmp").exists()
    assert controller.calls == [(CONTROL, GRANT, {"execution_id": EXECUTION, "generation": 3})]
    assert controller.sleeps == []
    assert boot.requests == [
        worker_home.Request(
            tmp_path / "attempts",
            EXECUTION,
            tmp_path / "templates",
            {"claude": "engineer"},
            Path(sys.executable),
            {},
            {API_URL: CONTROL},
            os.getuid(),
            os.getgid(),
        )
    ]


def test_an_engineer_launch_record_renders_the_engineer_profile_in_the_pod_home(tmp_path):
    attempt = tmp_path / "attempts" / EXECUTION
    controller = Controller((200, ANSWER))

    start.prepare(attempt, material(tmp_path), controller.send, controller.sleep, templates=image_templates(tmp_path))

    record = json.loads((attempt / "execution.json").read_text())
    assert record["profiles"] == {"claude": "engineer"}
    home = attempt / record["homes"]["claude"] / ".claude"
    assert "# Role: Swarm Engineer\n" in (home / "CLAUDE.md").read_text()
    assert (home / "rules" / "engineer-role.md").is_file()
    assert (home / "skills" / "swarm-engineer" / "SKILL.md").is_file()


BUNDLE = Path(__file__).parent / "fixtures" / "swarm_v2" / "worker-bundle"


def bundled(tmp_path: Path, profile: str) -> tuple[dict, Path]:
    attempt = tmp_path / "attempts" / EXECUTION
    controller = Controller((200, ANSWER))
    launch = material(tmp_path, {**RECORD, "profile": profile})

    start.prepare(
        attempt, launch, controller.send, controller.sleep, templates=image_templates(tmp_path), bundle=BUNDLE
    )

    record = json.loads((attempt / "execution.json").read_text())
    return record, attempt / record["homes"]["claude"]


@pytest.mark.parametrize(
    ("profile", "chain"),
    [
        ("engineer", ["anton-base", "package:engineer", "engineer", "brain"]),
        ("frontend", ["anton-base", "package:engineer", "engineer", "frontend", "brain"]),
    ],
)
def test_a_bundle_seat_renders_the_local_overlay_chain_in_the_pod_home(tmp_path, profile, chain):
    record, home = bundled(tmp_path, profile)

    assert record["chains"] == {"claude": chain}
    persona = (home / ".claude" / "CLAUDE.md").read_text()
    for text in ("Fixture bundle global persona", "Fixture anton base persona", "# Role: Swarm Engineer"):
        assert text in persona
    assert "Fixture bundle engineer persona" in persona
    assert "Fixture brain persona" in persona
    assert ("Fixture bundle frontend persona" in persona) is (profile == "frontend")
    rules = home / ".claude" / "rules"
    for rule in ("fixture-global-rule.md", "fixture-base-rule.md", "engineer-role.md"):
        assert (rules / rule).is_file()
    assert (home / ".claude" / "skills" / "fixture-global-skill" / "SKILL.md").is_file()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert ("FIXTURE_FRONTEND" in settings["env"]) is (profile == "frontend")
    assert "brain" in settings["env"]["AGENTIHOOKS_BASE_CHANNELS"].split(",")


def test_a_bundle_seat_records_the_delivered_bundle_revision_and_digest(tmp_path):
    record, _ = bundled(tmp_path, "frontend")

    assert record["bundle"]["revision"] == json.loads((BUNDLE / "revision.json").read_text())
    assert record["bundle"]["digest"] == worker_home.bundle_digest(BUNDLE)
    assert "frontend" not in record["profile_digests"]


def test_a_bundle_seat_runs_the_local_browser_server_and_keeps_its_header_references(tmp_path):
    _, home = bundled(tmp_path, "engineer")

    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["playwright-cmd"]["command"] != "cmd.exe"
    assert servers["gateway-tools"]["headers"] == {"Authorization": "Bearer ${MCP_KEY_FIXTURE}"}


def test_a_profile_in_neither_the_image_nor_the_bundle_is_refused(tmp_path):
    controller, boot = Controller((200, ANSWER)), Boot()
    launch = material(tmp_path, {**RECORD, "profile": "designer"})

    with pytest.raises(LaunchRefused) as refused:
        start.prepare(
            tmp_path / "attempts" / EXECUTION,
            launch,
            controller.send,
            controller.sleep,
            boot,
            shipped(tmp_path),
            BUNDLE,
        )

    assert str(refused.value) == "profile designer is not shipped in the worker image or the bundle"
    assert boot.requests == []


def test_a_bundle_profile_reaches_bootstrap_with_the_delivered_bundle(tmp_path):
    controller = Controller((200, ANSWER))
    launch = material(tmp_path, {**RECORD, "profile": "frontend"})
    boot = Boot()

    start.prepare(
        tmp_path / "attempts" / EXECUTION, launch, controller.send, controller.sleep, boot, shipped(tmp_path), BUNDLE
    )

    [request] = boot.requests
    assert (request.profiles, request.bundle) == ({"claude": "frontend"}, BUNDLE)


def test_without_a_delivered_bundle_bootstrap_gets_none(tmp_path):
    boot = prepare(tmp_path, material(tmp_path), Controller((200, ANSWER)))

    assert boot.requests[0].bundle is None


def test_the_public_image_ships_no_bundle_profile(tmp_path):
    templates = image_templates(tmp_path)

    assert sorted(p.name for p in (BUNDLE / "bundle" / "profiles").iterdir() if (templates / p.name).exists()) == [
        "engineer"
    ]
    assert not (templates / "frontend").exists() and not (templates / "anton-base").exists()
    assert (templates / "engineer" / "profile.yml").read_text() != (
        BUNDLE / "bundle" / "profiles" / "engineer" / "profile.yml"
    ).read_text()


def test_the_public_image_copies_only_paths_of_this_repository():
    root = Path(__file__).resolve().parents[1]
    sources = [
        word
        for line in (root / "docker" / "swarm-node" / "Dockerfile").read_text().splitlines()
        if line.split()[:1] == ["COPY"]
        for word in line.split()[1:-1]
        if not word.startswith("--")
    ]

    assert "profiles/" in sources
    assert all((root / source).exists() and "bundle" not in source for source in sources)


@pytest.mark.parametrize("role", ["engineer", "cicd", "planner", "qa", "master", "dispatcher"])
def test_the_image_ships_every_role_profile_as_a_template(tmp_path, role):
    templates = image_templates(tmp_path)

    assert (templates / role / "profile.yml").is_file()
    assert (templates / "default" / "profile.yml").is_file()


@pytest.mark.parametrize("role", ["planner", "qa"])
def test_a_seeded_role_finds_the_shared_guard_its_conditions_load(tmp_path, role):
    attempt = tmp_path / "attempts" / EXECUTION
    controller = Controller((200, ANSWER))
    launch = material(tmp_path, {**RECORD, "profile": role})

    start.prepare(attempt, launch, controller.send, controller.sleep, templates=image_templates(tmp_path))

    record = json.loads((attempt / "execution.json").read_text())
    state = json.loads((attempt / record["homes"]["claude"] / ".agentihooks" / "state.json").read_text())
    [linked] = state["linked_profiles"]
    conditions = sorted((Path(linked["path"]) / ".claude" / "conditions").glob("*no_code_edits.py"))
    assert linked["name"] == role and conditions
    for condition in conditions:
        assert (condition.parents[3] / "_guards" / "no_code_edits.py").is_file()


@pytest.mark.parametrize("kind", ["file", "link"])
def test_a_profile_template_that_is_not_a_folder_is_not_shipped(tmp_path, kind):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "shipped").mkdir()
    if kind == "file":
        (templates / "engineer").write_text("")
    else:
        (templates / "engineer").symlink_to(templates / "shipped")
    controller, boot = Controller((200, ANSWER)), Boot()

    with pytest.raises(LaunchRefused) as refused:
        start.prepare(
            tmp_path / "attempts" / EXECUTION, material(tmp_path), controller.send, controller.sleep, boot, templates
        )

    assert str(refused.value) == "profile engineer is not shipped in the worker image"
    assert (controller.calls, boot.requests) == ([], [])
    assert not (tmp_path / "attempts").exists()


def test_a_busy_controller_is_asked_again_until_it_answers(tmp_path):
    controller = Controller((503, {}), (503, {}), (200, ANSWER))

    prepare(tmp_path, material(tmp_path), controller)

    assert len(controller.calls) == 3
    assert controller.sleeps == [2, 2]


def test_a_controller_busy_on_every_attempt_refuses_the_start(tmp_path):
    controller = Controller(*[(503, {})] * 10)

    assert refusal(tmp_path, material(tmp_path), controller) == "registration refused with status 503"
    assert len(controller.calls) == 10
    assert controller.sleeps == [2] * 9


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ("{not json", "missing or unreadable launch record"),
        ([], "unsupported launch"),
        ({**RECORD, "schema_version": 2}, "unsupported launch"),
        ({key: value for key, value in RECORD.items() if key != "authority"}, "invalid authority"),
        ({**RECORD, "authority": {**AUTHORITY, "grant_id": "forged"}}, "invalid authority"),
        ({**RECORD, "harness": "vim"}, "unsupported harness"),
        ({key: value for key, value in RECORD.items() if key != "control_url"}, "invalid control url"),
        ({**RECORD, "control_url": "file:///etc/passwd"}, "invalid control url"),
        ({**RECORD, "control_url": "http://"}, "invalid control url"),
        ({**RECORD, "control_url": 8780}, "invalid control url"),
        ({**RECORD, "control_url": "http://controller.swarm.invalid:port"}, "invalid control url"),
        ({key: value for key, value in RECORD.items() if key != "profile"}, "launch record names no profile"),
        ({**RECORD, "profile": ""}, "launch record names no profile"),
        ({**RECORD, "profile": 7}, "launch record names no profile"),
        ({**RECORD, "profile": "../engineer"}, "launch record names an invalid profile"),
        ({**RECORD, "profile": "frontend"}, "profile frontend is not shipped in the worker image"),
    ],
)
def test_a_malformed_launch_record_refuses_before_anything_is_written(tmp_path, record, message):
    assert refusal(tmp_path, material(tmp_path, record)) == message


def test_a_missing_launch_record_refuses(tmp_path):
    assert refusal(tmp_path, tmp_path / "launch" / "launch.json") == "missing or unreadable launch record"


@pytest.mark.parametrize("grant", [None, "  "])
def test_a_missing_launch_grant_refuses(tmp_path, grant):
    assert refusal(tmp_path, material(tmp_path, grant=grant)) == "missing launch grant"


def test_an_attempt_folder_for_another_execution_refuses(tmp_path):
    launch = material(tmp_path, {**RECORD, "authority": {**AUTHORITY, "execution_id": "exe-" + "1" * 32}})

    assert refusal(tmp_path, launch) == "attempt folder does not name the launch execution"


def test_a_refused_registration_refuses_the_start(tmp_path):
    controller = Controller((401, {}))

    assert refusal(tmp_path, material(tmp_path), controller) == "registration refused with status 401"
    assert controller.sleeps == []


@pytest.mark.parametrize("field", ["execution_id", "task_id", "grant_id"])
def test_an_answer_for_another_registration_refuses(tmp_path, field):
    controller = Controller((200, {**ANSWER, field: "other"}))

    assert (
        refusal(tmp_path, material(tmp_path), controller) == "registration answer does not match the launch authority"
    )


def test_an_answer_that_is_not_an_object_refuses(tmp_path):
    controller = Controller((200, []))

    assert (
        refusal(tmp_path, material(tmp_path), controller) == "registration answer does not match the launch authority"
    )


FRESH_RESTORE = {"checkpoint_id": "", "recovery": "fresh", "session_id": "", "watermark": 4096}
NATIVE_RESTORE = {"checkpoint_id": "ckpt-1", "recovery": "native", "session_id": "s-1", "watermark": 4096}


def test_a_launch_naming_no_restore_writes_no_restore_record(tmp_path):
    prepare(tmp_path, material(tmp_path), Controller((200, ANSWER)))
    assert not (tmp_path / "attempts" / EXECUTION / start.RESTORED).exists()
    assert start.RESTORED == "restore.json"


def test_a_fresh_restore_is_recorded_before_the_registration_record(tmp_path):
    prepare(tmp_path, material(tmp_path, {**RECORD, "restore": FRESH_RESTORE}), Controller((200, ANSWER)))
    attempt = tmp_path / "attempts" / EXECUTION
    recorded = attempt / start.RESTORED
    expected = {"recovery": "fresh", "checkpoint_id": "", "watermark": 4096, "session_id": "", "command": []}
    assert json.loads(recorded.read_text()) == expected
    assert stat.S_IMODE(recorded.stat().st_mode) == 0o600
    assert json.loads((attempt / "registration.json").read_text()) == AUTHORITY


def test_the_launch_restore_runs_after_bootstrap_with_the_launch_record(tmp_path, monkeypatch):
    seen = []

    def restoring(spec, attempt):
        seen.append((spec["restore"], (tmp_path / "attempts" / EXECUTION / "run").is_dir()))
        return restore.Restored("native", "ckpt-1", 41, "s-1", ("claude", "--resume", "s-1"))

    launch = material(tmp_path, {**RECORD, "restore": NATIVE_RESTORE})
    attempt = tmp_path / "attempts" / EXECUTION
    monkeypatch.setattr(start.checkpoint_runtime, "prepare", restoring)
    start.prepare(attempt, launch, Controller((200, ANSWER)).send, None, Boot(), shipped(tmp_path))
    assert seen == [(NATIVE_RESTORE, True)]
    recorded = json.loads((attempt / start.RESTORED).read_text())
    assert recorded == {
        "recovery": "native",
        "checkpoint_id": "ckpt-1",
        "watermark": 41,
        "session_id": "s-1",
        "command": ["claude", "--resume", "s-1"],
    }


def test_remote_restore_is_reported_before_receipt_and_registration(tmp_path, monkeypatch):
    attempt = tmp_path / "attempts" / EXECUTION
    spec = {**RECORD, "restore": NATIVE_RESTORE, "checkpoint_remote": True}
    launch = material(tmp_path, spec)
    calls = []
    restored = restore.Restored("native", "ckpt-1", 41, "s-1", ("claude", "--resume", "s-1"))
    monkeypatch.delenv(start.checkpoint_runtime.REMOTE, raising=False)

    def restoring(actual, root):
        assert actual == spec
        assert root == attempt
        assert os.environ[start.checkpoint_runtime.REMOTE] == "true"
        assert json.loads((attempt / "launch.json").read_text()) == spec
        assert stat.S_IMODE((attempt / "launch.json").stat().st_mode) == 0o600
        return restored

    def reporting(actual, grant, outcome):
        calls.append((actual, grant, outcome))
        assert not (attempt / start.RESTORED).exists()
        assert not (attempt / "registration.json").exists()

    monkeypatch.setattr(start.checkpoint_runtime, "prepare", restoring)
    monkeypatch.setattr(start, "report", reporting)
    start.prepare(attempt, launch, Controller((200, ANSWER)).send, None, Boot(), shipped(tmp_path))
    assert calls == [(spec, GRANT, restored)]
    assert json.loads((attempt / start.RESTORED).read_text())["recovery"] == "native"


@pytest.mark.parametrize("status", [200, 201, 403])
@pytest.mark.parametrize("suffix, base", [("/", CONTROL), ("/proxyX/", CONTROL + "/proxyX")])
def test_restore_receipt_transport_is_authenticated_bounded_and_requires_success(monkeypatch, status, suffix, base):
    from contextlib import nullcontext
    from types import SimpleNamespace

    restored = restore.Restored("native", "ckp-one", 41, "session", ("claude", "--resume", "session"))
    spec = {"execution_id": EXECUTION, "control_url": CONTROL + suffix}
    calls = []
    monkeypatch.setattr(
        start.urllib.request,
        "urlopen",
        lambda request, **kwargs: calls.append((request, kwargs)) or nullcontext(SimpleNamespace(status=status)),
    )
    if status == 200:
        assert start.report(spec, GRANT, restored) is None
    else:
        with pytest.raises(start.LaunchRefused) as caught:
            start.report(spec, GRANT, restored)
        assert str(caught.value) == "restore outcome was not accepted"
    [(request, kwargs)] = calls
    assert request.full_url == base + f"/v1/executions/{EXECUTION}/restore-outcome"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer " + GRANT
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "recovery": "native",
        "checkpoint_id": "ckp-one",
        "watermark": 41,
        "session_id": "session",
        "command": ["claude", "--resume", "session"],
    }
    assert kwargs == {"timeout": start.TIMEOUT_SECONDS}


@pytest.mark.parametrize("error", [OSError("transport refused"), start.HTTPException("broken response")])
def test_failed_restore_receipt_transport_refuses_start(monkeypatch, error):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(start.urllib.request, "urlopen", broken)
    with pytest.raises(start.LaunchRefused) as caught:
        start.report({"execution_id": EXECUTION, "control_url": CONTROL}, GRANT, restore.Restored("fresh", "", 0))
    assert str(caught.value) == "restore outcome was not accepted"


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize(
    "recovery, checkpoint_id", [("native", "ckp-one"), ("reconstructed", "ckp-two"), ("fresh", None)]
)
def test_only_confirmed_restore_outcomes_have_a_public_summary(monkeypatch, capsys, status, recovery, checkpoint_id):
    from contextlib import nullcontext
    from types import SimpleNamespace

    restored = restore.Restored(
        recovery, checkpoint_id, 41, "private-session", ("claude", "--resume", "private-session")
    )
    monkeypatch.setattr(
        start.urllib.request, "urlopen", lambda *args, **kwargs: nullcontext(SimpleNamespace(status=status))
    )
    spec = {"execution_id": EXECUTION, "control_url": CONTROL}
    if status != 200:
        with pytest.raises(start.LaunchRefused):
            start.report(spec, GRANT, restored)
        assert capsys.readouterr().out == ""
        return
    start.report(spec, GRANT, restored)
    assert json.loads(capsys.readouterr().out) == {
        "event": "restore_verified",
        "execution_id": EXECUTION,
        "recovery": recovery,
        "checkpoint_id": checkpoint_id,
        "watermark": 41,
    }


def test_confirmed_restore_summary_is_delivered_before_report_returns(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from tests.public_output import BufferedOutput

    output = BufferedOutput()
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr(
        start.urllib.request, "urlopen", lambda *args, **kwargs: nullcontext(SimpleNamespace(status=200))
    )
    restored = restore.Restored("native", "ckp-one", 41, "private-session", ("claude", "--resume", "private-session"))
    start.report({"execution_id": EXECUTION, "control_url": CONTROL}, GRANT, restored)
    assert json.loads(output.visible) == {
        "event": "restore_verified",
        "execution_id": EXECUTION,
        "recovery": "native",
        "checkpoint_id": "ckp-one",
        "watermark": 41,
    }


def test_a_checkpoint_restore_without_a_store_refuses_the_start_and_registers_nothing(tmp_path):
    launch = material(tmp_path, {**RECORD, "restore": NATIVE_RESTORE})
    with pytest.raises(restore.RestoreRefused) as refused:
        prepare(tmp_path, launch, Controller((200, ANSWER)))
    assert str(refused.value) == restore.STORE_MISSING
    attempt = tmp_path / "attempts" / EXECUTION
    assert not (attempt / "registration.json").exists()
    assert not (attempt / start.RESTORED).exists()
    assert (attempt / "run" / GRANT_NAME).exists()


def test_a_refused_restore_is_a_refused_start(tmp_path, monkeypatch, capsys):
    def refused(attempt, launch):
        raise restore.RestoreRefused("version_changed")

    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", refused)
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: pytest.fail("supervisor ran"))
    assert start.main(["a", "b"]) == 64
    assert capsys.readouterr().err == "ERROR worker start refused: version_changed\n"


def test_the_start_runs_the_supervisor_on_the_prepared_attempt_under_a_private_umask(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(start.os, "umask", lambda mask: calls.append(("umask", mask)) or 0o022)
    monkeypatch.setattr(start, "prepare", lambda attempt, launch: calls.append(("prepare", attempt, launch)))
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: calls.append(("supervise", argv)) or 70)

    assert start.main(["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]) == 70

    assert calls == [
        ("umask", 0o077),
        ("prepare", Path("/home/worker/attempts/a"), Path("/var/run/swarm/launch/launch.json")),
        ("supervise", ["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]),
    ]


def test_a_refused_start_never_runs_the_supervisor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: pytest.fail("supervisor ran"))

    assert start.main([str(tmp_path / "attempts" / EXECUTION), str(tmp_path / "missing.json")]) == 64

    assert capsys.readouterr().err == "ERROR worker start refused: missing or unreadable launch record\n"
    assert not (tmp_path / "attempts").exists()


def test_a_failed_bootstrap_refuses_the_start(tmp_path, monkeypatch, capsys):
    def broken(attempt, launch):
        raise worker_home.BootstrapError("profile template not found: default")

    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", broken)

    assert start.main(["a", "b"]) == 64

    assert capsys.readouterr().err == "ERROR worker start refused: profile template not found: default\n"


@pytest.mark.parametrize("argv", [[], ["a"], ["a", "b", "c"]])
def test_the_start_needs_an_attempt_and_a_launch_record(argv, capsys):
    assert start.main(argv) == 64

    assert capsys.readouterr().err == "ERROR worker start requires attempt directory and launch record\n"


def test_the_start_reads_its_operands_from_the_command_line(monkeypatch):
    calls = []
    monkeypatch.setattr(start.sys, "argv", ["start", "/home/worker/attempts/a", "/launch.json"])
    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", lambda attempt, launch: calls.append((attempt, launch)))
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: 70)

    assert start.main() == 70

    assert calls == [(Path("/home/worker/attempts/a"), Path("/launch.json"))]


class Recorder(BaseHTTPRequestHandler):
    status, answer, seen = 200, b"{}", []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, self.headers["Authorization"], self.headers["Content-Type"], body))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(type(self).answer)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_post_registers_with_the_grant_as_bearer(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 200)
    monkeypatch.setattr(Recorder, "answer", json.dumps(ANSWER).encode())

    assert start.post(server, GRANT, {"execution_id": EXECUTION, "generation": 3}) == (200, ANSWER)

    assert Recorder.seen == [
        (
            "/v2/executions/register",
            f"Bearer {GRANT}",
            "application/json",
            json.dumps({"execution_id": EXECUTION, "generation": 3}).encode(),
        )
    ]


def test_post_registers_on_the_same_path_for_a_slash_terminated_control_url(monkeypatch):
    seen = []

    def unreachable(request, timeout):
        seen.append(request.full_url)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(start.urllib.request, "urlopen", unreachable)

    assert start.post("http://controller.swarm.invalid/apiX/", GRANT, {}) == (503, {})
    assert seen == ["http://controller.swarm.invalid/apiX/v2/executions/register"]


def test_post_reports_a_refusal_status_without_its_body(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 401)
    monkeypatch.setattr(Recorder, "answer", b'{"error_class": "unauthenticated"}')

    assert start.post(server, GRANT, {}) == (401, {})


def test_post_reports_an_unreadable_answer_as_empty(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 200)
    monkeypatch.setattr(Recorder, "answer", b"not json")

    assert start.post(server, GRANT, {}) == (200, {})


def test_post_reports_an_unreachable_controller_as_busy(server):
    closed = server.rsplit(":", 1)[0] + ":1"

    assert start.post(closed, GRANT, {}) == (503, {})


def test_an_https_control_url_reaches_the_controller_and_the_worker_settings(tmp_path):
    secure = "https://controller.swarm.invalid"
    controller = Controller((200, ANSWER))

    boot = prepare(tmp_path, material(tmp_path, {**RECORD, "control_url": secure}), controller)

    assert controller.calls[0][0] == secure
    assert boot.requests[0].endpoints == {API_URL: secure}


def test_a_stale_staged_record_never_loosens_the_registration_record(tmp_path):
    staged = tmp_path / "attempts" / EXECUTION / "registration.json.tmp"
    staged.parent.mkdir(parents=True)
    staged.write_text("stale")
    staged.chmod(0o644)

    prepare(tmp_path, material(tmp_path), Controller((200, ANSWER)))

    record = staged.with_name("registration.json")
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert json.loads(record.read_text()) == AUTHORITY
    assert not staged.exists()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError("disk full"), "disk full"),
        (KeyError("homes"), "'homes'"),
        (TypeError("bad record"), "bad record"),
        (subprocess.TimeoutExpired("python", 30), "Command 'python' timed out after 30 seconds"),
    ],
)
def test_any_start_failure_refuses_without_the_supervisor(monkeypatch, capsys, error, message):
    def broken(attempt, launch):
        raise error

    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", broken)
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: pytest.fail("supervisor ran"))

    assert start.main(["a", "b"]) == 64

    assert tuple(capsys.readouterr()) == ("", f"ERROR worker start refused: {message}\n")


def test_post_waits_at_most_its_timeout_for_the_controller(monkeypatch):
    seen = []

    def unreachable(request, timeout):
        seen.append(timeout)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(start.urllib.request, "urlopen", unreachable)

    assert start.post(CONTROL, GRANT, {}) == (503, {})
    assert seen == [10]


def test_post_reports_a_broken_http_answer_as_busy(monkeypatch):
    def broken(request, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(start.urllib.request, "urlopen", broken)

    assert start.post(CONTROL, GRANT, {}) == (503, {})


class Bootstrap(Boot):
    def __call__(self, request: worker_home.Request) -> dict:
        super().__call__(request)
        attempt = request.root / request.attempt
        (attempt / "homes" / "claude").mkdir(parents=True)
        layout = filesystem.mapping(filesystem.load())
        record = {"attempt": request.attempt, "homes": {"claude": "homes/claude"}, "layout": layout}
        (attempt / "execution.json").write_text(json.dumps(record))
        return record


@pytest.fixture
def umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_the_started_supervisor_loads_a_prepared_attempt_only_the_worker_can_read(tmp_path, monkeypatch, capsys, umask):
    launch = material(tmp_path)
    attempt = tmp_path / "attempts" / EXECUTION
    controller = Controller((200, ANSWER))
    loaded = []
    prepared = functools.partial(
        start.prepare, send=controller.send, sleep=controller.sleep, boot=Bootstrap(), templates=shipped(tmp_path)
    )
    monkeypatch.setattr(start, "prepare", prepared)
    monkeypatch.setattr(
        start.supervision_runtime, "main", lambda argv: loaded.append(Launch.load(*map(Path, argv))) or 70
    )

    assert start.main([str(attempt), str(launch)]) == 70

    [run] = loaded
    assert (run.authority, run.harness, run.home) == (AUTHORITY, "claude", attempt.resolve() / "homes" / "claude")
    for path in (attempt / "execution.json", attempt / "registration.json", attempt / "run" / GRANT_NAME):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(attempt.parent.stat().st_mode) == 0o700
    printed = {"worker_start": "prepared", "attempt": EXECUTION, "uid": os.getuid()}
    assert capsys.readouterr().out == json.dumps(printed) + "\n"


def test_the_start_prints_the_rendered_chain_when_bootstrap_names_one(monkeypatch, capsys):
    chains = {"claude": ["anton-base", "package:engineer", "engineer", "frontend", "brain"]}
    monkeypatch.setattr(start, "prepare", lambda attempt, launch: {"chains": chains})
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: 70)

    assert start.main(["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]) == 70

    printed = {"worker_start": "prepared", "attempt": "a", "uid": os.getuid(), "chains": chains}
    assert capsys.readouterr().out == json.dumps(printed) + "\n"


def test_the_image_supervisor_entry_is_the_worker_start_step():
    entry = Path(__file__).resolve().parents[1] / "docker" / "swarm-node" / "supervisor.py"
    spec = importlib.util.spec_from_file_location("swarm_node_supervisor", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main is start.main


def test_the_image_supervisor_entry_runs_the_start_step_as_a_script():
    root = Path(__file__).resolve().parents[1]
    entry = root / "docker" / "swarm-node" / "supervisor.py"
    environ = {**os.environ, "PYTHONPATH": str(root)}

    done = subprocess.run([sys.executable, str(entry)], env=environ, capture_output=True, text=True, timeout=60)

    assert (done.returncode, done.stdout) == (64, "")
    assert done.stderr == "ERROR worker start requires attempt directory and launch record\n"


PROJECT = "github.com/the-cloud-clockwork/agentihooks"
AGENT = "engineer@323133-1222"
CHECKOUT = {"project": PROJECT, "agent": AGENT}
BRANCH = "engineer-323133-1222"
BASE_COMMIT = "a" * 40


class Workspaces:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure, self.calls = failure, []

    def __call__(self, attempt: Path, request: workspaces.Request) -> workspaces.Workspace:
        self.calls.append((attempt, request, (attempt / "registration.json").exists()))
        if self.failure is not None:
            raise self.failure
        mirror = attempt / "checkouts" / "agentihooks.git"
        tree = attempt / "worktrees" / BRANCH
        return workspaces.Workspace(tree, BRANCH, mirror, PROJECT, "dev", BASE_COMMIT, request.task, 3, 1.5)


def checked_out(tmp_path, monkeypatch, prepared, record=None):
    controller = Controller((200, ANSWER))
    launch = material(tmp_path, record or {**RECORD, "checkout": CHECKOUT})
    attempt = tmp_path / "attempts" / EXECUTION
    monkeypatch.setattr(start, "checkout", prepared)
    return start.prepare(attempt, launch, controller.send, controller.sleep, Boot(), shipped(tmp_path))


def test_the_start_prepares_the_permitted_project_checkout_on_its_branch_before_the_registration_record(
    tmp_path, monkeypatch
):
    prepared = Workspaces()

    record = checked_out(tmp_path, monkeypatch, prepared)

    origin = "https://github.com/the-cloud-clockwork/agentihooks.git"
    attempt = tmp_path / "attempts" / EXECUTION
    assert prepared.calls == [(attempt, workspaces.Request(origin, "t1", 3, AGENT), False)]
    assert record == {"checkout": {"project": PROJECT, "branch": BRANCH, "base": "dev", "base_commit": BASE_COMMIT}}
    assert json.loads((attempt / "registration.json").read_text()) == AUTHORITY


def test_a_launch_record_without_a_checkout_prepares_none(tmp_path, monkeypatch):
    prepared = Workspaces()

    assert checked_out(tmp_path, monkeypatch, prepared, RECORD) == {}

    assert prepared.calls == []


def test_a_checkout_that_cannot_be_prepared_refuses_the_start_without_a_registration_record(tmp_path, monkeypatch):
    prepared = Workspaces(workspaces.WorkspaceError("fetch of the project failed"))

    with pytest.raises(workspaces.WorkspaceError) as refused:
        checked_out(tmp_path, monkeypatch, prepared)

    assert str(refused.value) == "fetch of the project failed"

    assert not (tmp_path / "attempts" / EXECUTION / "registration.json").exists()


@pytest.mark.parametrize(
    "checkout",
    [
        {"project": "user:token@github.com/the-cloud-clockwork/agentihooks", "agent": AGENT},
        {"project": "x-access-token@github.com/the-cloud-clockwork/agentihooks", "agent": AGENT},
        {"project": "local:fixture", "agent": AGENT},
        {"project": "github.com/the-cloud-clockwork/agentihooks/extra", "agent": AGENT},
        {"project": "github.com/the-cloud-clockwork/..", "agent": AGENT},
        {"project": "github.com/.hidden/agentihooks", "agent": AGENT},
        {"project": "gitlab.com/the-cloud-clockwork/agentihooks", "agent": AGENT},
        {"project": 7, "agent": AGENT},
        {"agent": AGENT},
        PROJECT,
    ],
)
def test_a_checkout_outside_a_plain_project_is_refused_before_anything_is_written(tmp_path, checkout):
    launch = material(tmp_path, {**RECORD, "checkout": checkout})

    assert refusal(tmp_path, launch) == "launch record names no permitted project"


@pytest.mark.parametrize("checkout", [{"project": PROJECT}, {"project": PROJECT, "agent": 7}])
def test_a_checkout_that_names_no_agent_is_refused_before_anything_is_written(tmp_path, checkout):
    launch = material(tmp_path, {**RECORD, "checkout": checkout})

    assert refusal(tmp_path, launch) == "launch record names no agent for its checkout"


def test_the_start_prints_the_prepared_checkout(monkeypatch, capsys):
    checkout = {"project": PROJECT, "branch": BRANCH, "base": "dev", "base_commit": BASE_COMMIT}
    monkeypatch.setattr(start, "prepare", lambda attempt, launch: {"checkout": checkout})
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: 70)

    assert start.main(["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]) == 70

    printed = {"worker_start": "prepared", "attempt": "a", "uid": os.getuid(), "checkout": checkout}
    assert capsys.readouterr().out == json.dumps(printed) + "\n"


def git(*args: str, cwd: Path | None = None) -> str:
    identity = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
    done = subprocess.run(["git", *identity, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_the_checkout_clones_its_origin_into_the_attempt_and_names_the_branch_from_the_agent(tmp_path):
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    git("init", "--bare", "--initial-branch", "dev", str(origin))
    git("init", "--initial-branch", "dev", str(seed))
    (seed / "README").write_text("fixture\n")
    git("add", "README", cwd=seed)
    git("commit", "-m", "seed", cwd=seed)
    git("push", str(origin), "dev", cwd=seed)
    layout = filesystem.load()
    attempt = filesystem.allocate(tmp_path, EXECUTION, layout).root
    (attempt / "execution.json").write_text(json.dumps({"attempt": EXECUTION, "layout": filesystem.mapping(layout)}))

    found = start.checkout(attempt, workspaces.Request(f"file://{origin}", "t1", 3, AGENT))

    assert (found.branch, found.base, found.base_commit) == (BRANCH, "dev", git("rev-parse", "HEAD", cwd=seed))
    assert found.path == attempt / "worktrees" / BRANCH
    assert (found.path / "README").read_text() == "fixture\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=found.path) == BRANCH
    assert git("--git-dir", str(found.mirror), "config", "remote.origin.url") == f"file://{origin}"
