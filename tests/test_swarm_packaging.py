import tomllib
from pathlib import Path


def test_ledger_dependencies_exclude_workstation_tools():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    requirements = project["optional-dependencies"]["ledger"]
    names = {requirement.split(">=")[0] for requirement in requirements}
    assert {"jsonschema", "redis", "requests"} <= names
    assert not names & {"mcp", "playwright", "tree-sitter", "opentelemetry-sdk"}
    assert len(requirements) < len(project["dependencies"])
