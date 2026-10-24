from pathlib import Path

from scripts.workflows import loader

PACKAGE_DIR = Path(loader.__file__).resolve().parents[2] / "profiles" / "package" / "workflows"


def workflow_text(name: str, intent: str) -> str:
    return (
        "workflow:\n"
        f"  name: {name}\n"
        f"  intent: {intent}\n"
        "  head: build\n"
        "  exit: build.pass\n"
        "stages:\n"
        "  build:\n"
        "    profile: engineer\n"
        "    next:\n"
        "      pass: exit\n"
    )


def write(directory: Path, filename: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(text)
    return path


def test_the_package_ships_simple_workflow():
    found, errors = loader.load([("package", PACKAGE_DIR)])

    assert errors == []
    simple = found["simple-workflow"]
    assert simple.layer == "package"
    assert simple.workflow.exit == "build-verdict.pass"
    assert simple.workflow.stages["scout"].count == 3


def test_a_project_layer_workflow_overrides_the_package_one_with_the_same_name(tmp_path):
    package = tmp_path / "package"
    project = tmp_path / "repo" / ".agentihooks" / "workflows"
    write(package, "simple.yaml", workflow_text("simple-workflow", "package intent"))
    write(project, "mine.yml", workflow_text("simple-workflow", "project intent"))

    found, errors = loader.load([("package", package), ("project", project)])

    assert errors == []
    assert found["simple-workflow"].layer == "project"
    assert found["simple-workflow"].workflow.intent == "project intent"
    assert found["simple-workflow"].path == project / "mine.yml"


def test_a_broken_file_is_reported_and_skipped(tmp_path):
    layer = tmp_path / "bundle"
    write(layer, "good.yaml", workflow_text("good", "fine"))
    bad = write(layer, "bad.yaml", workflow_text("bad", "fine").replace("profile:", "persona:"))
    write(layer, "notes.md", "not a workflow")

    found, errors = loader.load([("bundle", layer), ("profile:engineer", tmp_path / "missing")])

    assert list(found) == ["good"]
    assert len(errors) == 1
    assert errors[0].startswith(f"{bad}: ")
    assert "stages.build: unknown key persona" in errors[0]


def test_layer_dirs_list_package_bundle_profiles_then_project_in_order(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    role = tmp_path / "roles" / "engineer"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    calls = []

    def profile_dirs(bundle_arg, profile_csv, linked):
        calls.append((bundle_arg, profile_csv, linked))
        return [("engineer", role)]

    monkeypatch.setattr(loader.profile_chain, "bundle_path", lambda state: bundle)
    monkeypatch.setattr(loader.profile_chain, "active_profile", lambda state: "engineer")
    monkeypatch.setattr(loader.profile_chain, "linked_profiles", lambda state: {"x": tmp_path})
    monkeypatch.setattr(loader.profile_chain, "profile_dirs", profile_dirs)

    layers = loader.layer_dirs({}, repo / "sub")

    assert layers == [
        ("package", PACKAGE_DIR),
        ("bundle", bundle / "workflows"),
        ("profile:engineer", role / "workflows"),
        ("project", repo / ".agentihooks" / "workflows"),
    ]
    assert calls == [(bundle, "engineer", {"x": tmp_path})]


def test_layer_dirs_without_bundle_or_repo_hold_the_package_and_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(loader.profile_chain, "bundle_path", lambda state: None)
    monkeypatch.setattr(loader.profile_chain, "active_profile", lambda state: None)
    monkeypatch.setattr(loader.profile_chain, "linked_profiles", lambda state: {})
    monkeypatch.setattr(loader.profile_chain, "profile_dirs", lambda bundle, csv, linked: [])

    assert loader.layer_dirs({}, tmp_path) == [("package", PACKAGE_DIR)]
    assert loader.layer_dirs({}, None) == [("package", PACKAGE_DIR)]


def test_resolve_reads_the_installed_state_and_loads_its_layers(tmp_path, monkeypatch):
    project = tmp_path / "project"
    write(project, "a.yaml", workflow_text("a", "from project"))
    seen = []
    monkeypatch.setattr(loader.profile_chain, "read_state", lambda: {"state": 1})

    def layer_dirs(state, cwd):
        seen.append((state, cwd))
        return [("project", project)]

    monkeypatch.setattr(loader, "layer_dirs", layer_dirs)

    found, errors = loader.resolve(tmp_path)

    assert seen == [({"state": 1}, tmp_path)]
    assert found["a"].workflow.intent == "from project"
    assert errors == []
