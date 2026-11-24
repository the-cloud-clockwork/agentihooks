import io
import json
import os
import subprocess
import sys
import tarfile
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


WORKFLOWS = ROOT / ".github/workflows"
HIVE_PUBLISH = "Publish the hive worker once per release version"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / "swarm-node-image.yml").read_text())


def _chart_steps() -> list[dict[str, Any]]:
    steps = _workflow()["jobs"]["hive"]["steps"]
    names = [step.get("name") for step in steps]
    assert HIVE_PUBLISH in names, "Releases must publish the hive worker before the chart"
    return steps[names.index(HIVE_PUBLISH) + 1 :]


@pytest.mark.feature("release-chart-proven-and-pushed-by-the-hive-job")
def test_the_hive_job_proves_and_pushes_the_release_chart_on_its_own_runner() -> None:
    workflow = _workflow()
    assert workflow["name"] == "Swarm worker image"
    hive = workflow["jobs"]["hive"]
    assert hive["if"] == "github.event_name == 'release'"
    assert hive["needs"] == "image"
    assert hive["permissions"] == {"contents": "read", "packages": "write"}
    assert not hive.get("continue-on-error")
    published = yaml.safe_load((WORKFLOWS / "helm-kind-published.yml").read_text())
    assert hive["runs-on"] == published["jobs"]["kind"]["runs-on"]
    steps = _chart_steps()
    assert [step.get("name", step.get("uses")) for step in steps] == [
        "azure/setup-helm@v4",
        "Package the release chart",
        "Install kind",
        "Install the packaged release chart in kind",
        "Publish the release chart that kind installed",
    ]
    assert steps[0]["with"] == {"version": "v3.18.6"}
    assert all(not step.get("continue-on-error") and "if" not in step for step in steps)
    assert steps[-1]["env"] == {"GHCR_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    texts = {path.name: path.read_text() for path in sorted(WORKFLOWS.glob("*.y*ml"))}
    assert [name for name, text in texts.items() if "helm package" in text or "helm push" in text] == [
        "swarm-node-image.yml"
    ]
    promote = yaml.safe_load((WORKFLOWS / "promote.yml").read_text())
    release = next(step for step in promote["jobs"]["promote"]["steps"] if "gh release create" in step.get("run", ""))
    assert release["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


@pytest.mark.feature("release-chart-proven-and-pushed-by-the-hive-job")
def test_the_chart_publishes_only_after_kind_installs_the_packaged_archive() -> None:
    _, package, install, kind, publish = _chart_steps()
    called = yaml.safe_load((WORKFLOWS / "helm-kind-published.yml").read_text())
    job = called["jobs"]["kind"]
    assert install["env"] == {"KIND_VERSION": job["env"]["KIND_VERSION"]}
    assert install["run"] == next(step["run"] for step in job["steps"] if step.get("name") == "Install kind")
    assert kind["env"] == {"KIND_NODE_IMAGE": job["env"]["KIND_NODE_IMAGE"], "CHART_DIR": "${{ runner.temp }}/chart"}
    assert kind["run"] == 'bash deploy/helm/agentihooks-swarm/ci/kind-published.sh "$RELEASE"'
    assert '--destination "$RUNNER_TEMP/chart"' in package["run"]
    assert 'helm push "$RUNNER_TEMP/chart/agentihooks-swarm-$version.tgz"' in publish["run"]
    assert called[True]["workflow_call"] == {
        "inputs": {
            "version": {"type": "string", "required": True},
            "chart-artifact": {"type": "string", "default": ""},
        }
    }
    fetched, step = job["steps"][-2:]
    assert fetched["uses"].startswith("actions/download-artifact@")
    assert fetched["if"] == "inputs.chart-artifact != ''"
    assert fetched["with"] == {"name": "${{ inputs.chart-artifact }}", "path": "${{ runner.temp }}/packaged"}
    assert step["env"] == {
        "VERSION": "${{ inputs.version }}",
        "CHART_DIR": "${{ inputs.chart-artifact != '' && format('{0}/packaged', runner.temp) || '' }}",
    }
    assert step["run"] == 'bash deploy/helm/agentihooks-swarm/ci/kind-published.sh "$VERSION"'
    chart = ROOT / "deploy/helm/agentihooks-swarm"
    assert (chart / "ci/kind-values.yaml").is_file()
    ignored = (chart / ".helmignore").read_text().split() if (chart / ".helmignore").exists() else []
    shipped = ("ci", "ci/kind-values.yaml", "kind-values.yaml")
    assert not [pattern for pattern in ignored if any(fnmatch(path, pattern.strip("/")) for path in shipped)]


def _install_published(
    tmp_path: Path, mode: str, chart_dir: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    stub = tmp_path / "helm"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import io, json, os, sys, tarfile\n"
        "from pathlib import Path\n"
        "tool = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps([tool, *args]) + '\\n')\n"
        "if tool == 'helm' and args[0] == 'pull':\n"
        "    if os.environ['MODE'] == 'missing':\n"
        "        sys.exit(1)\n"
        "    destination = Path(args[args.index('--destination') + 1])\n"
        "    archive = destination / f\"agentihooks-swarm-{args[args.index('--version') + 1]}.tgz\"\n"
        "    with tarfile.open(archive, 'w:gz') as tar:\n"
        "        member = tarfile.TarInfo('agentihooks-swarm/ci/kind-values.yaml')\n"
        "        tar.addfile(member, io.BytesIO())\n"
        "if tool == 'helm' and args[0] == 'show':\n"
        "    print('version: ' + ('9.9.8' if os.environ['MODE'] == 'mismatch' else '9.9.9'))\n"
        "if tool == 'helm' and args[0] == 'install':\n"
        "    sys.exit(1)\n"
    )
    stub.chmod(0o755)
    for tool in ("kind", "docker", "kubectl"):
        (tmp_path / tool).symlink_to(stub)
    calls = tmp_path / "calls"
    env = {
        **{key: value for key, value in os.environ.items() if key != "GHCR_OWNER"},
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "MODE": mode,
        "KIND_NODE_IMAGE": "kindest/node:fixture",
        "CHART_DIR": chart_dir,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/helm/agentihooks-swarm/ci/kind-published.sh"), "v9.9.9"],
        env=env,
        capture_output=True,
        text=True,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result, [json.loads(line) for line in lines]


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "version 9.9.9 is not published"),
        ("mismatch", "the pulled chart is not agentihooks-swarm version 9.9.9"),
    ],
)
def test_a_missing_or_wrong_published_chart_fails_before_any_cluster_starts(tmp_path, mode, message):
    result, calls = _install_published(tmp_path, mode)
    assert result.returncode == 1
    assert calls[0][:3] == ["helm", "pull", "oci://ghcr.io/the-cloud-clockwork/charts/agentihooks-swarm"]
    assert calls[0][3:5] == ["--version", "9.9.9"]
    assert all(call[0] != "kind" for call in calls)
    assert message in result.stderr


def test_a_failed_install_of_the_published_chart_fails_and_deletes_the_cluster(tmp_path):
    result, calls = _install_published(tmp_path, "install")
    assert result.returncode == 1
    install = next(call for call in calls if call[:2] == ["helm", "install"])
    assert install[3].endswith("agentihooks-swarm-9.9.9.tgz")
    assert install[install.index("-f") + 1] == f"{install[3].rsplit('/', 1)[0]}/agentihooks-swarm/ci/kind-values.yaml"
    assert "image.tag=9.9.9" in install
    assert ["docker", "pull", "-q", "ghcr.io/the-cloud-clockwork/agentihooks-swarm:9.9.9"] in calls
    assert not [call for call in calls if call[:2] == ["helm", "test"]]
    assert calls[-1][:3] == ["kind", "delete", "cluster"]


def test_a_packaged_archive_installs_in_kind_without_pulling_the_registry(tmp_path: Path) -> None:
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    with tarfile.open(packaged / "agentihooks-swarm-9.9.9.tgz", "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("agentihooks-swarm/ci/kind-values.yaml"), io.BytesIO())
    result, calls = _install_published(tmp_path, "install", str(packaged))
    assert result.returncode == 1
    assert not [call for call in calls if call[:2] == ["helm", "pull"]]
    install = next(call for call in calls if call[:2] == ["helm", "install"])
    assert install[3].endswith("agentihooks-swarm-9.9.9.tgz")
    assert not install[3].startswith(str(packaged))
    assert calls[-1][:3] == ["kind", "delete", "cluster"]


def test_a_missing_packaged_archive_fails_before_any_cluster_starts(tmp_path: Path) -> None:
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    result, calls = _install_published(tmp_path, "install", str(packaged))
    assert result.returncode == 1
    assert all(call[0] != "kind" and call[:2] != ["helm", "pull"] for call in calls)
    assert "the packaged chart" in result.stderr and "version 9.9.9 is missing" in result.stderr


def _evaluated(value: str, expressions: dict[str, str]) -> str:
    for expression, replacement in expressions.items():
        value = value.replace(expression, replacement)
    assert "${{" not in value, f"the replay cannot evaluate {value}"
    return value


def _publish(tmp_path: Path, version: str, fail: str = "") -> tuple[int, str, list[list[str]]]:
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = tools / "helm"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "tool = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "chart = os.environ.get('CHART_DIR', '')\n"
        "with Path(os.environ['CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps([tool, *args, chart] if tool == 'kind-published.sh' else [tool, *args]) + '\\n')\n"
        "if tool == 'helm' and args[:2] == ['registry', 'login']:\n"
        "    assert sys.stdin.read() == os.environ['GHCR_TOKEN']\n"
        "if tool == 'kind-published.sh':\n"
        "    assert Path(chart, f\"agentihooks-swarm-{args[0].removeprefix('v')}.tgz\").is_file()\n"
        "if os.environ['FAIL_COMMAND'] == (tool if tool in ('sha256sum', 'kind-published.sh') else args[0]):\n"
        "    sys.exit(23)\n"
        "if tool == 'helm' and args[0] == 'package':\n"
        "    destination = Path(args[args.index('--destination') + 1])\n"
        "    destination.mkdir(parents=True, exist_ok=True)\n"
        "    version = args[args.index('--version') + 1]\n"
        "    (destination / f'agentihooks-swarm-{version}.tgz').touch()\n"
        "if tool == 'helm' and args[0] == 'push':\n"
        "    assert Path(args[1]).is_file()\n"
    )
    stub.chmod(0o755)
    for tool in ("sha256sum", "kind", "kubectl", "kind-published.sh"):
        (tools / tool).symlink_to(stub)
    chart = tmp_path / "tree/deploy/helm/agentihooks-swarm"
    (chart / "ci").mkdir(parents=True)
    source = ROOT / "deploy/helm/agentihooks-swarm"
    (chart / "values.yaml").write_text((source / "values.yaml").read_text())
    (chart / "ci/kind-published.sh").write_text(f'exec "{tools}/kind-published.sh" "$@"\n')
    calls = tmp_path / "calls"
    env = {
        **{key: value for key, value in os.environ.items() if key != "GHCR_TOKEN"},
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "FAIL_COMMAND": fail,
        "RELEASE": f"v{version}",
        "GITHUB_REPOSITORY_OWNER": "The-Cloud-Clockwork",
        "GITHUB_ACTOR": "release-bot",
        "RUNNER_TEMP": str(tmp_path / "runner temp"),
    }
    expressions = {"${{ runner.temp }}": env["RUNNER_TEMP"], "${{ secrets.GITHUB_TOKEN }}": "fixture"}
    status, output = 0, ""
    for step in (step for step in _chart_steps() if "run" in step):
        values = {key: _evaluated(value, expressions) for key, value in step.get("env", {}).items()}
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            env={**env, **values},
            capture_output=True,
            text=True,
            cwd=tmp_path / "tree",
        )
        output += result.stdout + result.stderr
        if result.returncode:
            status = result.returncode
            break
    lines = calls.read_text().splitlines() if calls.exists() else []
    return status, output, [json.loads(line) for line in lines]


@pytest.mark.parametrize("version", ["2.9.0", "2.10.1"])
def test_the_published_chart_uses_the_release_version_and_a_separate_oci_namespace(
    tmp_path: Path, version: str
) -> None:
    status, output, calls = _publish(tmp_path, version)
    assert status == 0, output
    packaged = str(tmp_path / "runner temp/chart")
    assert calls == [
        ["sha256sum", "-c", "deploy/helm/agentihooks-swarm-deps.sha256"],
        [
            "helm",
            "package",
            "deploy/helm/agentihooks-swarm",
            "--version",
            version,
            "--app-version",
            version,
            "--destination",
            packaged,
        ],
        ["kind", "version"],
        ["helm", "version", "--short"],
        ["kubectl", "version", "--client"],
        ["kind-published.sh", f"v{version}", packaged],
        ["helm", "registry", "login", "ghcr.io", "--username", "release-bot", "--password-stdin"],
        ["helm", "push", f"{packaged}/agentihooks-swarm-{version}.tgz", "oci://ghcr.io/the-cloud-clockwork/charts"],
    ]
    assert "fixture" not in output


@pytest.mark.feature("release-chart-proven-and-pushed-by-the-hive-job")
@pytest.mark.parametrize(
    "fail,last",
    [
        ("sha256sum", ["sha256sum"]),
        ("package", ["helm", "package"]),
        ("kind-published.sh", ["kind-published.sh"]),
        ("registry", ["helm", "registry"]),
        ("push", ["helm", "push"]),
    ],
)
def test_a_failed_checksum_package_kind_check_or_registry_step_stops_publication(
    tmp_path: Path, fail: str, last: list[str]
) -> None:
    status, _, calls = _publish(tmp_path, "2.10.1", fail)
    assert status == 23
    assert calls[-1][: len(last)] == last
