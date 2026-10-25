import hashlib
import io
import json
import tarfile
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from scripts.swarm_v2 import worker_image


def artifact(name, version):
    content = f'#!/bin/sh\n[ "$1" = "--version" ] || exit 2\nprintf "{name} {version}\\n"\n'.encode()
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


@pytest.mark.parametrize("registry", ["", "public.ecr.aws/docker/library/"])
def test_lock_accepts_pinned_fixture(locked, registry):
    path, lock, _ = locked
    lock["base_image"] = registry + lock["base_image"]
    path.write_text(json.dumps(lock))
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
    download = Mock(side_effect=lambda url, timeout: io.BytesIO(payloads[url]))
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", download)
    destination = tmp_path / "bin"
    destination.mkdir()
    worker_image.install_tools(lock, destination)
    assert download.call_args_list == [call(tool["url"], timeout=120) for tool in lock["tools"].values()]
    assert all(binary.stat().st_mode & 0o777 == 0o755 for binary in destination.iterdir())
    assert worker_image.tool_versions(lock, destination) == {name: f"{name} 1.2.3" for name in lock["tools"]}
    (destination / "herdr").write_text("#!/bin/sh\nprintf 'herdr 9.9.9\\n'\n")
    with pytest.raises(ValueError) as error:
        worker_image.tool_versions(lock, destination)
    assert str(error.value) == "binary version mismatch: herdr"


def test_install_reuses_only_verified_cached_downloads(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    download = Mock(side_effect=lambda url, timeout: io.BytesIO(payloads[url]))
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", download)
    cache = tmp_path / "cache"
    cache.mkdir()
    first = tmp_path / "first"
    first.mkdir()
    worker_image.install_tools(lock, first, cache)
    assert download.call_count == 3
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == {
        tool["sha256"]: payloads[tool["url"]] for tool in lock["tools"].values()
    }
    herdr = lock["tools"]["herdr"]
    (cache / herdr["sha256"]).write_bytes(b"tampered")
    download.reset_mock()
    second = tmp_path / "second"
    second.mkdir()
    worker_image.install_tools(lock, second, cache)
    assert download.call_args_list == [call(herdr["url"], timeout=120)]
    assert (cache / herdr["sha256"]).read_bytes() == payloads[herdr["url"]]
    assert {p.name: p.read_bytes() for p in second.iterdir()} == {p.name: p.read_bytes() for p in first.iterdir()}


def test_download_fetches_every_locked_tool_concurrently(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    barrier = threading.Barrier(len(lock["tools"]), timeout=5)

    def download(url, timeout):
        barrier.wait()
        return io.BytesIO(payloads[url])

    monkeypatch.setattr(worker_image.urllib.request, "urlopen", download)
    cache = tmp_path / "cache"
    cache.mkdir()
    worker_image.download_tools(lock, cache)
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == {
        tool["sha256"]: payloads[tool["url"]] for tool in lock["tools"].values()
    }


def test_download_fails_when_any_locked_tool_fails(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    payloads[lock["tools"]["codex"]["url"]] = b"wrong binary"
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    cache = tmp_path / "cache"
    cache.mkdir()
    with pytest.raises(ValueError) as error:
        worker_image.download_tools(lock, cache)
    assert str(error.value) == "artifact checksum mismatch: codex"
    assert lock["tools"]["codex"]["sha256"] not in {p.name for p in cache.iterdir()}


def test_install_never_caches_a_mismatched_download(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    codex = lock["tools"]["codex"]
    payloads[codex["url"]] = b"wrong binary"
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / codex["sha256"]).write_bytes(b"also wrong")
    destination = tmp_path / "bin"
    destination.mkdir()
    with pytest.raises(ValueError) as error:
        worker_image.install_tools(lock, destination, cache)
    assert str(error.value) == "artifact checksum mismatch: codex"
    assert (cache / codex["sha256"]).read_bytes() == b"also wrong"
    assert list(destination.iterdir()) == []


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
    download = Mock(side_effect=lambda url, timeout: io.BytesIO(payloads[url]))
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", download)
    destination = tmp_path / "bin"
    destination.mkdir()
    worker_image.install_tools(lock, destination)
    assert download.call_args_list == [call(tool["url"], timeout=120) for tool in lock["tools"].values()]
    assert all(binary.stat().st_mode & 0o777 == 0o755 for binary in destination.iterdir())
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
    assert f"ARG BASE_IMAGE={lock['base_image']}" in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert '--base-image "$BASE_IMAGE"' in dockerfile
    assert "COPY profiles/ /opt/agentihooks/templates/" in dockerfile
    assert "COPY profiles/package/roles/ /opt/agentihooks/templates/" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "DISABLE_AUTOUPDATER=1" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--require-hashes -d /downloads/pip -r /opt/swarm-node/requirements.lock" in dockerfile
    python_stage = dockerfile.split("FROM ${BASE_IMAGE} AS python-downloads\n", 1)[1].split("\n\n", 1)[0]
    assert python_stage.splitlines()[0] == "COPY docker/swarm-node/requirements.lock /opt/swarm-node/"
    assert "versions.lock" not in python_stage
    assert (
        'worker_image.py download --architecture "$TARGETARCH" --cache /downloads/tools\n\nFROM ${BASE_IMAGE} AS worker\n'
        in dockerfile.split("FROM ${BASE_IMAGE} AS tool-downloads\n", 1)[1]
    )
    assert 'worker_image.py install --architecture "$TARGETARCH" --cache /downloads/tools \\\n' in dockerfile
    smoke = (inputs / "smoke.sh").read_text()
    assert "docker build --no-cache-filter worker --platform" in smoke
    assert "--mount=type=bind,from=python-downloads,source=/downloads/pip,target=/downloads/pip" in dockerfile
    assert "--mount=type=bind,from=tool-downloads,source=/downloads/tools,target=/downloads/tools" in dockerfile
    assert "--no-index --find-links /downloads/pip --require-hashes" in dockerfile
    assert "--cache /downloads/tools \\\n    && chmod -R a-w /opt/venv\nCOPY pyproject" in dockerfile
    assert "&& find /opt/venv \\! -type l -perm /222 -exec chmod a-w {} + \\\n" in dockerfile
    assert dockerfile.count("chmod -R a-w") == 2 and "a-w /opt/agentihooks/templates /opt/swarm-node \\\n" in dockerfile
    assert dockerfile.endswith('CMD ["python", "/opt/swarm-node/worker_image.py", "report"]\n')
    assert dockerfile.count("FROM ") == 3


def companion(lock, payloads, name="codex-code-mode-host", version="1.2.3"):
    content = f'#!/bin/sh\n[ "$1" = "--help" ] || exit 2\nprintf "Usage: {name} [OPTIONS]\\n"\n'.encode()
    lock["tools"][name] = {
        "version": version,
        "url": f"https://example.test/{version}/{name}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "companion": "codex",
    }
    payloads[lock["tools"][name]["url"]] = content
    return lock["tools"][name]


def test_committed_lock_installs_the_code_mode_host_beside_codex():
    root = Path(__file__).resolve().parents[1]
    tools = worker_image.load_lock(root / "docker/swarm-node/versions.lock", "amd64")["tools"]
    host = tools["codex-code-mode-host"]
    assert host["companion"] == "codex" and host["version"] == tools["codex"]["version"]
    assert host["url"] == tools["codex"]["url"].replace("/codex-", "/codex-code-mode-host-")
    assert host["member"] == tools["codex"]["member"].replace("codex-", "codex-code-mode-host-")
    assert host["sha256"] != tools["codex"]["sha256"]


def test_companion_installs_beside_codex_and_checks_its_identity(locked, tmp_path, monkeypatch):
    path, lock, payloads = locked
    companion(lock, payloads)
    path.write_text(json.dumps(lock))
    assert worker_image.load_lock(path, "amd64") == lock
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    destination = tmp_path / "bin"
    destination.mkdir()
    worker_image.install_tools(lock, destination)
    assert (destination / "codex-code-mode-host").stat().st_mode & 0o777 == 0o755
    host = destination / "codex-code-mode-host"
    assert worker_image.tool_versions(lock, destination)["codex-code-mode-host"] == (
        f"codex-code-mode-host 1.2.3 sha256:{lock['tools']['codex-code-mode-host']['sha256']}"
    )
    host.write_text(host.read_text() + "\n")
    assert worker_image.tool_versions(lock, destination)["codex-code-mode-host"].endswith(
        f"sha256:{worker_image.checksum(host)}"
    )
    host.write_text("#!/bin/sh\nprintf 'Usage: other [OPTIONS]\\n'\n")
    with pytest.raises(ValueError) as error:
        worker_image.tool_versions(lock, destination)
    assert str(error.value) == "binary usage mismatch: codex-code-mode-host"


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.2.4", "url": "https://example.test/1.2.4/codex-code-mode-host"},
        {"companion": "missing"},
        {"companion": "codex-code-mode-host"},
        {"version_output": "codex-code-mode-host 1.2.3"},
        {"sha256": ""},
    ],
)
def test_lock_rejects_a_companion_off_its_codex_release(locked, change):
    path, lock, payloads = locked
    companion(lock, payloads).update(change)
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: codex-code-mode-host"


def test_lock_rejects_an_extra_tool_without_a_companion(locked):
    path, lock, payloads = locked
    del companion(lock, payloads)["companion"]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: codex-code-mode-host"


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


@pytest.mark.parametrize("field", ["url", "version_output"])
def test_declared_version_cannot_be_a_prefix_of_artifact_version(locked, field):
    path, lock, _ = locked
    lock["tools"]["herdr"][field] = lock["tools"]["herdr"][field].replace("1.2.3", "1.2.30")
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
    versions = Mock(return_value={"herdr": "herdr 1.2.3"})
    monkeypatch.setattr(worker_image, "tool_versions", versions)
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
    versions.assert_called_once_with(lock, Path("/bin"))
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
    path, lock, _ = locked
    observed = {"python": "3.12.12", "tools": {"herdr": "herdr 1.2.3"}}
    inventory = Mock(return_value=observed)
    monkeypatch.setattr(worker_image, "observed_inventory", inventory)
    monkeypatch.setattr(io, "text_encoding", lambda encoding, *args: "utf-16" if encoding is None else encoding)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "profile.md").write_text("fixture profile", encoding="utf-8")
    (templates / "empty").mkdir()
    worker_image.write_manifest(path, "amd64", "tested-commit", templates)
    manifest_bytes = path.with_name("manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    assert manifest_bytes == (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    inventory.assert_called_once_with(lock, Path("/usr/local/bin"))
    inventory.reset_mock()
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
    inventory.assert_called_once_with(lock, Path("/usr/local/bin"))
    manifest["declared"]["python"] = "bad"
    path.with_name("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        worker_image.report(path)
    assert str(error.value) == "installed inventory differs from build manifest"
    manifest["declared"]["python"] = "3.12.12"
    manifest["observed"] = {}
    path.with_name("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        worker_image.report(path)
    assert str(error.value) == "installed inventory differs from build manifest"


def test_build_rejects_selected_base_that_differs_from_lock(locked, monkeypatch):
    path, _, _ = locked
    monkeypatch.setattr("sys.argv", ["worker_image", "validate", "--lock", str(path), "--base-image", "python:latest"])
    with pytest.raises(ValueError) as error:
        worker_image.main()
    assert str(error.value) == "base image differs from lock"


@pytest.mark.parametrize("action", ["validate", "download", "install", "manifest", "report", "shell-packages"])
def test_build_command_routes_locked_actions(locked, monkeypatch, capsys, action):
    path, lock, _ = locked
    lock["shell_packages"].update(bash="5.2", curl="7.88")
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker_image",
            action,
            "--lock",
            str(path),
            "--architecture",
            "amd64",
            "--source-revision",
            "tested",
            "--cache",
            "/var/cache/tools",
        ],
    )
    fetch = Mock()
    install = Mock()
    manifest = Mock()
    report = Mock(return_value={"zeta": 2, "alpha": 1})
    monkeypatch.setattr(worker_image, "fetch", fetch)
    monkeypatch.setattr(worker_image, "install_tools", install)
    monkeypatch.setattr(worker_image, "write_manifest", manifest)
    monkeypatch.setattr(worker_image, "report", report)
    worker_image.main()
    if action == "download":
        assert fetch.call_args_list == [
            call(name, artifact, Path("/var/cache/tools")) for name, artifact in lock["tools"].items()
        ]
        install.assert_not_called()
    elif action == "install":
        install.assert_called_once_with(lock, Path("/usr/local/bin"), Path("/var/cache/tools"))
    elif action == "manifest":
        manifest.assert_called_once_with(path, "amd64", "tested", Path("/opt/agentihooks/templates"))
    elif action == "report":
        report.assert_called_once_with(path)
        assert capsys.readouterr().out == '{"alpha": 1, "zeta": 2}\n'
    elif action == "shell-packages":
        assert capsys.readouterr().out == "git=1:2.39.5-0+deb12u3 bash=5.2 curl=7.88\n"
    else:
        fetch.assert_not_called()
        install.assert_not_called()
        manifest.assert_not_called()
        report.assert_not_called()


@pytest.mark.parametrize("field", ["version", "sha256", "url", "version_output"])
def test_missing_artifact_metadata_has_the_rejection_contract(locked, field):
    path, lock, _ = locked
    del lock["tools"]["herdr"][field]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError) as error:
        worker_image.load_lock(path, "amd64")
    assert str(error.value) == "invalid artifact lock: herdr"


def test_lock_decodes_utf8_independently_of_the_default_codec(locked, monkeypatch):
    path, lock, _ = locked
    lock["tools"]["herdr"]["version_output"] = "hé rdr 1.2.3"
    path.write_text(json.dumps(lock, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(io, "text_encoding", lambda encoding, *args: "latin-1" if encoding is None else encoding)
    assert worker_image.load_lock(path, "amd64") == lock


def test_version_probe_rejects_a_failed_binary_with_matching_output(locked, tmp_path):
    _, lock, _ = locked
    (tmp_path / "herdr").write_text("#!/bin/sh\nprintf 'herdr 1.2.3\\n'\nexit 7\n")
    (tmp_path / "herdr").chmod(0o755)
    with pytest.raises(worker_image.subprocess.CalledProcessError) as error:
        worker_image.tool_versions(lock, tmp_path)
    assert error.value.returncode == 7


def test_archive_requires_the_locked_gzip_format(locked, tmp_path, monkeypatch):
    _, lock, payloads = locked
    url = lock["tools"]["codex"]["url"]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("codex-linux")
        member.size = len(payloads[url])
        archive.addfile(member, io.BytesIO(payloads[url]))
    payloads[url] = buffer.getvalue()
    lock["tools"]["codex"].update(member="codex-linux", sha256=hashlib.sha256(payloads[url]).hexdigest())
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    destination = tmp_path / "bin"
    destination.mkdir()
    with pytest.raises(tarfile.ReadError):
        worker_image.install_tools(lock, destination)
    assert list(destination.iterdir()) == []


def test_command_defaults_use_the_image_paths_and_unknown_revision(locked, monkeypatch):
    _, lock, _ = locked
    load = Mock(return_value=lock)
    manifest = Mock()
    monkeypatch.setattr(worker_image, "load_lock", load)
    monkeypatch.setattr(worker_image, "write_manifest", manifest)
    monkeypatch.setattr("sys.argv", ["worker_image", "manifest"])
    worker_image.main()
    load.assert_called_once_with(Path("/opt/swarm-node/versions.lock"), "amd64")
    manifest.assert_called_once_with(
        Path("/opt/swarm-node/versions.lock"), "amd64", "unknown", Path("/opt/agentihooks/templates")
    )


def test_command_rejects_unknown_actions(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["worker_image", "unexpected"])
    with pytest.raises(SystemExit) as error:
        worker_image.main()
    assert error.value.code == 2
    assert "invalid choice: 'unexpected'" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["download", "install"])
def test_a_named_tool_limits_download_and_install_to_that_tool(locked, monkeypatch, action):
    path, lock, _ = locked
    argv = ["worker_image", action, "--lock", str(path), "--tool", "herdr", "--cache", "/var/cache/tools"]
    monkeypatch.setattr("sys.argv", argv)
    fetch = Mock()
    install = Mock()
    monkeypatch.setattr(worker_image, "fetch", fetch)
    monkeypatch.setattr(worker_image, "install_tools", install)
    worker_image.main()
    herdr = lock["tools"]["herdr"]
    if action == "download":
        assert fetch.call_args_list == [call("herdr", herdr, Path("/var/cache/tools"))]
    else:
        install.assert_called_once_with(
            {**lock, "tools": {"herdr": herdr}}, Path("/usr/local/bin"), Path("/var/cache/tools")
        )


def test_a_named_tool_installs_and_checks_only_that_binary(locked, tmp_path, monkeypatch):
    path, lock, payloads = locked
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payloads[url]))
    installed = Mock(wraps=worker_image.install_tools)
    monkeypatch.setattr(worker_image, "install_tools", lambda lock, _, cache: installed(lock, tmp_path, cache))
    monkeypatch.setattr("sys.argv", ["worker_image", "install", "--lock", str(path), "--tool", "herdr"])
    worker_image.main()
    assert [entry.name for entry in tmp_path.iterdir() if entry.name in lock["tools"]] == ["herdr"]


def test_command_rejects_an_unlocked_tool(locked, monkeypatch, capsys):
    path, _, _ = locked
    monkeypatch.setattr("sys.argv", ["worker_image", "install", "--lock", str(path), "--tool", "curl"])
    with pytest.raises(SystemExit) as error:
        worker_image.main()
    assert error.value.code == 2
    assert "invalid choice: 'curl'" in capsys.readouterr().err


def test_a_named_package_limits_shell_packages_to_that_package(locked, monkeypatch, capsys):
    path, lock, _ = locked
    lock["shell_packages"].update(bash="5.2", **{"openssh-client": "1:9.2p1-2+deb12u7"})
    path.write_text(json.dumps(lock))
    argv = ["worker_image", "shell-packages", "--lock", str(path), "--package", "openssh-client", "--package", "git"]
    monkeypatch.setattr("sys.argv", argv)
    worker_image.main()
    assert capsys.readouterr().out == "openssh-client=1:9.2p1-2+deb12u7 git=1:2.39.5-0+deb12u3\n"


def test_an_unlocked_package_is_refused(locked, monkeypatch, capsys):
    path, _, _ = locked
    argv = ["worker_image", "shell-packages", "--lock", str(path), "--package", "zsh", "--package", "curl"]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as error:
        worker_image.main()
    assert error.value.code == 2
    assert "--package names packages the lock does not pin: curl, zsh" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["install", "download"])
@pytest.mark.parametrize("failure", [500, 504, 599, "timeout", "wrapped-timeout", "read-timeout"])
@pytest.mark.parametrize("failures", [1, 4, 5])
def test_image_tool_commands_recover_from_a_server_error(locked, tmp_path, monkeypatch, action, failure, failures):
    path, lock, payloads = locked
    herdr = lock["tools"]["herdr"]
    attempts = []
    sleeps = Mock()
    monkeypatch.setattr("time.sleep", sleeps)
    if isinstance(failure, int):
        error = urllib.error.HTTPError(herdr["url"], failure, "Server error", {}, None)
    elif failure == "wrapped-timeout":
        error = urllib.error.URLError(TimeoutError("timed out"))
    else:
        error = TimeoutError("timed out")

    def opened(url, timeout):
        assert timeout == 120
        attempts.append(url)
        response = io.BytesIO(payloads[url])
        if attempts.count(url) <= failures:
            if failure == "read-timeout":
                response.read = Mock(side_effect=error)
            else:
                raise error
        return response

    monkeypatch.setattr(worker_image.urllib.request, "urlopen", opened)
    install = worker_image.install_tools
    monkeypatch.setattr(worker_image, "install_tools", lambda lock, _, cache: install(lock, tmp_path, cache))
    cache = tmp_path / "cache"
    cache.mkdir()
    argv = ["worker_image", action, "--lock", str(path), "--cache", str(cache)]
    if action == "install":
        argv.extend(["--tool", "herdr"])
    monkeypatch.setattr("sys.argv", argv)
    selected = {"herdr": herdr} if action == "install" else lock["tools"]
    if failures == 5:
        with pytest.raises(type(error)) as caught:
            worker_image.main()
        assert caught.value is error
        assert not list(cache.iterdir())
        assert not any((tmp_path / name).exists() for name in selected)
    else:
        worker_image.main()
        for name, tool in selected.items():
            assert (cache / tool["sha256"]).read_bytes() == payloads[tool["url"]]
            if action == "install":
                assert (tmp_path / name).read_bytes() == payloads[tool["url"]]
    expected_attempts, delays = {
        1: (2, [1]),
        4: (5, [1, 2, 4, 8]),
        5: (5, [1, 2, 4, 8]),
    }[failures]
    assert {url: attempts.count(url) for url in attempts} == {
        tool["url"]: expected_attempts for tool in selected.values()
    }
    assert sorted(entry.args[0] for entry in sleeps.call_args_list) == sorted(delays * len(selected))


@pytest.mark.parametrize("status", [404, 499, 600, "dns"])
def test_tool_download_does_not_retry_permanent_errors(locked, tmp_path, monkeypatch, status):
    _, lock, _ = locked
    tool = lock["tools"]["herdr"]
    error = (
        urllib.error.URLError("name resolution failed")
        if status == "dns"
        else urllib.error.HTTPError(tool["url"], status, "Permanent error", {}, None)
    )
    opened = Mock(side_effect=error)
    sleeps = Mock()
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", opened)
    monkeypatch.setattr("time.sleep", sleeps)
    with pytest.raises(type(error)) as caught:
        worker_image.fetch("herdr", tool, tmp_path)
    assert caught.value is error
    opened.assert_called_once_with(tool["url"], timeout=120)
    sleeps.assert_not_called()
    assert not (tmp_path / tool["sha256"]).exists()


@pytest.mark.parametrize("action", ["install", "download"])
def test_image_tool_commands_reject_bad_checksums_without_retry(locked, tmp_path, monkeypatch, action):
    path, lock, payloads = locked
    herdr = lock["tools"]["herdr"]
    opened = Mock(side_effect=lambda url, timeout: io.BytesIO(b"corrupt" if url == herdr["url"] else payloads[url]))
    sleeps = Mock()
    monkeypatch.setattr(worker_image.urllib.request, "urlopen", opened)
    monkeypatch.setattr("time.sleep", sleeps)
    install = worker_image.install_tools
    monkeypatch.setattr(worker_image, "install_tools", lambda lock, _, cache: install(lock, tmp_path, cache))
    cache = tmp_path / "cache"
    cache.mkdir()
    argv = ["worker_image", action, "--lock", str(path), "--cache", str(cache)]
    if action == "install":
        argv.extend(["--tool", "herdr"])
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(ValueError, match="artifact checksum mismatch: herdr"):
        worker_image.main()
    assert opened.call_args_list.count(call(herdr["url"], timeout=120)) == 1
    sleeps.assert_not_called()
    assert not (cache / herdr["sha256"]).exists()
    assert not (tmp_path / "herdr").exists()
