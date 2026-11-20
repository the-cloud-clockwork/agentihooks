import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, cast

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = ".github/workflows/test.yml"
_KEYS = frozenset({"python-version", "PYTHON_VERSION"})
_VERSION = re.compile(r"(?<![\w.])3\.\d+(?:\.(?:\d+|x))?(?![\w.])")
_ONLY = re.compile(r"3\.12(?:\.(?:\d+|x))?")
_REFERENCE = re.compile(r"--python(?:-version)?[=\s]+\S+|uv python (?:install|pin)[^\n;&|]*")
_PIN = re.compile(r"\bpython(3\.\d+)|\bpython:(\S+)")
_SETUP = {"uses": "actions/setup-python@v6", "with": {"python-version": "3.13"}}
_RUN = f"{_TESTS} $.jobs.smoke.steps[0].run"


class _Setting(NamedTuple):
    where: str
    text: str
    pinned: bool


def _pinned(text: str, required: bool) -> bool:
    """True when every Python version the text names is 3.12 and, when required, the text names one."""
    versions = _VERSION.findall(text)
    return all(_ONLY.fullmatch(version) for version in versions) and (bool(versions) or not required)


def _walk(node: object, where: str) -> Iterator[tuple[str, object, object]]:
    if isinstance(node, dict):
        for key, value in cast(dict[object, object], node).items():
            yield f"{where}.{key}", key, value
            yield from _walk(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(cast(list[object], node)):
            yield from _walk(item, f"{where}[{index}]")


def _key_settings(where: str, value: object) -> Iterator[_Setting]:
    if isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            yield from _key_settings(f"{where}[{index}]", item)
    elif isinstance(value, dict):
        declared = cast(dict[object, object], value)
        if "default" in declared:
            yield from _key_settings(f"{where}.default", declared["default"])
    else:
        text = "" if value is None else str(value)
        yield _Setting(where, text, _pinned(text, "${{" not in text))


def _text_settings(where: str, text: str) -> Iterator[_Setting]:
    for match in _REFERENCE.finditer(text):
        yield _Setting(where, match.group(), _pinned(match.group(), False))
    for match in _PIN.finditer(text):
        yield _Setting(where, match.group(), _pinned(match.group(1) or match.group(2), True))


def _local(root: Path, uses: str) -> str:
    """Name the file a local uses reads.

    A checkout such as ./head or ./provenance-grader holds this repository's .github folder at the head or the base
    commit. A base copy passed this test in the merge queue when it merged, so the head copy stands for both.
    """
    relative = uses[uses.rfind(".github/") :] if ".github/" in uses else uses.removeprefix("./")
    if relative.endswith((".yml", ".yaml")):
        return relative
    return next(
        (f"{relative}/{name}" for name in ("action.yml", "action.yaml") if (root / relative / name).is_file()),
        f"{relative}/action.yml",
    )


def _settings(root: Path, relative: str, seen: set[str], trail: str = "") -> Iterator[_Setting]:
    seen.add(relative)
    for where, key, value in _walk(yaml.safe_load((root / relative).read_text()), f"{trail}{relative} $"):
        if key in _KEYS:
            yield from _key_settings(where, value)
        if isinstance(value, str):
            yield from _text_settings(where, value)
        if key == "uses" and isinstance(value, str) and value.startswith("./"):
            target = _local(root, value)
            if not (root / target).is_file():
                yield _Setting(where, f"{value} has no workflow or action file", False)
            elif target not in seen:
                yield from _settings(root, target, seen, f"{where} > ")


def _run(command: str) -> dict[str, object]:
    return {_TESTS: {"jobs": {"smoke": {"steps": [{"run": command}]}}}}


def test_every_tests_workflow_job_sets_python_3_12_only() -> None:
    settings = list(_settings(_ROOT, _TESTS, set()))
    offenders = [f"{setting.where}: {setting.text}" for setting in settings if not setting.pinned]
    assert any(" > " not in setting.where for setting in settings), "the Tests workflow sets no Python version"
    assert any(" > " in setting.where for setting in settings), "no file the Tests workflow uses sets a Python version"
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {
                _TESTS: {
                    "env": {"PYTHON_VERSION": "3.12"},
                    "jobs": {
                        "unit": {
                            "strategy": {
                                "matrix": {
                                    "python-version": ["3.12"],
                                    "exclude": [
                                        {"python-version": "${{ github.event_name == 'merge_group' && '3.12' || '' }}"}
                                    ],
                                }
                            },
                            "container": {"image": "mirror.gcr.io/library/python:3.12.12-slim-bookworm"},
                            "steps": [
                                {
                                    "uses": "actions/setup-python@v6",
                                    "with": {"python-version": "${{ matrix.python-version }}"},
                                },
                                {"run": 'uv venv --python "$PYTHON_PATH" venv && uv python install 3.12'},
                            ],
                        }
                    },
                }
            },
            [],
        ),
        (
            {_TESTS: {"jobs": {"unit": {"strategy": {"matrix": {"python-version": ["3.12", "3.13"]}}}}}},
            [f"{_TESTS} $.jobs.unit.strategy.matrix.python-version[1]"],
        ),
        (
            {
                _TESTS: {
                    "jobs": {
                        "unit": {
                            "strategy": {
                                "matrix": {
                                    "exclude": [
                                        {"python-version": "${{ github.event_name == 'merge_group' && '3.13' || '' }}"}
                                    ]
                                }
                            }
                        }
                    }
                }
            },
            [f"{_TESTS} $.jobs.unit.strategy.matrix.exclude[0].python-version"],
        ),
        ({_TESTS: {"jobs": {"lint": {"steps": [_SETUP]}}}}, [f"{_TESTS} $.jobs.lint.steps[0].with.python-version"]),
        (
            {
                _TESTS: {
                    "jobs": {"suite": {"uses": "./.github/workflows/suite.yml", "with": {"python-version": "3.13"}}}
                },
                ".github/workflows/suite.yml": {"jobs": {}},
            },
            [f"{_TESTS} $.jobs.suite.with.python-version"],
        ),
        (
            {
                _TESTS: {"jobs": {"suite": {"uses": "./.github/workflows/suite.yml"}}},
                ".github/workflows/suite.yml": {
                    "on": {"workflow_call": {"inputs": {"python-version": {"default": "3.13"}}}}
                },
            },
            [
                f"{_TESTS} $.jobs.suite.uses > .github/workflows/suite.yml $.on.workflow_call.inputs.python-version.default"
            ],
        ),
        ({_TESTS: {"env": {"PYTHON_VERSION": "3.13"}}}, [f"{_TESTS} $.env.PYTHON_VERSION"]),
        (_run("uv venv --python 3.13 venv"), [_RUN]),
        (_run("uv pip compile --python-version 3.13 pyproject.toml"), [_RUN]),
        (_run("uv python install 3.13"), [_RUN]),
        (_run("uv python pin 3.13"), [_RUN]),
        (_run("python3.13 -m pip --version"), [_RUN]),
        (
            {_TESTS: {"jobs": {"smoke": {"container": {"image": "python:3.13-slim"}}}}},
            [f"{_TESTS} $.jobs.smoke.container.image"],
        ),
        (
            {_TESTS: {"jobs": {"smoke": {"steps": [{"uses": "acme/tool@v1", "with": {"args": "--python 3.13"}}]}}}},
            [f"{_TESTS} $.jobs.smoke.steps[0].with.args"],
        ),
        (
            {
                _TESTS: {"jobs": {"cpu": {"steps": [{"uses": "./head/.github/actions/setup"}]}}},
                ".github/actions/setup/action.yml": {"runs": {"steps": [_SETUP]}},
            },
            [
                f"{_TESTS} $.jobs.cpu.steps[0].uses > .github/actions/setup/action.yml $.runs.steps[0].with.python-version"
            ],
        ),
        ({_TESTS: {"jobs": {"suite": {"uses": "./.github/workflows/gone.yml"}}}}, [f"{_TESTS} $.jobs.suite.uses"]),
    ],
)
def test_a_planted_python_other_than_3_12_is_named_by_where_it_is_set(
    files: dict[str, object], expected: list[str], tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    for relative, document in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(yaml.safe_dump(document))
    assert [setting.where for setting in _settings(root, _TESTS, set()) if not setting.pinned] == expected
