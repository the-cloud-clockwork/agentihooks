import ast
import functools
import os
import re
import shlex
import subprocess
import tomllib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LOCK = ".github/test-requirements.lock"


def _unlocked_installs(root):
    failures = []
    for path in sorted((root / ".github").rglob("*")):
        if path.suffix in {".yml", ".yaml"}:
            document = yaml.safe_load(path.read_text()) or {}
            scripts = [
                (step.get("run", ""), document.get("env", {}) | job.get("env", {}) | step.get("env", {}))
                for job in document.get("jobs", {}).values()
                for step in job.get("steps", [])
            ]
            scripts += [
                (step.get("run", ""), step.get("env", {})) for step in document.get("runs", {}).get("steps", [])
            ]
        elif path.suffix == ".sh":
            scripts = [(path.read_text(), {})]
        else:
            continue
        for script, environment in scripts:
            script = script.replace("\\\n", " ")
            for line in script.splitlines():
                if not re.search(r"\bpip\s+install\b", line):
                    continue
                if not re.search(r"pytest|mutmut|coverage|\[[^]]*\bdev\b", line):
                    continue
                tokens = shlex.split(line)
                constrained = any(
                    token in {"-c", "--constraint"}
                    and index + 1 < len(tokens)
                    and tokens[index + 1].endswith(LOCK)
                    or token.startswith("--constraint=")
                    and token.endswith(LOCK)
                    for index, token in enumerate(tokens)
                )
                constraint = environment.get("UV_CONSTRAINT", "")
                if re.search(r"\buv\s+pip\s+install\b", line) and constraint.endswith(".github/test-constraints.txt"):
                    constrained |= (
                        "--constraint test-requirements.lock"
                        in (root / ".github/test-constraints.txt").read_text().splitlines()
                    )
                if not constrained:
                    failures.append(f"{path.relative_to(root)}: unlocked test tool install: {line.strip()}")
    return failures


def test_ci_test_tool_installs_use_the_repository_lock():
    assert not (failures := _unlocked_installs(ROOT)), "\n".join(failures)


@pytest.mark.parametrize("command", ["uv pip install --system -e '.[dev,all]'", "python -m pip install pytest mutmut"])
def test_dependency_inventory_rejects_a_planted_unlocked_install(tmp_path, command):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "plant.yml").write_text(yaml.safe_dump({"jobs": {"plant": {"steps": [{"run": command}]}}}))
    assert len(_unlocked_installs(tmp_path)) == 1
    (workflows / "plant.yml").write_text(
        yaml.safe_dump({"jobs": {"plant": {"steps": [{"run": f"{command} --constraint {LOCK}"}]}}})
    )
    assert _unlocked_installs(tmp_path) == []


def test_repository_lock_pins_pytest_plugins_and_mutmut():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tools = {
        Requirement(value).name
        for value in project["project"]["optional-dependencies"]["dev"]
        if Requirement(value).name.startswith("pytest") or Requirement(value).name == "mutmut"
    } | {"coverage"}
    requirements = [
        Requirement(line.strip())
        for line in (ROOT / LOCK).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for tool in tools:
        matches = [requirement for requirement in requirements if requirement.name == tool]
        assert matches, f"{tool} is missing from the repository lock"
        assert all(len(requirement.specifier) == 1 for requirement in matches)
        assert all(next(iter(requirement.specifier)).operator == "==" for requirement in matches)


@pytest.mark.parametrize(
    "workflow,job,checkout",
    [("mutation-line-selection-proof.yml", "compare", "head"), ("ledger-row-memory.yml", "replay", "proof")],
)
def test_historical_replays_use_the_current_checkouts_lock(workflow, job, checkout):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = document["jobs"][job]["steps"]
    current = next(step for step in steps if step.get("with", {}).get("path") == checkout)
    assert "ref" not in current["with"]
    install = next(step for step in steps if "pip install" in step.get("run", ""))
    assert install["working-directory"] != checkout
    tokens = shlex.split(install["run"])
    assert tokens[tokens.index("--constraint") + 1] == f"../{checkout}/{LOCK}"


@pytest.mark.parametrize("scope", ["workflow", "job", "step"])
@pytest.mark.parametrize("installer", ["uv pip", "python -m pip"])
def test_inherited_constraints_must_include_the_generated_lock(tmp_path, scope, installer):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    constraints = workflows.parent / "test-constraints.txt"
    constraints.write_text("pytest<9.2\n")
    step = {"run": f"{installer} install pytest mutmut"}
    job = {"steps": [step]}
    workflow = {"jobs": {"plant": job}}
    {"workflow": workflow, "job": job, "step": step}[scope]["env"] = {
        "UV_CONSTRAINT": "head/.github/test-constraints.txt"
    }
    (workflows / "plant.yml").write_text(yaml.safe_dump(workflow))
    assert len(_unlocked_installs(tmp_path)) == 1
    constraints.write_text("pytest<9.2\n--constraint test-requirements.lock\n")
    assert len(_unlocked_installs(tmp_path)) == (0 if installer == "uv pip" else 1)


def _named_steps(job: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str | None]]:
    """Return a job's steps and their names in order."""
    steps = job["steps"]
    return steps, [step.get("name") for step in steps]


@pytest.mark.parametrize("job", ["mutation-plan", "mutation-stats", "mutation-shard"])
def test_protected_mutation_jobs_install_with_no_branch_lock(tmp_path: Path, job: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps, names = _named_steps(workflow["jobs"][job])
    export = steps[names.index("Take the helper scripts from the workflow revision")]["run"]
    install = steps[names.index("Install dependencies")]["run"]
    trusted = tmp_path / "workflow"
    trusted.mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_SHA="HEAD")
    archive = next(line for line in export.splitlines() if line.strip().startswith("git archive"))
    subprocess.run(["bash", "-euc", archive], cwd=ROOT, env=env, check=True)
    branch = tmp_path / "old-branch"
    branch.mkdir()
    probe = """
uv() {
    [[ "$1 $2 $3 $4" == "pip install --system --constraint" ]] || return 1
    [[ "$5" == "$RUNNER_TEMP/workflow/.github/test-requirements.lock" ]] || return 1
    [[ -s "$5" ]] || return 1
    echo "Trusted dependency lock resolved without a branch lock"
}
export -f uv
"""
    result = subprocess.run(
        ["bash", "-euc", probe + install], cwd=branch, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Trusted dependency lock resolved without a branch lock" in result.stdout


@pytest.mark.parametrize("job", ["mutation-stats", "mutation-shard"])
def test_mutation_jobs_keep_the_head_lock_when_the_head_has_one(tmp_path: Path, job: str) -> None:
    steps, names = _named_steps(yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"][job])
    install = steps[names.index("Install dependencies")]["run"]
    helper = tmp_path / "workflow/.github/scripts/pypi-retry.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text('shift 2\n"$@"\n')
    branch = tmp_path / "branch"
    (branch / ".github").mkdir(parents=True)
    (branch / ".github/test-requirements.lock").write_text("pytest==8.0\n")
    probe = 'uv() { echo "constraint=$5"; }\nexport -f uv\n'
    result = subprocess.run(
        ["bash", "-euc", probe + install],
        cwd=branch,
        env=dict(os.environ, RUNNER_TEMP=str(tmp_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "constraint=./.github/test-requirements.lock"


CI_TOOLS = ".github/ci-tools/"
CONSTRAINTS = ".github/test-constraints.txt"
PLANT_LOCK = ".github/ci-tools/plant.lock"
UNCONSTRAINED = "no checked in constraint"
SOURCES = {".yml", ".yaml", ".sh", ".py"}
RUNNERS = {"uvx", "uv tool run", "pipx run"}
INSTALLER_WORDS = {"install", "uvx", "pipx", "tool"}
CONTROL = {"&&", "||", ";", "|", "&", "(", ")", "|&", ";;"}
REDIRECT = {">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>", ">|", "<>"}
CONSTRAINT_FLAGS = {"-c", "--constraint", "--constraints", "-r", "--requirement", "--requirements"}
VALUED = CONSTRAINT_FLAGS | {
    "-e",
    "--editable",
    "-p",
    "--python",
    "--excludes",
    "-f",
    "--find-links",
    "-i",
    "--index-url",
    "--extra-index-url",
    "--index",
    "--default-index",
    "--with",
    "--from",
    "--with-requirements",
    "--with-editable",
    "-t",
    "--target",
    "--prefix",
    "--root",
    "--overrides",
    "-b",
    "--build-constraints",
    "--extra",
    "--group",
    "--python-version",
    "--python-platform",
    "--platform",
    "--only-binary",
    "--no-binary",
    "-C",
    "--config-settings",
    "-P",
    "--upgrade-package",
    "--reinstall-package",
    "--exclude-newer",
    "--cache-dir",
    "--resolution",
    "--prerelease",
    "--index-strategy",
    "--keyring-provider",
    "--link-mode",
    "--pip-args",
    "--spec",
    "--suffix",
    "--src",
    "--log",
    "--trusted-host",
    "--allow-insecure-host",
    "--directory",
    "--project",
}
INSTALL_WORD = re.compile(r"\b(?:pip3?|uvx|pipx)\b|\buv\s+tool\b")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=.*")
ASSIGNMENT_LINE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_]\w*=")
ENV_REFERENCE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
SHELL_RUN = re.compile(r"(?:\$\{?\w+\}?/)?((?:[\w.-]+/)*[\w.-]+\.sh)\b")
MODULE_RUN = re.compile(r"\bpython3?\b[^\n;&|]*?\s-m\s+([A-Za-z_]\w*(?:\.\w+)*)")
FILE_RUN = re.compile(r"\bpython3?\s+(?:-\w+\s+)*((?:[\w.-]+/)*[\w.-]+\.py)\b")
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
INTENTIONAL_SMOKES = {
    ".github/workflows/test.yml::Install the wheel into a clean venv": (
        "installs the built wheel into a clean venv with the resolution a user gets from the package index"
    ),
    "scripts/packaging/swarm-init-smoke.sh": (
        "installs the checkout into a fresh venv with its natural resolution to prove a new user's first swarm init"
    ),
}
FLOATING_INSTALLS: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Floating:
    name: str
    key: str
    reasons: tuple[str, ...]


def _words(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _split(words: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    target = False
    for word in words:
        if target:
            target = False
        elif word in CONTROL:
            commands.append([])
        elif word in REDIRECT:
            target = True
            if commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()
        else:
            commands[-1].append(word)
    return [command for command in commands if command]


def _assignments(words: list[str]) -> dict[str, str]:
    return {name: value for name, _, value in (word.partition("=") for word in words if ASSIGNMENT.fullmatch(word))}


def _shell_commands(text: str, env: dict[str, str]) -> Iterator[tuple[list[str], dict[str, str]]]:
    scoped = dict(env)
    for line in text.replace("\\\n", " ").splitlines():
        if not (INSTALL_WORD.search(line) or ASSIGNMENT_LINE.match(line)):
            continue
        for words in _split(_words(line)):
            assigned = words[1:] if words[0] == "export" else words
            if all(ASSIGNMENT.fullmatch(word) for word in assigned):
                scoped |= _assignments(assigned)
            else:
                yield words, dict(scoped)


def _yaml_runs(node: object, env: dict[str, str], step: str) -> Iterator[tuple[str, dict[str, str], str]]:
    if isinstance(node, list):
        for item in node:
            yield from _yaml_runs(item, env, step)
    elif isinstance(node, dict):
        variables = node.get("env")
        scoped = env | {str(k): str(v) for k, v in variables.items()} if isinstance(variables, dict) else env
        named = str(node.get("name") or step)
        if isinstance(node.get("run"), str):
            yield str(node["run"]), scoped, named
        for key, value in node.items():
            if key != "env":
                yield from _yaml_runs(value, scoped, named)


def _assigned(node: ast.AST) -> tuple[str, ast.expr] | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        return (target.id, node.value) if isinstance(target, ast.Name) else None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return node.target.id, node.value
    return None


@functools.cache
def _module_strings(text: str) -> dict[str, str]:
    strings: dict[str, str] = {}
    for statement in ast.parse(text).body:
        pair = _assigned(statement)
        if pair is not None:
            name, value = pair
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings[name] = value.value
    return strings


def _imported_strings(node: ast.ImportFrom, root: Path) -> dict[str, str]:
    if node.level or not node.module:
        return {}
    base = root.joinpath(*node.module.split("."))
    source = next((path for path in (base.with_suffix(".py"), base / "__init__.py") if path.is_file()), None)
    strings = _module_strings(source.read_text()) if source else {}
    return {alias.asname or alias.name: strings[alias.name] for alias in node.names if alias.name in strings}


def _python_name(node: ast.Name | ast.Starred, names: dict[str, str | list[str]]) -> list[str]:
    value = names.get(ast.unparse(node.value) if isinstance(node, ast.Starred) else node.id)
    if isinstance(value, list):
        return list(value)
    return [value] if isinstance(value, str) else [ast.unparse(node)]


def _python_words(node: ast.expr, names: dict[str, str | list[str]]) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Name, ast.Starred)):
        return _python_name(node, names)
    if isinstance(node, ast.Call) and ast.unparse(node.func) in {"str", "Path", "os.fspath"} and len(node.args) == 1:
        return _python_words(node.args[0], names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return ["/".join([*_python_words(node.left, names)[:1], *_python_words(node.right, names)[:1]])]
    return [ast.unparse(node)]


def _python_names(tree: ast.Module, root: Path) -> dict[str, str | list[str]]:
    names: dict[str, str | list[str]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            names |= _imported_strings(statement, root)
    for node in ast.walk(tree):
        pair = _assigned(node)
        if pair is None:
            continue
        name, value = pair
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            names[name] = value.value
        elif isinstance(value, (ast.List, ast.Tuple)):
            names[name] = [word for element in value.elts for word in _python_words(element, names)]
    return names


def _python_argvs(path: Path, root: Path) -> Iterator[list[str]]:
    """Every list or tuple display holding an installer literal, as argv words with constants resolved."""
    tree = ast.parse(path.read_text())
    names = _python_names(tree, root)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and any(
            isinstance(element, ast.Constant) and element.value in INSTALLER_WORDS for element in node.elts
        ):
            yield [word for element in node.elts for word in _python_words(element, names)]


def _commands(path: Path, root: Path) -> Iterator[tuple[str, list[str], dict[str, str]]]:
    if path.suffix == ".py":
        yield from (("", words, {}) for words in _python_argvs(path, root))
    elif path.suffix == ".sh":
        yield from (("", words, env) for words, env in _shell_commands(path.read_text(), {}))
    else:
        for run, env, step in _yaml_runs(yaml.load(path.read_text(), Loader=LOADER), {}, ""):
            yield from ((step, words, scoped) for words, scoped in _shell_commands(run, env))


def _run_targets(path: Path, root: Path) -> list[Path]:
    if path.suffix == ".py":
        return []
    text = path.read_text()
    shells = [base / match for match in SHELL_RUN.findall(text) for base in (root, path.parent)]
    modules = [root.joinpath(*module.split(".")) for module in MODULE_RUN.findall(text)]
    candidates = [
        *shells,
        *(root / match for match in FILE_RUN.findall(text)),
        *(module.with_suffix(".py") for module in modules),
        *(module / "__main__.py" for module in modules),
    ]
    return [target for target in candidates if ".." not in target.parts and target.is_file()]


def _ci_sources(root: Path) -> list[Path]:
    """Everything under .github and scripts/ci_*, then every shell script and python module their run lines name."""
    tops = [*(root / ".github").glob("*"), *(root / "scripts").glob("ci_*")]
    pending = [path for top in tops for path in (top.rglob("*") if top.is_dir() else [top]) if path.suffix in SOURCES]
    sources: set[Path] = set()
    while pending:
        path = pending.pop()
        if path.is_file() and path not in sources:
            sources.add(path)
            pending += _run_targets(path, root)
    return sorted(sources)


def _installer_at(names: list[str], index: int) -> tuple[str, int]:
    name, following = names[index], names[index + 1 : index + 3]
    if name in {"pip", "pip3"} and following[:1] == ["install"]:
        return ("uv pip install" if index and names[index - 1] == "uv" else "pip install"), index + 2
    if name == "uvx":
        return "uvx", index + 1
    if name == "uv" and following in (["tool", "run"], ["tool", "install"]):
        return f"uv tool {following[1]}", index + 3
    if name == "pipx" and following[:1] in (["install"], ["run"]):
        return f"pipx {following[0]}", index + 2
    return "", 0


def _installer(words: list[str]) -> tuple[str, list[str], dict[str, str]] | None:
    names = [word.rsplit("/", 1)[-1] for word in words]
    for index in range(len(names)):
        kind, start = _installer_at(names, index)
        if kind:
            return kind, words[start:], _assignments(words[:index])
    return None


def _expand(word: str, env: dict[str, str]) -> str:
    return ENV_REFERENCE.sub(lambda match: env.get(match[1] or match[2], match[0]), word)


def _arguments(kind: str, args: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    options: list[tuple[str, str]] = []
    targets: list[str] = []
    words = iter(args)
    for word in words:
        if word.startswith("-") and len(word) > 1:
            flag, equals, value = word.partition("=")
            options.append((flag, value if equals or flag not in VALUED else next(words, "")))
        elif kind in RUNNERS:
            return options, [] if any(flag == "--from" for flag, _ in options) else [word]
        else:
            targets.append(word)
    return options, targets


def _local(target: str) -> bool:
    return target.startswith((".", "$", "~")) or "/" in target or target.endswith((".whl", ".tar.gz", ".zip"))


def _exact(spec: str) -> bool:
    try:
        specifiers = list(Requirement(spec).specifier)
    except ValueError:
        return False
    return len(specifiers) == 1 and specifiers[0].operator in {"==", "==="} and "*" not in specifiers[0].version


def _checked_in(value: str, root: Path) -> bool:
    start = value.find(".github/")
    relative = value[start:]
    if start < 0 or (start and value[start - 1] != "/") or not (root / relative).is_file():
        return False
    if relative == CONSTRAINTS:
        return "--constraint test-requirements.lock" in (root / CONSTRAINTS).read_text().splitlines()
    return relative == LOCK or (relative.startswith(CI_TOOLS) and "/" not in relative.removeprefix(CI_TOOLS))


def _constrained(kind: str, options: list[tuple[str, str]], env: dict[str, str], root: Path) -> bool:
    values = [value for flag, value in options if flag in CONSTRAINT_FLAGS]
    values += [
        value
        for flag, pip_args in options
        if flag == "--pip-args"
        for inner, value in _arguments("pip install", _words(pip_args))[0]
        if inner in CONSTRAINT_FLAGS
    ]
    values += env.get("UV_CONSTRAINT" if kind.startswith("uv") else "PIP_CONSTRAINT", "").split()
    return any(_checked_in(value, root) for value in values)


def _problems(kind: str, args: list[str], env: dict[str, str], root: Path) -> tuple[str, ...]:
    options, targets = _arguments(kind, [_expand(arg, env) for arg in args])
    named = [target for target in targets if not _local(target)]
    named += [value for flag, value in options if flag in {"--with", "--from"}]
    floating = tuple(f"floating version {spec}" for spec in named if not _exact(spec))
    return floating if _constrained(kind, options, env, root) else (*floating, UNCONSTRAINED)


def _floating_ci_installs(root: Path) -> list[_Floating]:
    found: list[_Floating] = []
    for path in _ci_sources(root):
        relative = path.relative_to(root).as_posix()
        for step, words, env in _commands(path, root):
            install = _installer(words)
            if install is None:
                continue
            kind, args, prefix = install
            reasons = _problems(kind, args, env | prefix, root)
            if reasons:
                name = f"{relative}::{step}" if step else relative
                found.append(_Floating(name, f"{relative}: {kind} {shlex.join(args)}".rstrip(), reasons))
    return found


@functools.cache
def _repository() -> tuple[_Floating, ...]:
    return tuple(_floating_ci_installs(ROOT))


def _intentional(finding: _Floating) -> bool:
    return finding.name in INTENTIONAL_SMOKES and finding.reasons == (UNCONSTRAINED,)


def _job(run: str, env: dict[str, str] | None = None) -> str:
    return yaml.safe_dump({"jobs": {"plant": {"env": env or {}, "steps": [{"name": "Plant", "run": run}]}}})


def _composite(run: str) -> str:
    return yaml.safe_dump({"runs": {"using": "composite", "steps": [{"name": "Plant", "run": run, "shell": "bash"}]}})


def _plant(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.mark.feature("ci-tool-installs-pinned-walk")
def test_every_ci_tool_install_pins_an_exact_version_under_a_checked_in_constraint() -> None:
    found = [finding for finding in _repository() if not _intentional(finding)]
    reasons = {finding.key: "; ".join(finding.reasons) for finding in found}
    measured, listed = Counter(finding.key for finding in found), Counter(FLOATING_INSTALLS)
    problems = [
        f"floating CI tool install, pin it: {key} ({reasons[key]})" for key in sorted((measured - listed).elements())
    ]
    problems += [
        f"no longer floating, remove it from FLOATING_INSTALLS: {key}" for key in sorted((listed - measured).elements())
    ]
    listing = [f"    {key!r}," for key in sorted(measured.elements())]
    assert not problems, "\n".join([*problems, "Measured floating installs:", *listing])


@pytest.mark.feature("ci-tool-installs-pinned-walk")
@pytest.mark.parametrize(
    ("planted", "pinned"),
    [
        pytest.param(
            {".github/workflows/plant.yml": _job("python -m pip install 'pyyaml>=6.0'")},
            {".github/workflows/plant.yml": _job(f"python -m pip install --constraint {PLANT_LOCK} PyYAML==6.0.3")},
            id="workflow pip install",
        ),
        pytest.param(
            {".github/actions/plant/action.yml": _composite("uv pip install --system ruff==0.15.12")},
            {".github/actions/plant/action.yml": _composite(f"uv pip install --system -c {PLANT_LOCK} ruff==0.15.12")},
            id="composite action uv pip install",
        ),
        pytest.param(
            {"scripts/ci_plant.yml": yaml.safe_dump([{"name": "Plant", "run": "pip install build"}])},
            {"scripts/ci_plant.yml": yaml.safe_dump([{"name": "Plant", "run": f"pip install -r {LOCK} build==1.3.0"}])},
            id="step template pip install",
        ),
        pytest.param(
            {".github/scripts/plant.sh": "uvx pip-audit==2.10.1 --format json\n"},
            {".github/scripts/plant.sh": f"uvx --constraints {PLANT_LOCK} pip-audit==2.10.1 --format json\n"},
            id="github shell script uvx",
        ),
        pytest.param(
            {".github/workflows/plant.yml": _job("bash deploy/plant.sh"), "deploy/plant.sh": "uv tool install ruff\n"},
            {"deploy/plant.sh": f"UV_CONSTRAINT={PLANT_LOCK} uv tool install ruff==0.15.12\n"},
            id="shell script a step runs uv tool install",
        ),
        pytest.param(
            {
                "scripts/ci_plant.py": 'PIN = "platformdirs!=4.14.8"\nAUDIT = ["uvx", "--with", PIN, "pip-audit==2.10.1"]\n'
            },
            {
                "scripts/ci_plant.py": f'LOCKS = ["--constraints", "{PLANT_LOCK}"]\nAUDIT = ["uvx", *LOCKS, "pip-audit==2.10.1"]\n'
            },
            id="python ci script uvx",
        ),
        pytest.param(
            {".github/workflows/plant.yml": _job("pipx run build")},
            {".github/workflows/plant.yml": _job(f"pipx run --pip-args='--constraint {PLANT_LOCK}' build==1.3.0")},
            id="workflow pipx run",
        ),
        pytest.param(
            {
                ".github/workflows/plant.yml": _job("python -m tools.plant"),
                "tools/plant.py": 'ARGV = ("python", "-m", "pip", "install", "pyyaml")\n',
            },
            {
                "tools/plant.py": f'ARGV = ("python", "-m", "pip", "install", "--constraint", "{PLANT_LOCK}", "pyyaml==6.0.3")\n'
            },
            id="python module a step runs pip install",
        ),
    ],
)
def test_the_install_walker_reports_a_planted_floating_install(
    tmp_path: Path, planted: dict[str, str], pinned: dict[str, str]
) -> None:
    _plant(tmp_path, {LOCK: "", PLANT_LOCK: "", **planted})
    assert len(found := _floating_ci_installs(tmp_path)) == 1, found
    _plant(tmp_path, pinned)
    assert _floating_ci_installs(tmp_path) == []


@pytest.mark.feature("ci-tool-installs-pinned-walk")
@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {".github/workflows/plant.yml": _job("echo '::error::pin it on the pip install line' && exit 1")},
            id="echo text",
        ),
        pytest.param(
            {".github/scripts/plant.sh": "uv pip compile pyproject.toml --output-file requirements.txt\n"},
            id="uv pip compile",
        ),
        pytest.param(
            {
                ".github/workflows/plant.yml": _job(
                    'bash .github/scripts/pypi-retry.sh "Install" -- uv pip install --system'
                    f' --constraint "$RUNNER_TEMP/workflow/{LOCK}" mutmut==3.6.0 -e ".[dev,all]"'
                )
            },
            id="retry wrapper",
        ),
        pytest.param(
            {
                ".github/workflows/plant.yml": _job(
                    "uv pip install --system pytest==9.0.1", {"UV_CONSTRAINT": "${{ github.workspace }}/" + CONSTRAINTS}
                )
            },
            id="env chained constraint",
        ),
        pytest.param(
            {
                ".github/workflows/plant.yml": _job(
                    f'python -m pip install -c {PLANT_LOCK} "semgrep==$SEMGREP_VERSION"', {"SEMGREP_VERSION": "1.177.0"}
                )
            },
            id="env resolved version",
        ),
        pytest.param(
            {
                "scripts/ci_plant/venvs.py": (
                    "from scripts.ci_plant.paths import LOCKFILE\n\n\ndef build(uv, tree):\n"
                    "    constraints = ['--constraint', str(tree / LOCKFILE)]\n"
                    "    return [uv, 'pip', 'install', *constraints, '-r', str(tree / 'pyproject.toml')]\n"
                ),
                "scripts/ci_plant/paths.py": f"LOCKFILE = {LOCK!r}\n",
            },
            id="python argv constrained by an imported lock",
        ),
    ],
)
def test_the_install_walker_passes_pinned_and_non_install_commands(tmp_path: Path, files: dict[str, str]) -> None:
    _plant(tmp_path, {LOCK: "", PLANT_LOCK: "", CONSTRAINTS: "--constraint test-requirements.lock\n", **files})
    assert _floating_ci_installs(tmp_path) == []


@pytest.mark.feature("ci-tool-installs-pinned-walk")
def test_each_intentional_install_smoke_names_an_unconstrained_install_and_its_reason() -> None:
    assert {finding.name for finding in _repository() if _intentional(finding)} == set(INTENTIONAL_SMOKES)
    assert all(reason.strip() for reason in INTENTIONAL_SMOKES.values())
