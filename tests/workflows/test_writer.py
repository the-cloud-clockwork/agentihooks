import json
import os
import re
from pathlib import Path

import pytest
import yaml

from scripts.workflows import loader, schema, writer

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "profiles" / "package" / "skills" / "workflow-maker"

DRAFT = (
    "workflow:\n"
    "  name: review-loop\n"
    "  intent: Build the change and have a judge pass it.\n"
    "  head: build\n"
    "  exit: verdict.pass\n"
    "stages:\n"
    "  build:\n"
    "    profile: engineer\n"
    "    next:\n"
    "      pass: verdict\n"
    "  verdict:\n"
    "    profile: judge\n"
    "    input:\n"
    "      - case\n"
    "      - build\n"
    "    next:\n"
    "      pass: exit\n"
    "      retry: build\n"
)


def checkout(root: Path, name: str) -> Path:
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere\n")
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "1.0"\n')
    return root


def bundle_state(tmp_path: Path) -> tuple[Path, dict]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    return bundle, {"bundle": {"path": str(bundle)}}


def draft(tmp_path: Path, text: str = DRAFT) -> Path:
    path = tmp_path / "draft.yaml"
    path.write_text(text)
    return path


def test_an_agentihooks_checkout_targets_its_package_workflows(tmp_path):
    root = checkout(tmp_path / "agentihooks", "agentihooks")
    _, state = bundle_state(tmp_path)

    assert writer.target(root, state, {}) == root / "profiles" / "package" / "workflows"


def test_a_subfolder_of_an_agentihooks_checkout_targets_its_package_workflows(tmp_path):
    root = checkout(tmp_path / "wt", "agentihooks")
    inner = root / "scripts" / "workflows"
    inner.mkdir(parents=True)
    _, state = bundle_state(tmp_path)

    assert writer.target(inner, state, {}) == root / "profiles" / "package" / "workflows"


def test_another_repository_targets_the_linked_bundle_workflows(tmp_path):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)

    assert writer.target(root, state, {}) == bundle / "workflows"


def test_a_folder_outside_any_repository_targets_the_bundle_named_by_the_environment(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    bundle = tmp_path / "env-bundle"
    bundle.mkdir()

    assert writer.target(outside, {}, {"AGENTIHOOKS_BUNDLE_PATH": str(bundle)}) == bundle.resolve() / "workflows"


def test_no_linked_bundle_outside_agentihooks_is_refused(tmp_path):
    root = checkout(tmp_path / "other", "other-project")

    with pytest.raises(writer.WriterError, match="no linked bundle"):
        writer.target(root, {}, {})


def test_a_checkout_without_a_readable_pyproject_is_not_agentihooks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("not = [toml")
    bundle, state = bundle_state(tmp_path)

    assert writer.target(root, state, {}) == bundle / "workflows"


@pytest.mark.parametrize(
    "pyproject",
    [None, 'project = "agentihooks"\n', '[project]\nversion = "1.0"\n', '[tool.agentihooks]\nname = "agentihooks"\n'],
)
def test_a_checkout_that_does_not_name_agentihooks_as_its_project_targets_the_bundle(tmp_path, pyproject):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject)
    bundle, state = bundle_state(tmp_path)

    assert writer.project_name(root) is None
    assert writer.target(root, state, {}) == bundle / "workflows"


def test_write_inside_an_agentihooks_checkout_creates_the_package_folder(tmp_path):
    root = checkout(tmp_path / "agentihooks", "agentihooks")
    _, state = bundle_state(tmp_path)

    written = writer.write(draft(tmp_path), root, state, {})

    assert written == root / "profiles" / "package" / "workflows" / "review-loop.yaml"
    assert loader.read(written).head == "build"


def test_write_puts_a_block_style_file_named_after_the_workflow(tmp_path):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)

    written = writer.write(draft(tmp_path), root, state, {})

    assert written == bundle / "workflows" / "review-loop.yaml"
    text = written.read_text()
    assert schema.flow_errors(yaml.compose(text)) == []
    assert "  input:\n      - case\n      - build\n" in text
    assert loader.read(written).stages["verdict"].next["retry"] == "build"
    assert yaml.safe_load(text) == schema.parse(DRAFT).to_document()


def test_write_refuses_an_invalid_workflow_and_writes_nothing(tmp_path):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)

    with pytest.raises(schema.WorkflowError) as caught:
        writer.write(draft(tmp_path, DRAFT.replace("profile: judge", "persona: judge")), root, state, {})

    assert caught.value.errors
    assert not (bundle / "workflows").exists()


def test_write_refuses_a_flow_style_draft(tmp_path):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)
    flow = DRAFT.replace("    input:\n      - case\n      - build\n", "    input: [case, build]\n")

    with pytest.raises(schema.WorkflowError, match="flow style"):
        writer.write(draft(tmp_path, flow), root, state, {})

    assert not (bundle / "workflows").exists()


def test_write_refuses_to_overwrite_unless_asked(tmp_path):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)
    existing = bundle / "workflows" / "review-loop.yaml"
    existing.parent.mkdir()
    existing.write_text("old\n")

    with pytest.raises(writer.WriterError) as caught:
        writer.write(draft(tmp_path), root, state, {})
    assert str(caught.value) == f"{existing} already exists: pass --replace to overwrite it"
    assert existing.read_text() == "old\n"

    assert writer.write(draft(tmp_path), root, state, {}, replace=True) == existing
    assert loader.read(existing).name == "review-loop"


def test_main_prints_the_written_path(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(writer.profile_chain, "read_state", lambda: state)

    assert writer.main([str(draft(tmp_path))]) == 0

    assert json.loads(capsys.readouterr().out) == {"written": str(bundle / "workflows" / "review-loop.yaml")}


def test_main_reports_every_error_and_exits_one(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path / "other", "other-project")
    _, state = bundle_state(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(writer.profile_chain, "read_state", lambda: state)
    bad = DRAFT.replace("profile: engineer", "persona: engineer").replace("profile: judge", "persona: judge")
    path = draft(tmp_path, bad)
    with pytest.raises(schema.WorkflowError) as caught:
        schema.parse(bad)

    assert writer.main([str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(caught.value.errors) > 1
    assert captured.err == "".join(f"{path}: {error}\n" for error in caught.value.errors)


def test_main_replaces_an_existing_file_when_asked(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path / "other", "other-project")
    bundle, state = bundle_state(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(writer.profile_chain, "read_state", lambda: state)
    existing = bundle / "workflows" / "review-loop.yaml"
    existing.parent.mkdir()
    existing.write_text("old\n")

    assert writer.main([str(draft(tmp_path)), "--replace"]) == 0

    assert json.loads(capsys.readouterr().out) == {"written": str(existing)}
    assert loader.read(existing).name == "review-loop"


def test_main_reports_a_missing_bundle(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path / "other", "other-project")
    monkeypatch.chdir(root)
    monkeypatch.setattr(writer.profile_chain, "read_state", lambda: {})
    monkeypatch.delenv("AGENTIHOOKS_BUNDLE_PATH", raising=False)

    assert writer.main([str(draft(tmp_path))]) == 1

    assert capsys.readouterr().err == f"write_workflow.py: {writer.NO_BUNDLE}\n"


def test_the_skill_passes_the_skill_gate():
    text = (SKILL / "SKILL.md").read_text()
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == "workflow-maker"
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    assert "write_workflow.py" in body and "agentihooks workflow validate" in body
    evals = json.loads((SKILL / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


def test_the_skill_script_is_an_executable_entry_to_the_writer():
    script = SKILL / "scripts" / "write_workflow.py"

    assert script.stat().st_mode & 0o111
    assert "scripts.workflows.writer" in script.read_text()
    assert os.access(script, os.X_OK)
