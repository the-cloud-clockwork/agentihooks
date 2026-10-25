import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def _workflow():
    path = ROOT / ".github/workflows/chart-release.yml"
    assert path.is_file(), "Releases must publish the Helm chart to GHCR"
    return yaml.safe_load(path.read_text())


def test_the_promotion_release_event_publishes_the_chart_independently_of_images():
    workflow = _workflow()
    assert workflow.get("on", workflow.get(True)) == {"release": {"types": ["published"]}}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["publish"]
    assert not job.get("needs")
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert not job.get("continue-on-error")
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {
        "ref": "${{ github.event.release.tag_name }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert any(step.get("uses", "").startswith("azure/setup-helm@") for step in steps)
    publish = next(step for step in steps if "helm push" in step.get("run", ""))
    assert publish["env"]["RELEASE"] == "${{ github.event.release.tag_name }}"
    assert publish["env"]["GHCR_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert all(not step.get("continue-on-error") and "if" not in step for step in steps)
    promote = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    release = next(step for step in promote["jobs"]["promote"]["steps"] if "gh release create" in step.get("run", ""))
    assert release["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_the_chart_just_published_installs_on_kind_after_publication():
    job = _workflow()["jobs"]["kind"]
    assert job["needs"] == "publish"
    assert job["uses"] == "./.github/workflows/helm-kind-published.yml"
    assert job["with"] == {"version": "${{ github.event.release.tag_name }}"}
    assert not job.get("if") and not job.get("continue-on-error")
    called = yaml.safe_load((ROOT / ".github/workflows/helm-kind-published.yml").read_text())
    assert called[True]["workflow_call"] == {"inputs": {"version": {"type": "string", "required": True}}}
    step = called["jobs"]["kind"]["steps"][-1]
    assert step["env"] == {"VERSION": "${{ inputs.version }}"}
    assert step["run"] == 'bash deploy/helm/agentihooks-swarm/ci/kind-published.sh "$VERSION"'
    chart = ROOT / "deploy/helm/agentihooks-swarm"
    assert (chart / "ci/kind-values.yaml").is_file()
    ignored = (chart / ".helmignore").read_text().split() if (chart / ".helmignore").exists() else []
    shipped = ("ci", "ci/kind-values.yaml", "kind-values.yaml")
    assert not [pattern for pattern in ignored if any(fnmatch(path, pattern.strip("/")) for path in shipped)]


def _install_published(tmp_path, mode):
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
    }
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/helm/agentihooks-swarm/ci/kind-published.sh"), "v9.9.9"],
        env=env,
        capture_output=True,
        text=True,
    )
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


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


def _publish(tmp_path, version, fail=""):
    stub = tmp_path / "helm"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "tool = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps([tool, *args]) + '\\n')\n"
        "if tool == 'helm' and args[:2] == ['registry', 'login']:\n"
        "    assert sys.stdin.read() == os.environ['GHCR_TOKEN']\n"
        "if os.environ['FAIL_COMMAND'] == (tool if tool in ('git', 'sha256sum') else args[0]):\n"
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
    (tmp_path / "git").symlink_to(stub)
    (tmp_path / "sha256sum").symlink_to(stub)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "FAIL_COMMAND": fail,
        "RELEASE": f"v{version}",
        "GITHUB_SHA": "promoted-release-commit",
        "GITHUB_REPOSITORY_OWNER": "The-Cloud-Clockwork",
        "GITHUB_ACTOR": "release-bot",
        "GHCR_TOKEN": "fixture",
        "RUNNER_TEMP": str(tmp_path / "runner temp"),
    }
    steps = _workflow()["jobs"]["publish"]["steps"]
    script = "\n".join(step["run"] for step in steps if "run" in step)
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


@pytest.mark.parametrize("version", ["2.9.0", "2.10.1"])
def test_the_published_chart_uses_the_release_version_and_a_separate_oci_namespace(tmp_path, version):
    result, calls = _publish(tmp_path, version)
    assert result.returncode == 0, result.stderr
    assert calls == [
        ["git", "merge-base", "--is-ancestor", "promoted-release-commit", "origin/main"],
        ["helm", "registry", "login", "ghcr.io", "--username", "release-bot", "--password-stdin"],
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
            str(tmp_path / "runner temp/chart"),
        ],
        [
            "helm",
            "push",
            str(tmp_path / f"runner temp/chart/agentihooks-swarm-{version}.tgz"),
            "oci://ghcr.io/the-cloud-clockwork/charts",
        ],
    ]
    assert "fixture" not in result.stdout + result.stderr


@pytest.mark.parametrize("fail,count", [("git", 1), ("registry", 2), ("sha256sum", 3), ("package", 4), ("push", 5)])
def test_off_main_releases_and_registry_or_packaging_failures_stop_publication(tmp_path, fail, count):
    result, calls = _publish(tmp_path, "2.10.1", fail)
    assert result.returncode == 23
    assert len(calls) == count
