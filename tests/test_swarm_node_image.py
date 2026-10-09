import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_v2 import worker_image


def artifact(name, version):
    content = f"#!/bin/sh\nprintf '{name} {version}\\n'\n".encode()
    return {
        "version": version,
        "url": f"https://example.test/{version}/{name}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "version_output": f"{name} {version}",
    }, content


@pytest.fixture
def locked(tmp_path):
    tools = {}
    payloads = {}
    for name in ("herdr", "claude", "codex"):
        tools[name], payloads[tools[name]["url"]] = artifact(name, "1.2.3")
    requirements = tmp_path / "requirements.lock"
    requirements.write_text("example==1.2.3 --hash=sha256:" + "a" * 64 + "\n")
    lock = {
        "schema_version": 1,
        "architectures": ["amd64"],
        "base_image": "python:3.12.12-slim-bookworm@sha256:" + "b" * 64,
        "python": "3.12.12",
        "debian_snapshot": "20260301T000000Z",
        "shell_packages": {"git": "1:2.39.5-0+deb12u3"},
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "tools": tools,
    }
    path = tmp_path / "versions.lock"
    path.write_text(json.dumps(lock))
    return path, lock, payloads


def test_lock_accepts_pinned_fixture(locked):
    path, lock, _ = locked
    assert worker_image.load_lock(path, "amd64") == lock


@pytest.mark.parametrize("architecture", ["arm64", "", "ppc64le"])
def test_lock_rejects_unsupported_architecture(locked, architecture):
    path, _, _ = locked
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, architecture)
    assert str(error.value) == "unsupported architecture"


@pytest.mark.parametrize("checksum", [None, "", "a" * 63, "g" * 64])
def test_lock_rejects_missing_or_invalid_checksum(locked, checksum):
    path, lock, _ = locked
    lock["tools"]["claude"]["sha256"] = checksum
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: claude"


def test_lock_rejects_unpinned_base_and_modified_python_environment(locked):
    path, lock, _ = locked
    lock["base_image"] = "python:latest"
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "base image requires a sha256 digest"
    lock["base_image"] = "python:3.12.12@sha256:" + "b" * 64
    path.write_text(json.dumps(lock))
    path.with_name("requirements.lock").write_text("changed")
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "Python requirements checksum mismatch"


def test_install_verifies_every_download_before_mutating_binaries(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    payloads[lock["tools"]["codex"]["url"]] = b"wrong binary"
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    destination = tmp_path / "bin"
    destination.mkdir()
    (destination / "existing").write_text("protected")
    with pytest.raises(ValueError) as error:
        worker_image.install_tools(lock, destination)
    assert str(error.value) == "artifact checksum mismatch: codex"
    assert [(p.name, p.read_text()) for p in destination.iterdir()] == [("existing", "protected")]


def test_install_native_binaries_and_reject_wrong_version(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    destination = tmp_path / "bin"
    destination.mkdir()
    worker_image.install_tools(lock, destination)
    assert worker_image.tool_versions(lock, destination) == {name: f"{name} 1.2.3" for name in lock["tools"]}
    (destination / "herdr").write_text("#!/bin/sh\nprintf 'herdr 9.9.9\\n'\n")
    with pytest.raises(ValueError) as error:
        worker_image.tool_versions(lock, destination)
    assert str(error.value) == "binary version mismatch: herdr"


def test_codex_archive_installs_only_named_binary(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    url = lock["tools"]["codex"]["url"]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (("codex-linux", payloads[url]), ("../escape", b"bad")):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    payloads[url] = buffer.getvalue()
    lock["tools"]["codex"].update(member="codex-linux", sha256=hashlib.sha256(payloads[url]).hexdigest())
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    destination = tmp_path / "bin"
    destination.mkdir()
    worker_image.install_tools(lock, destination)
    assert not (tmp_path / "escape").exists()
    assert worker_image.tool_versions(lock, destination)["codex"] == "codex 1.2.3"


def test_declared_inventory_is_stable_with_changed_upstream_latest(locked, monkeypatch):
    path, lock, _ = locked
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network used"))
    first = worker_image.declared_inventory(path, "amd64")
    second = worker_image.declared_inventory(path, "amd64")
    assert (
        first
        == second
        == {
            "architecture": "amd64",
            "base_image": lock["base_image"],
            "python": "3.12.12",
            "debian_snapshot": "20260301T000000Z",
            "shell_packages": lock["shell_packages"],
            "tools": {name: "1.2.3" for name in lock["tools"]},
            "requirements_sha256": lock["requirements_sha256"],
            "lock_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )


def test_committed_image_inputs_pin_base_and_keep_profiles_outside_home():
    root = Path(__file__).resolve().parents[1]
    inputs = root / "docker/swarm-node"
    lock = worker_image.load_lock(inputs / "versions.lock", "amd64")
    dockerfile = (inputs / "Dockerfile").read_text()
    assert f"FROM {lock['base_image']}" in dockerfile
    assert "COPY profiles/ /opt/agentihooks/templates/" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "DISABLE_AUTOUPDATER=1" in dockerfile
    assert "--require-hashes" in dockerfile


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "latest"),
        ("url", "http://example.test/1.2.3/x"),
        ("url", "https://example.test/latest/x"),
        ("version_output", "x latest"),
    ],
)
def test_invalid_artifact_metadata_is_rejected(locked, field, value):
    path, lock, _ = locked
    lock["tools"]["herdr"][field] = value
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: herdr"


def test_lock_rejects_schema_and_missing_tool(locked):
    path, lock, _ = locked
    lock["schema_version"] = 2
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "unsupported lock schema"
    lock["schema_version"] = 1
    del lock["tools"]["herdr"]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: herdr"


def test_observed_inventory_checks_python_shell_and_tool_versions(locked, monkeypatch):
    _, lock, _ = locked
    monkeypatch.setattr(worker_image.platform, "python_version", lambda: "3.12.12")
    query = Mock(return_value=SimpleNamespace(stdout="git=1:2.39.5-0+deb12u3\nbash=5.2\n"))
    monkeypatch.setattr(worker_image.subprocess, "run", query)
    monkeypatch.setattr(worker_image, "tool_versions", lambda data, binaries: {"herdr": "herdr 1.2.3"})
    monkeypatch.setattr(
        worker_image.importlib.metadata,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": "zeta"}, version="2.0.0"),
            SimpleNamespace(metadata={"Name": "alpha"}, version="1.0.0"),
        ],
    )
    assert worker_image.observed_inventory(lock, Path("/bin")) == {
        "python": "3.12.12",
        "tools": {"herdr": "herdr 1.2.3"},
        "debian_packages": ["bash=5.2", "git=1:2.39.5-0+deb12u3"],
        "python_packages": ["alpha==1.0.0", "zeta==2.0.0"],
    }
    query.assert_called_once_with(
        ["dpkg-query", "-W", "-f=${Package}=${Version}\n"], check=True, capture_output=True, text=True
    )
    monkeypatch.setattr(worker_image.platform, "python_version", lambda: "3.13.0")
    with pytest.raises(ValueError) as error:
        worker_image.observed_inventory(lock, Path("/bin"))
    assert str(error.value) == "Python version mismatch"
    monkeypatch.setattr(worker_image.platform, "python_version", lambda: "3.12.12")
    lock["shell_packages"]["git"] = "bad"
    with pytest.raises(ValueError) as error:
        worker_image.observed_inventory(lock, Path("/bin"))
    assert str(error.value) == "shell package version mismatch"


def test_manifest_contains_observed_inventory_and_immutable_templates(locked, tmp_path, monkeypatch):
    path, _, _ = locked
    observed = {"python": "3.12.12", "tools": {"herdr": "herdr 1.2.3"}}
    monkeypatch.setattr(worker_image, "observed_inventory", lambda lock, binaries: observed)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "profile.md").write_text("fixture profile")
    (templates / "empty").mkdir()
    worker_image.write_manifest(path, "amd64", "tested-commit", templates)
    manifest = json.loads(path.with_name("manifest.json").read_text())
    assert manifest == {
        "schema_version": 1,
        "package": "SV2-IMG-01",
        "source_revision": "tested-commit",
        "declared": worker_image.declared_inventory(path, "amd64"),
        "observed": observed,
        "profile_templates": {"profile.md": hashlib.sha256(b"fixture profile").hexdigest()},
    }
    assert worker_image.report(path) == {
        "package": "SV2-IMG-01",
        "worker_image_build_validation_failures": 0,
        "manifest": manifest,
    }
    manifest["declared"]["python"] = "bad"
    path.with_name("manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError) as error:
        worker_image.report(path)
    assert str(error.value) == "installed inventory differs from build manifest"
    manifest["declared"]["python"] = "3.12.12"
    manifest["observed"] = {}
    path.with_name("manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError) as error:
        worker_image.report(path)
    assert str(error.value) == "installed inventory differs from build manifest"


@pytest.mark.parametrize("action", ["validate", "install", "manifest", "report", "shell-packages"])
def test_build_command_routes_locked_actions(locked, monkeypatch, capsys, action):
    path, lock, _ = locked
    monkeypatch.setattr(
        "sys.argv",
        ["worker_image", action, "--lock", str(path), "--architecture", "amd64", "--source-revision", "tested"],
    )
    install = Mock()
    manifest = Mock()
    report = Mock(return_value={"ok": True})
    monkeypatch.setattr(worker_image, "install_tools", install)
    monkeypatch.setattr(worker_image, "write_manifest", manifest)
    monkeypatch.setattr(worker_image, "report", report)
    worker_image.main()
    if action == "install":
        install.assert_called_once_with(lock, Path("/usr/local/bin"))
    elif action == "manifest":
        manifest.assert_called_once_with(path, "amd64", "tested", Path("/opt/agentihooks/templates"))
    elif action == "report":
        report.assert_called_once_with(path)
        assert capsys.readouterr().out == '{"ok": true}\n'
    elif action == "shell-packages":
        assert capsys.readouterr().out == "git=1:2.39.5-0+deb12u3\n"
    else:
        install.assert_not_called()
        manifest.assert_not_called()
        report.assert_not_called()
