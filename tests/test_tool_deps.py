import base64
import hashlib
import importlib.metadata
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import tool_deps

BEFORE = '[project]\ndependencies = [\n    "redis>=5.0.0",\n    "mcp[cli]>=1.28,<2.0.0",\n]\n'
AFTER = '[project]\ndependencies = [\n    "redis>=5.0.0",\n    "mcp[cli]>=1.28,<2.0.0",\n    "croniter>=6.2.4,<7",\n]\n'


class FakeGit:
    def __init__(self, before, after):
        self.text, self.after, self.calls = before, after, []

    def __call__(self, argv):
        self.calls.append(argv)
        if "merge" in argv:
            self.text = self.after
        return self.text if "show" in argv else ""


def show(repo):
    return ["git", "-C", repo, "show", "HEAD:pyproject.toml"]


def sync(repo):
    return [["git", "-C", repo, "fetch", "origin", "dev"], ["git", "-C", repo, "merge", "--ff-only", "origin/dev"]]


def test_declared_reads_the_project_dependencies():
    assert tool_deps.declared(BEFORE) == ["redis>=5.0.0", "mcp[cli]>=1.28,<2.0.0"]


def test_declared_without_a_project_table_is_empty():
    assert tool_deps.declared("[tool.ruff]\nline-length = 120\n") == []
    assert tool_deps.declared('[project]\nname = "x"\n') == []


def test_missing_names_each_declared_spec_whose_distribution_is_absent():
    seen = []

    def found(name):
        seen.append(name)
        if name == "croniter":
            raise importlib.metadata.PackageNotFoundError(name)

    assert tool_deps.missing(tool_deps.declared(AFTER), found) == ["croniter>=6.2.4,<7"]
    assert seen == ["redis", "mcp", "croniter"]


def test_missing_here_reads_the_source_pyproject_from_disk(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=7", "agentihooks-absent-dist-for-test>=1"]\n'
    )
    monkeypatch.setattr(tool_deps, "SOURCE", tmp_path)
    assert tool_deps.missing_here() == ["agentihooks-absent-dist-for-test>=1"]


def test_missing_here_without_a_source_pyproject_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_deps, "SOURCE", tmp_path)
    assert tool_deps.missing_here() == []


def test_pull_that_adds_a_declared_dependency_installs_it_into_the_tool_environment(tmp_path):
    git, repo = FakeGit(BEFORE, AFTER), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == ["croniter>=6.2.4,<7"]
    assert git.calls == [
        show(repo),
        *sync(repo),
        show(repo),
        ["uv", "pip", "install", "--python", "/tool/python", "croniter>=6.2.4,<7"],
    ]


def test_pull_that_changes_a_declared_version_installs_the_new_spec(tmp_path):
    git, repo = FakeGit(BEFORE, BEFORE.replace("redis>=5.0.0", "redis>=6.0.0")), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == ["redis>=6.0.0"]
    assert git.calls[-1] == ["uv", "pip", "install", "--python", "/tool/python", "redis>=6.0.0"]


def test_pull_with_unchanged_dependencies_installs_nothing(tmp_path):
    git, repo = FakeGit(BEFORE, BEFORE), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == []
    assert git.calls == [show(repo), *sync(repo), show(repo)]


def test_pull_with_unchanged_dependencies_installs_a_declared_spec_still_missing(tmp_path):
    text = '[project]\ndependencies = ["redis>=5.0.0", "agentihooks-absent-dist-for-test>=1"]\n'
    git, repo = FakeGit(text, text), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == ["agentihooks-absent-dist-for-test>=1"]
    assert git.calls[-1] == [
        "uv",
        "pip",
        "install",
        "--python",
        "/tool/python",
        "agentihooks-absent-dist-for-test>=1",
    ]


def test_pull_installs_a_new_spec_once_even_when_it_is_also_missing(tmp_path):
    after = '[project]\ndependencies = ["redis>=5.0.0", "agentihooks-absent-dist-for-test>=1"]\n'
    git, repo = FakeGit(BEFORE, after), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == ["agentihooks-absent-dist-for-test>=1"]


def test_pull_that_only_removes_a_dependency_installs_nothing(tmp_path):
    git, repo = FakeGit(AFTER, BEFORE), str(tmp_path)
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == []
    assert git.calls == [show(repo), *sync(repo), show(repo)]


def test_pull_of_another_repository_never_installs_its_dependencies(tmp_path):
    git, repo = FakeGit(BEFORE, AFTER), str(tmp_path / "other")
    assert tool_deps.pull(repo, git, "/tool/python", source=tmp_path) == []
    assert git.calls == sync(repo)


def test_call_returns_stdout_and_raises_with_stderr_on_failure():
    assert tool_deps.call([sys.executable, "-c", "print(' out ')"]) == "out"
    code = "import sys; sys.stdout.write('out'); sys.stderr.write('boom'); sys.exit(3)"
    with pytest.raises(RuntimeError) as raised:
        tool_deps.call([sys.executable, "-c", code])
    assert str(raised.value) == f"{sys.executable} -c {code} failed: boom"


def test_call_failing_with_no_stderr_reports_its_stdout():
    code = "import sys; sys.stdout.write(' only out '); sys.exit(3)"
    with pytest.raises(RuntimeError) as raised:
        tool_deps.call([sys.executable, "-c", code])
    assert str(raised.value) == f"{sys.executable} -c {code} failed: only out"


def test_call_reports_a_timeout_as_a_failure(monkeypatch):
    monkeypatch.setattr(tool_deps, "TIMEOUT_SECONDS", 0.2)
    code = "import time; time.sleep(5)"
    with pytest.raises(RuntimeError) as raised:
        tool_deps.call([sys.executable, "-c", code])
    assert str(raised.value) == f"{sys.executable} -c {code} timed out after 0.2 seconds"


def test_install_of_nothing_runs_nothing():
    assert tool_deps.install([], lambda argv: pytest.fail(f"ran {argv}"), "/tool/python") == []


def _core():
    return tool_deps.declared((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())


@pytest.mark.parametrize("module", ["psycopg2", "boto3"])
def test_core_dependencies_name_the_distribution_of_each_module_the_tick_imports(module):
    providers = {name.lower() for name in importlib.metadata.packages_distributions().get(module, ())}
    core = {tool_deps._NAME.match(spec).group(1).lower() for spec in _core()}
    assert providers & core, f"{module} comes from {sorted(providers)}, none of them a core dependency"


def test_missing_here_names_the_tick_database_and_storage_distributions_when_absent(monkeypatch):
    def found(name):
        if name in {"psycopg2-binary", "boto3"}:
            raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(tool_deps, "SOURCE", Path(__file__).resolve().parents[1])
    monkeypatch.setattr(tool_deps.missing, "__defaults__", (found,))
    absent = tool_deps.missing_here()
    assert sorted(tool_deps._NAME.match(spec).group(1) for spec in absent) == ["boto3", "psycopg2-binary"]


def _wheel(folder: Path) -> None:
    files = {
        "fakedep_t442/__init__.py": b"VALUE = 442\n",
        "fakedep_t442-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: fakedep-t442\nVersion: 1.0\n",
        "fakedep_t442-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append("fakedep_t442-1.0.dist-info/RECORD,,")
    with zipfile.ZipFile(folder / "fakedep_t442-1.0-py3-none-any.whl", "w") as wheel:
        for name, data in files.items():
            wheel.writestr(name, data)
        wheel.writestr("fakedep_t442-1.0.dist-info/RECORD", "\n".join(record) + "\n")


def _git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv installs the tool environment")
def test_a_real_sync_that_adds_a_declared_dependency_leaves_it_importable(tmp_path, monkeypatch):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    env |= {"UV_OFFLINE": "1", "UV_FIND_LINKS": str(wheels), "UV_NO_CACHE": "1"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    upstream, checkout = tmp_path / "upstream", tmp_path / "checkout"
    _git("init", "-q", "-b", "dev", str(upstream))
    (upstream / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    _git("-C", str(upstream), "add", "pyproject.toml")
    _git("-C", str(upstream), "commit", "-q", "-m", "before")
    _git("clone", "-q", "-b", "dev", str(upstream), str(checkout))
    (upstream / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["fakedep-t442>=1.0"]\n')
    _git("-C", str(upstream), "commit", "-q", "-am", "after")
    subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(tmp_path / "tool")], check=True)
    python = str(tmp_path / "tool" / "bin" / "python")
    probe = [python, "-c", "import fakedep_t442; print(fakedep_t442.VALUE)"]
    assert subprocess.run(probe, capture_output=True).returncode != 0
    installed = tool_deps.pull(str(checkout), tool_deps.call, python, source=checkout.resolve())
    assert installed == ["fakedep-t442>=1.0"]
    assert subprocess.run(probe, capture_output=True, text=True).stdout == "442\n"


def _upstream_and_checkout(tmp_path, monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t")
    upstream, checkout = tmp_path / "upstream", tmp_path / "checkout"
    _git("init", "-q", "-b", "dev", str(upstream))
    _git("-C", str(upstream), "commit", "-q", "--allow-empty", "-m", "before")
    _git("clone", "-q", "-b", "dev", str(upstream), str(checkout))
    _git("-C", str(upstream), "commit", "-q", "--allow-empty", "-m", "after")
    return upstream, checkout


def _rev(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True).stdout.strip()


def test_a_fetch_that_records_dev_twice_still_fast_forwards_the_checkout(tmp_path, monkeypatch):
    upstream, checkout = _upstream_and_checkout(tmp_path, monkeypatch)

    def twice(argv):
        if argv[-2:] == ["origin", "dev"]:
            argv = [*argv, "dev"]
        return tool_deps.call(argv)

    assert tool_deps.pull(str(checkout), twice, "/tool/python") == []
    assert (checkout / ".git" / "FETCH_HEAD").read_text().count("branch 'dev'") == 2
    assert _rev(checkout, "HEAD") == _rev(upstream, "HEAD")


def test_a_checkout_that_already_contains_origin_dev_pulls_as_success(tmp_path, monkeypatch):
    upstream, checkout = _upstream_and_checkout(tmp_path, monkeypatch)
    _git("-C", str(checkout), "pull", "-q", "--ff-only", "origin", "dev")
    _git("-C", str(checkout), "commit", "-q", "--allow-empty", "-m", "local")
    local = _rev(checkout, "HEAD")
    assert tool_deps.pull(str(checkout), tool_deps.call, "/tool/python") == []
    assert _rev(checkout, "HEAD") == local
    assert _rev(checkout, "origin/dev") == _rev(upstream, "HEAD")
