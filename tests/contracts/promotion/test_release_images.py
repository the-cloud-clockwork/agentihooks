import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from tests.test_ci_hosted_lanes import _resolve

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
IMAGES = ("swarm-image.yml", "swarm-node-image.yml", "ci-lab-image.yml")
TAG = {"event_name": "release", "ref": "refs/tags/v2.9.1", "run_id": 1000}
WORKER = "registry/agentihooks-worker"
SWARM = "ghcr.io/the-cloud-clockwork/agentihooks-swarm"
COMMITS = {"v2.1.7": "aaa", "v2.1.7rc1": "aaa", "v2.1.8": "bbb"}
SEEDED = {"2.1.6": "d6", "release": "d6", "sha-aaa": "d7", "sha-bbb": "d8"}


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(job, name):
    [step] = [step for step in job["steps"] if step.get("name") == name]
    return step


def _job(name):
    jobs = _load(name)["jobs"]
    return jobs.get("image") or jobs["publish"]


def test_promotion_creates_the_release_with_the_app_token_so_its_event_starts_workflows():
    job = _load("promote.yml")["jobs"]["promote"]
    release = _step(job, "Create the release with generated notes")
    assert release["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "gh release create" in release["run"]


@pytest.mark.parametrize("name", IMAGES)
def test_each_image_workflow_publishes_on_a_published_release_in_its_own_run(name):
    assert _load(name)[True]["release"] == {"types": ["published"]}
    assert not _resolve(_load(name)["concurrency"], **TAG)["cancel-in-progress"]


@pytest.mark.parametrize("name", IMAGES)
def test_a_release_build_refuses_a_tag_that_main_does_not_hold(name):
    job = _job(name)
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == "${{ github.event_name == 'release' && '0' || '1' }}"
    guard = _step(job, "Require a release tag on main")
    assert guard["if"] == "github.event_name == 'release'"
    position = job["steps"].index(guard)
    publishing = [i for i, step in enumerate(job["steps"]) if "login-action" in step.get("uses", "")]
    assert publishing
    assert position < publishing[0]


def _guard(tmp_path, ref_name, on_main=True):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for tag in ("v2.9.0", "v2.10.0", "v2.9.1"):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", tag], check=True)
        subprocess.run([*git, "tag", tag], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    if not on_main:
        subprocess.run([*git, "checkout", "-q", "--orphan", "side"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "side"], check=True)
        subprocess.run([*git, "tag", "v9.0.0"], check=True)
    sha = subprocess.run([*git, "rev-parse", f"{ref_name}^{{commit}}"], capture_output=True, text=True, check=True)
    output = tmp_path / "output"
    run = _step(_job("swarm-image.yml"), "Require a release tag on main")["run"]
    env = {**os.environ, "GITHUB_SHA": sha.stdout.strip(), "GITHUB_REF_NAME": ref_name, "GITHUB_OUTPUT": str(output)}
    done = subprocess.run(["bash", "-e", "-c", run], cwd=tmp_path, env=env)
    return done.returncode, output.read_text() if output.exists() else ""


def test_both_image_workflows_share_the_release_guard():
    runs = {name: _step(_job(name), "Require a release tag on main")["run"] for name in IMAGES}
    assert len(set(runs.values())) == 1


@pytest.mark.parametrize(("ref_name", "latest"), [("v2.10.0", "true"), ("v2.9.1", "false"), ("v2.9.0", "false")])
def test_only_the_newest_stable_tag_on_main_moves_the_release_tag(tmp_path, ref_name, latest):
    assert _guard(tmp_path, ref_name) == (0, f"latest={latest}\n")


def test_a_release_tag_off_main_publishes_nothing(tmp_path):
    code, output = _guard(tmp_path, "v9.0.0", on_main=False)
    assert code != 0
    assert output == ""


@pytest.mark.feature("swarm-release-tag-moves-only-for-the-newest-published-release")
def test_the_swarm_release_build_pushes_only_its_commit_image(tmp_path: Path) -> None:
    job = _job("swarm-image.yml")
    assert "github.event_name == 'release'" in job["if"]
    tags = next(step for step in job["steps"] if step.get("id") == "tags")
    assert tags["env"] == {"IMAGE": SWARM, "RELEASE": "${{ github.event.release.tag_name }}"}

    def run(release: str) -> list[str]:
        output = tmp_path / f"output-{release or 'dev'}"
        env = {
            **os.environ,
            **tags["env"],
            "RELEASE": release,
            "GITHUB_SHA": "123456789abcdef",
            "GITHUB_OUTPUT": str(output),
        }
        subprocess.run(["bash", "-e", "-c", tags["run"]], env=env, check=True)
        return output.read_text().splitlines()

    assert run("v2.9.1") == ["tags<<EOF", f"{SWARM}:sha-123456789abcdef", "EOF"]
    assert run("") == ["tags<<EOF", f"{SWARM}:dev", f"{SWARM}:dev-1234567", "EOF"]


def test_the_worker_release_publishes_the_qualified_candidate_but_never_moves_dev() -> None:
    job = _job("swarm-node-image.yml")
    for name in ("Log in to the registry", "Publish the qualified candidate"):
        assert _step(job, name)["if"].startswith("github.event_name == 'release' || ")
    dev = _step(job, "Move the floating dev tag to the published digest")
    assert "github.ref == 'refs/heads/dev'" in dev["if"]


def _fakes(
    tmp_path: Path, tags: str, seeded: dict[str, str], error: str = "", unreachable: bool = False
) -> tuple[Path, dict[str, str]]:
    tools = tmp_path / "bin"
    tools.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    for tag, digest in seeded.items():
        (registry / tag).write_text(digest)
    (tools / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "docker %s\\n" "$*" >> "$COMMAND_LOG"\n'
        '[[ "$1 $2" == "buildx imagetools" ]] || exit 2\n'
        'if [[ "$3" == inspect ]]; then\n'
        '  [[ "${4##*:}" != "$FAKE_ERROR" ]] || { echo "ERROR: $4: unauthorized" >&2; exit 1; }\n'
        '  [[ -f "$REGISTRY/${4##*:}" ]] && exit 0\n'
        '  echo "ERROR: $4: not found" >&2; exit 1\n'
        "fi\n"
        "shift 3\n"
        "tags=()\n"
        'while [[ "$1" == --tag ]]; do tags+=("${2##*:}"); shift 2; done\n'
        'for tag in "${tags[@]}"; do cp "$REGISTRY/${1##*:}" "$REGISTRY/$tag"; done\n'
    )
    (tools / "git").write_text(
        "#!/usr/bin/env bash\n"
        'printf "git %s\\n" "$*" >> "$GIT_LOG"\n'
        '[[ "$*" == "ls-remote --tags --refs origin" ]] || exit 2\n'
        '[[ -z "$FAKE_UNREACHABLE" ]] || { echo "fatal: unable to access origin" >&2; exit 128; }\n'
        'cat "$FAKE_TAGS"\n'
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    listed = (*tags.split(), "v2.1.7rc1", "v2.1.10-beta")
    (tmp_path / "tags").write_text("".join(f"{0:040d}\trefs/tags/{tag}\n" for tag in listed))
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "COMMAND_LOG": str(tmp_path / "commands"),
        "GIT_LOG": str(tmp_path / "git"),
        "REGISTRY": str(registry),
        "FAKE_ERROR": error,
        "FAKE_UNREACHABLE": "yes" if unreachable else "",
        "FAKE_TAGS": str(tmp_path / "tags"),
    }
    return registry, env


def _worker_release(
    tmp_path: Path, tags: str, error: str = "", unreachable: bool = False
) -> tuple[Path, Callable[[str], subprocess.CompletedProcess[str]]]:
    registry, env = _fakes(tmp_path, tags, SEEDED, error, unreachable)

    def release(tag: str) -> subprocess.CompletedProcess[str]:
        argv = ["bash", str(ROOT / "docker/swarm-node/release.sh"), WORKER, tag, COMMITS[tag]]
        return subprocess.run(argv, env=env, capture_output=True, text=True, cwd=ROOT)

    return registry, release


def _tags(registry: Path) -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(registry.iterdir())}


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("v2.1.8", "v2.1.7"), id="older-finishes-last"),
        pytest.param(("v2.1.7", "v2.1.8"), id="newer-finishes-last"),
    ],
)
def test_the_worker_release_tag_stays_on_the_newer_release_whichever_finishes_last(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    registry, release = _worker_release(tmp_path, "v2.1.6 v2.1.7 v2.1.8")
    for tag in order:
        result = release(tag)
        assert result.returncode == 0, result.stderr
    assert _tags(registry) == {**SEEDED, "2.1.7": "d7", "2.1.8": "d8", "release": "d8"}


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
def test_a_worker_release_moves_the_release_tag_before_its_version_while_a_newer_release_is_only_tagged(
    tmp_path: Path,
) -> None:
    registry, release = _worker_release(tmp_path, "v2.1.6 v2.1.7 v2.1.8")
    result = release("v2.1.7")
    assert result.returncode == 0, result.stderr
    assert _tags(registry) == {**SEEDED, "2.1.7": "d7", "release": "d7"}
    creates = [line for line in (tmp_path / "commands").read_text().splitlines() if " create " in line]
    assert creates == [
        f"docker buildx imagetools create --tag {WORKER}:release {WORKER}:sha-aaa",
        f"docker buildx imagetools create --tag {WORKER}:2.1.7 {WORKER}:sha-aaa",
    ]


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
def test_a_registry_error_on_a_newer_worker_release_fails_before_moving_any_tag(tmp_path: Path) -> None:
    registry, release = _worker_release(tmp_path, "v2.1.6 v2.1.7 v2.1.8", error="2.1.8")
    result = release("v2.1.7")
    assert result.returncode != 0
    assert "unauthorized" in result.stderr
    assert _tags(registry) == SEEDED


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
def test_a_failed_tag_read_fails_the_worker_release_before_moving_any_tag(tmp_path: Path) -> None:
    registry, release = _worker_release(tmp_path, "v2.1.6 v2.1.7", unreachable=True)
    result = release("v2.1.7")
    assert result.returncode != 0
    assert "unable to access" in result.stderr
    assert _tags(registry) == SEEDED


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
def test_a_worker_prerelease_tags_only_its_version_without_reading_tags(tmp_path: Path) -> None:
    registry, release = _worker_release(tmp_path, "v2.1.6 v2.1.7")
    result = release("v2.1.7rc1")
    assert result.returncode == 0, result.stderr
    assert "v2.1.7rc1 is not a stable release; leaving the release tag where it is" in result.stdout
    assert _tags(registry) == {**SEEDED, "2.1.7rc1": "d7"}
    assert not (tmp_path / "git").exists()


@pytest.mark.feature("worker-release-tag-moves-only-for-the-newest-published-release")
def test_the_queued_hive_job_moves_the_worker_release_tag_and_the_image_job_moves_none() -> None:
    jobs = _load("swarm-node-image.yml")["jobs"]
    moving = [step.get("name") for step in jobs["image"]["steps"] if ":release" in step.get("run", "")]
    assert moving == []
    hive = jobs["hive"]
    assert hive["concurrency"] == {"group": "hive-worker-release", "queue": "max", "cancel-in-progress": False}
    assert hive["env"]["WORKER"] == "ghcr.io/the-cloud-clockwork/agentihooks-worker"
    worker = _step(hive, "Tag the worker release and move the release tag unless a newer release already published")
    assert "env" not in worker
    assert worker["run"] == 'bash docker/swarm-node/release.sh "$WORKER" "$RELEASE" "$GITHUB_SHA"\n'
    names = [step.get("name", step.get("uses")) for step in hive["steps"]]
    assert names.index("docker/login-action@v3") < names.index(worker["name"])
    assert names.index(worker["name"]) < names.index("Publish the hive worker once per release version")


def _resolved(block: dict[str, str], tag: str) -> dict[str, str]:
    values = {"${{ github.event.release.tag_name }}": tag, "${{ steps.main.outputs.latest }}": "true"}
    assert [value for value in block.values() if value.startswith("${{") and value not in values] == []
    return {key: values.get(value, value) for key, value in block.items()}


def _swarm_release_run(env: dict[str, str], registry: Path, tag: str) -> None:
    jobs = _load("swarm-image.yml")["jobs"]
    publish = jobs["publish"]["steps"]
    tags = next(step for step in publish if step.get("id") == "tags")
    build = next(step for step in publish if "build-push-action" in step.get("uses", ""))
    assert build["with"]["tags"] == "${{ steps.tags.outputs.tags }}"
    output = registry.parent / f"tags-{tag}"
    built = {**env, **_resolved(tags["env"], tag), "GITHUB_SHA": COMMITS[tag], "GITHUB_OUTPUT": str(output)}
    subprocess.run(["bash", "-e", "-c", tags["run"]], env=built, check=True)
    head, *images, tail = output.read_text().splitlines()
    assert (head, tail) == ("tags<<EOF", "EOF")
    for image in images:
        (registry / image.rsplit(":", 1)[1]).write_text(SEEDED[f"sha-{COMMITS[tag]}"])
    moves = [(job, step) for job in jobs.values() if job.get("needs") == "publish" for step in job["steps"]]
    for job, step in moves:
        if "run" in step:
            moved = {**env, **_resolved(job.get("env", {}), tag), **_resolved(step.get("env", {}), tag)}
            argv = ["bash", "-e", "-c", step["run"]]
            done = subprocess.run(
                argv, env={**moved, "GITHUB_SHA": COMMITS[tag]}, cwd=ROOT, capture_output=True, text=True
            )
            assert done.returncode == 0, done.stderr


@pytest.mark.feature("swarm-release-tag-moves-only-for-the-newest-published-release")
@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("v2.1.8", "v2.1.7"), id="older-finishes-last"),
        pytest.param(("v2.1.7", "v2.1.8"), id="newer-finishes-last"),
    ],
)
def test_the_swarm_release_tag_stays_on_the_newer_of_two_overlapping_releases_whichever_finishes_last(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    registry, env = _fakes(tmp_path, "v2.1.6 v2.1.7 v2.1.8", {"2.1.6": "d6", "release": "d6"})
    for tag in order:
        _swarm_release_run(env, registry, tag)
    assert _tags(registry)["release"] == "d8"
    assert _tags(registry) == {
        "2.1.6": "d6",
        "2.1.7": "d7",
        "2.1.8": "d8",
        "release": "d8",
        "sha-aaa": "d7",
        "sha-bbb": "d8",
    }


@pytest.mark.feature("swarm-release-tag-moves-only-for-the-newest-published-release")
def test_the_release_job_moves_the_swarm_release_tag_and_the_publish_job_moves_none() -> None:
    jobs = _load("swarm-image.yml")["jobs"]
    moving = [
        step.get("name", step.get("id")) for step in jobs["publish"]["steps"] if ":release" in step.get("run", "")
    ]
    assert moving == []
    release = jobs["release"]
    assert release["if"] == "github.event_name == 'release'"
    assert release["needs"] == "publish"
    assert release["env"] == {"IMAGE": SWARM, "RELEASE": "${{ github.event.release.tag_name }}"}
    swarm = _step(release, "Tag the swarm release and move the release tag unless a newer release already published")
    assert "env" not in swarm
    assert swarm["run"] == 'bash docker/swarm-node/release.sh "$IMAGE" "$RELEASE" "$GITHUB_SHA"\n'
    names = [step.get("name", step.get("uses")) for step in release["steps"]]
    assert names.index("actions/checkout@v4") < names.index(swarm["name"])
    assert names.index("docker/login-action@v3") < names.index(swarm["name"])
