import hashlib
import json
from unittest.mock import Mock

import pytest

from scripts.swarm_v2 import supervision as config
from scripts.swarm_v2 import supervision_agent as agent
from scripts.swarm_v2 import supervision_processes as trees
from scripts.swarm_v2 import supervision_protocol as protocol


@pytest.mark.parametrize("value", [None, [], "tool", [""], ["tool\0"], [1], [True]])
def test_invalid_commands_have_stable_refusal(value):
    with pytest.raises(config.LaunchRefused, match="^invalid command$"):
        config._command(value)


@pytest.mark.parametrize("value", [None, "1", True, 0, -1, 301, float("inf"), float("nan")])
def test_deadlines_require_bounded_numeric_values(value):
    with pytest.raises(config.LaunchRefused, match="^invalid deadline$"):
        config._deadline(value)


@pytest.mark.parametrize("value", [0.1, 300])
def test_deadline_boundary_is_accepted(value):
    assert config._deadline(value) == float(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "invalid"},
        {"grant_id": "invalid"},
        {"generation": True},
        {"generation": 0},
        {"task_id": "!"},
        {"seat_id": 1},
        {"swarm_id": ""},
    ],
)
def test_authority_validation_refuses_invalid_scope(changes):
    authority = {
        "execution_id": "exe-" + "1" * 32,
        "grant_id": "lgr-" + "2" * 32,
        "generation": 1,
        "task_id": "task",
        "seat_id": "seat",
        "swarm_id": "swarm",
    }
    with pytest.raises(config.LaunchRefused, match="^invalid authority$"):
        config._authority({**authority, **changes})


@pytest.mark.parametrize("value,message", [(None, "invalid authority"), ({}, "missing authority")])
def test_authority_requires_complete_object(value, message):
    with pytest.raises(config.LaunchRefused, match=f"^{message}$"):
        config._authority(value)


@pytest.mark.parametrize("state", ["trusted", "untrusted"])
def test_claude_trust_uses_admitted_directory(tmp_path, monkeypatch, state):
    from scripts import claude_trust

    trust = Mock(return_value=(state, None))
    monkeypatch.setattr(claude_trust, "ensure_trusted", trust)
    environment = {"HOME": str(tmp_path)}
    if state == "untrusted":
        with pytest.raises(ValueError, match="^private execution directory is untrusted$"):
            agent.native_command(("claude", "--resume"), tmp_path, environment)
    else:
        assert agent.native_command(("claude", "--resume"), tmp_path, environment) == ("claude", "--resume")
    trust.assert_called_once_with(tmp_path, environment)


@pytest.mark.parametrize("result", [0, -1])
def test_subreaper_requests_kernel_child_ownership(monkeypatch, result):
    prctl = Mock(return_value=result)
    library = Mock(return_value=Mock(prctl=prctl))
    monkeypatch.setattr(trees.ctypes, "CDLL", library)
    if result:
        with pytest.raises(OSError, match="^child reaping unavailable$"):
            trees.subreaper()
    else:
        trees.subreaper()
    library.assert_called_once_with(None, use_errno=True)
    prctl.assert_called_once_with(36, 1, 0, 0, 0)


@pytest.fixture
def material(tmp_path):
    scope = {"authority": {"execution_id": "current"}, "incarnation": "launch"}
    path = tmp_path / "checkpoints" / "manifest.json"
    path.parent.mkdir()
    document = {**scope, "status": "complete", "checkpoint_id": "checkpoint"}
    return tmp_path, scope, path, document


def receipt(attempt, scope, path):
    return {
        **scope,
        "status": "complete",
        "manifest": str(path.relative_to(attempt)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"incarnation": "old"},
        {"status": "incomplete"},
        {"checkpoint_id": 1},
        {"checkpoint_id": ""},
        {"checkpoint_id": None},
    ],
)
def test_checkpoint_rejects_invalid_acknowledged_material(material, changes):
    attempt, scope, path, document = material
    protocol.write(path, {**document, **changes})
    assert protocol.checkpoint(attempt, receipt(attempt, scope, path), scope) is None


def test_checkpoint_rejects_acknowledged_file_outside_archive(material):
    attempt, scope, _, document = material
    path = attempt / "outside.json"
    protocol.write(path, document)
    assert protocol.checkpoint(attempt, receipt(attempt, scope, path), scope) is None


def test_checkpoint_rejects_symlink_outside_attempt(material, tmp_path):
    attempt, scope, path, document = material
    external = tmp_path.parent / (tmp_path.name + "-outside.json")
    external.write_text(json.dumps(document))
    try:
        path.symlink_to(external)
        assert protocol.checkpoint(attempt, receipt(attempt, scope, path), scope) is None
    finally:
        external.unlink()


def test_protocol_writes_sorted_atomic_material(tmp_path, monkeypatch):
    directory = tmp_path / "material"
    directory.mkdir()
    path = directory / "receipt.json"
    opened = Mock(wraps=protocol.os.open)
    monkeypatch.setattr(protocol.os, "open", opened)
    protocol.write(path, {"z": 1, "a": 2})
    opened.assert_called_once_with(directory, protocol.os.O_RDONLY | protocol.os.O_DIRECTORY)
    assert path.read_bytes() == b'{"a": 2, "z": 1}'
    assert list(directory.iterdir()) == [path]


def test_authority_accepts_case_preserving_scope_names():
    authority = {
        "execution_id": "exe-" + "1" * 32,
        "grant_id": "lgr-" + "2" * 32,
        "generation": 1,
        "task_id": "Task",
        "seat_id": "Seat",
        "swarm_id": "Swarm",
    }
    assert config._authority(authority) == authority


def test_cleanup_stops_observing_at_the_deadline(monkeypatch):
    child = {2: object()}

    def living(root):
        assert root == 1
        return child

    monkeypatch.setattr(trees, "living", living)
    monkeypatch.setattr(trees, "send", lambda *_: None)
    monkeypatch.setattr(trees.time, "monotonic", Mock(side_effect=[0, 1]))
    observe = Mock()
    monkeypatch.setattr(trees.time, "sleep", Mock(side_effect=AssertionError("sleep after deadline")))
    assert not trees.cleanup(1, 1, observe)
    observe.assert_called_once_with()
