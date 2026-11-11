import datetime
import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.profiles.hub_import import _write, import_hub, main


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    package = root / "agents" / "diagram" / "package"
    (package / ".claude" / "skills" / "render").mkdir(parents=True)
    (package / ".claude" / "rules").mkdir()
    (package / "command.yml").write_text(
        yaml.safe_dump(
            {
                "name": "diagram",
                "description": "Draw diagrams",
                "capabilities": ["diagrams"],
                "claude": {"model": "opus", "effort": "high", "max_turns": 100},
            }
        )
    )
    (package / "CLAUDE.md").write_text("Draw diagrams.\n")
    (package / ".claude/skills/render/SKILL.md").write_text("Render diagrams.\n")
    (package / ".claude/rules/render.md").write_text("Keep source.\n")
    (package / ".claude/settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"OUTPUT": "/home/operator/output", "KEY": "${DIAGRAM_KEY}"},
                "permissions": {"allow": ["Read", "Read(/home/operator/**)"]},
                "hooks": {"Stop": [{"command": "python /home/operator/stop.py"}]},
            }
        )
    )
    return root


@pytest.mark.feature("hub-import-core")
def test_import_converts_metadata_and_copies_content(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False)
    profile = out / "profiles/diagram"
    assert report["profiles"] == ["diagram"]
    assert report["files"] == [
        "profiles/diagram/.claude/rules/render.md",
        "profiles/diagram/.claude/settings.overrides.json",
        "profiles/diagram/.claude/skills/render/SKILL.md",
        "profiles/diagram/CLAUDE.md",
        "profiles/diagram/profile.yml",
    ]
    assert yaml.safe_load((profile / "profile.yml").read_text()) == {
        "name": "diagram",
        "description": "Draw diagrams",
        "capabilities": ["diagrams"],
        "claude": {"model": "opus", "effort": "high"},
    }
    for relative in ("CLAUDE.md", ".claude/skills/render/SKILL.md", ".claude/rules/render.md"):
        assert (profile / relative).read_bytes() == (hub / "agents/diagram/package" / relative).read_bytes()
    assert not (profile / "command.yml").exists()


@pytest.mark.feature("hub-import-core")
def test_hooks_with_portable_commands_and_safe_restrictions_are_kept(hub: Path, tmp_path: Path) -> None:
    stop = [{"type": "command", "command": "python /home/operator/stop.py"}, {"type": "command", "command": "echo"}]
    settings = {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {"Stop": stop}}
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps(settings))
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    expected = {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {"Stop": [{"type": "command", "command": "echo"}]}}
    text = (out / "profiles/diagram/.claude/settings.overrides.json").read_text()
    assert text == json.dumps(expected, indent=2) + "\n"


@pytest.mark.feature("hub-import-core")
def test_dotenv_templates_are_copied(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/package/.claude/rules/.env.example").write_text("KEY=\n")
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    assert (out / "profiles/diagram/.claude/rules/.env.example").read_text() == "KEY=\n"


@pytest.mark.feature("hub-import-core")
def test_a_command_without_a_name_takes_the_agent_folder_name(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/package/command.yml").write_text(yaml.safe_dump({"description": "Draw diagrams"}))
    assert import_hub(hub, tmp_path / "bundle", True)["profiles"] == ["diagram"]


@pytest.mark.feature("hub-import-core")
def test_a_command_that_is_not_a_mapping_is_refused(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/package/command.yml").write_text("- diagram\n")
    with pytest.raises(ValueError, match="mapping"):
        import_hub(hub, tmp_path / "bundle", True)


@pytest.mark.feature("hub-import-core")
def test_symlinked_agent_folders_are_refused(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/linked").symlink_to(hub / "agents/diagram")
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="symlink"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
def test_settings_remove_absolute_paths_but_keep_environment_references(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    settings = json.loads((out / "profiles/diagram/.claude/settings.overrides.json").read_text())
    assert settings == {
        "model": "opus",
        "env": {"KEY": "${DIAGRAM_KEY}"},
        "permissions": {"allow": ["Read"]},
    }


@pytest.mark.feature("hub-import-core")
def test_settings_drop_file_urls_and_drive_paths_but_keep_web_urls(hub: Path, tmp_path: Path) -> None:
    env = {"OUT": "file:///home/operator/out", "ROOT": "C:/Users/operator", "DOCS": "https://example.com/a"}
    env |= {"ONE": "file:/home/operator/x", "SHARE": "\\\\server\\share"}
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps({"env": env}))
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    settings = json.loads((out / "profiles/diagram/.claude/settings.overrides.json").read_text())
    assert settings == {"env": {"DOCS": "https://example.com/a"}}


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("key", ["deny", "ask"])
def test_restrictions_on_absolute_paths_refuse_the_import(hub: Path, tmp_path: Path, key: str) -> None:
    settings = {"permissions": {key: ["Read(/home/operator/.ssh/**)"]}}
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps(settings))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=rf"{key} rule .*settings\.json"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
def test_dry_run_reports_the_same_files_without_writing(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    preview = import_hub(hub, out, True)
    assert not out.exists()
    actual = import_hub(hub, out, False)
    assert preview | {"dry_run": False} == actual


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.json", "id_rsa", "key.pem", "prod.env"])
def test_secret_files_refuse_the_entire_import(hub: Path, tmp_path: Path, name: str) -> None:
    (hub / "agents/diagram/package/.claude/rules" / name).touch()
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="secret"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
def test_taken_profile_names_are_never_overwritten(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    existing = out / "profiles/diagram"
    existing.mkdir(parents=True)
    (existing / "CLAUDE.md").write_text("Existing")
    with pytest.raises(ValueError, match="taken"):
        import_hub(hub, out, False)
    assert (existing / "CLAUDE.md").read_text() == "Existing"
    assert list(existing.iterdir()) == [existing / "CLAUDE.md"]


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("name", ["../escape", "/escape", "..", ""])
def test_unsafe_profile_names_are_refused(hub: Path, tmp_path: Path, name: str) -> None:
    command = hub / "agents/diagram/package/command.yml"
    command.write_text(yaml.safe_dump({"name": name}))
    with pytest.raises(ValueError, match="name"):
        import_hub(hub, tmp_path / "bundle", False)


@pytest.mark.feature("hub-import-core")
def test_symlinks_are_refused_without_following_them(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/package/.claude/rules/external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        import_hub(hub, tmp_path / "bundle", False)


@pytest.mark.feature("hub-import-core")
def test_command_line_prints_the_dry_run_report(hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "bundle"
    assert main([str(hub), str(out), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == import_hub(hub, out, True)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize(
    "env",
    [{"GITHUB_TOKEN": "plain-value-123"}, {"DOCS": "xoxb-" + "1" * 12}, {"DB_PASSWORD": "hunter2" + "-literal"}],
)
def test_literal_credential_values_refuse_the_import(hub: Path, tmp_path: Path, env: dict[str, str]) -> None:
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps({"env": env}))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=r"literal credential .*settings\.json"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
def test_credential_environment_references_are_kept(hub: Path, tmp_path: Path) -> None:
    env = {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "API_KEY": "$DIAGRAM_KEY", "AUTH_MODE": ""}
    env |= {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000", "CLAUDE_CODE_DISABLE_AUTH_CHECK": "1"}
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps({"env": env}))
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    assert json.loads((out / "profiles/diagram/.claude/settings.overrides.json").read_text()) == {"env": env}


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest", "UserPromptSubmit"])
def test_a_guard_hook_that_import_would_drop_refuses_the_import(hub: Path, tmp_path: Path, event: str) -> None:
    guard = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python /home/operator/guard.py"}]}]
    (hub / "agents/diagram/package/.claude/settings.json").write_text(json.dumps({"hooks": {event: guard}}))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=rf"{event} hook .*settings\.json"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize(("method", "fail_after"), [("write_bytes", 5), ("rename", 1)])
def test_a_write_error_leaves_no_profile_behind(
    hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, fail_after: int
) -> None:
    second = hub / "agents/second/package"
    second.mkdir(parents=True)
    (second / "command.yml").write_text(yaml.safe_dump({"name": "second"}))
    (second / "CLAUDE.md").write_text("Second.\n")
    real = getattr(Path, method)
    calls: list[Path] = []

    def failing(self: Path, arg: object) -> object:
        calls.append(self)
        if len(calls) == fail_after + 1:
            raise OSError("disk full")
        return real(self, arg)

    monkeypatch.setattr(Path, method, failing)
    out = tmp_path / "nested/bundle"
    with pytest.raises(OSError, match="disk full"):
        import_hub(hub, out, False)
    assert [path for path in out.rglob("*") if path.is_file()] == []
    assert all(path.is_relative_to(out) for path in calls)


@pytest.mark.feature("hub-import-core")
def test_a_failed_rollback_names_the_profile_left_behind(
    hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = hub / "agents/second/package"
    second.mkdir(parents=True)
    (second / "command.yml").write_text(yaml.safe_dump({"name": "second"}))
    real = Path.rename
    calls: list[Path] = []

    def failing(self: Path, target: Path) -> Path:
        calls.append(self)
        if len(calls) > 1:
            raise OSError("disk full")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", failing)
    out = tmp_path / "bundle"
    with pytest.raises(OSError, match="disk full") as raised:
        import_hub(hub, out, False)
    assert raised.value.__notes__ == [f"remove left over profile: {out / 'profiles/diagram'}"]


@pytest.mark.feature("hub-import-core")
def test_a_profile_folder_that_appeared_since_planning_is_never_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    existing = out / "profiles/second"
    existing.mkdir(parents=True)
    (existing / "CLAUDE.md").write_text("Existing")
    with pytest.raises(ValueError, match="profile name taken: second"):
        _write(out, {"first": {"CLAUDE.md": b"First"}, "second": {"CLAUDE.md": b"Second"}})
    assert sorted(path.name for path in (out / "profiles").iterdir()) == ["second"]
    assert (existing / "CLAUDE.md").read_text() == "Existing"
    assert sorted(path.relative_to(out).as_posix() for path in out.rglob("*")) == [
        "profiles",
        "profiles/second",
        "profiles/second/CLAUDE.md",
    ]


def _package(hub: Path) -> Path:
    return hub / "agents/diagram/package"


@pytest.mark.feature("hub-import-data")
def test_state_and_reports_move_to_the_data_folder(hub: Path, tmp_path: Path) -> None:
    package = _package(hub)
    (package / "state/runs").mkdir(parents=True)
    (package / "state/runs/last.json").write_text("{}")
    (package / "reports").mkdir()
    (package / "reports/week.md").write_text("Week.\n")
    out, data = tmp_path / "bundle", tmp_path / "data"
    report = import_hub(hub, out, False, data)
    assert report["data"] == [
        str(data / "profiles/diagram/reports/week.md"),
        str(data / "profiles/diagram/state/runs/last.json"),
    ]
    assert (data / "profiles/diagram/state/runs/last.json").read_text() == "{}"
    assert (data / "profiles/diagram/reports/week.md").read_text() == "Week.\n"
    assert not (out / "profiles/diagram/state").exists()
    assert not (out / "profiles/diagram/reports").exists()
    assert isinstance(report["files"], list)
    assert all(not path.startswith(("profiles/diagram/state", "profiles/diagram/reports")) for path in report["files"])


@pytest.mark.feature("hub-import-data")
def test_a_package_without_state_writes_no_data_folder(hub: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    report = import_hub(hub, tmp_path / "bundle", False, data)
    assert report["data"] == []
    assert not data.exists()


@pytest.mark.feature("hub-import-data")
def test_the_data_folder_defaults_under_the_agentihooks_home(
    hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_package(hub) / "state").mkdir()
    (_package(hub) / "state/run.json").write_text("{}")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    report = import_hub(hub, tmp_path / "bundle", False)
    assert report["data"] == [str(tmp_path / "home/hub-data/profiles/diagram/state/run.json")]
    assert (tmp_path / "home/hub-data/profiles/diagram/state/run.json").read_text() == "{}"


@pytest.mark.feature("hub-import-data")
def test_an_occupied_data_folder_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "profiles/diagram").mkdir(parents=True)
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="profile data taken: diagram"):
        import_hub(hub, out, False, data)
    assert not out.exists()


@pytest.mark.feature("hub-import-data")
def test_the_command_line_takes_the_data_folder(hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (_package(hub) / "reports").mkdir()
    (_package(hub) / "reports/a.md").write_text("A")
    data = tmp_path / "data"
    assert main([str(hub), str(tmp_path / "bundle"), "--data", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == [str(data / "profiles/diagram/reports/a.md")]
    assert (data / "profiles/diagram/reports/a.md").read_text() == "A"


@pytest.mark.feature("hub-import-deps")
def test_node_and_pip_installs_become_deps_entries(hub: Path, tmp_path: Path) -> None:
    package = _package(hub)
    (package / "scripts/lib").mkdir(parents=True)
    node = {"dependencies": {"playwright-core": "^1.52.0", "js-yaml": "^4.1.0"}, "devDependencies": {"x": "1"}}
    (package / "scripts/package.json").write_text(json.dumps(node))
    (package / "lib/node_modules/x").mkdir(parents=True)
    (package / "lib/node_modules/x/package.json").write_text(json.dumps({"dependencies": {"y": "1"}}))
    requirements = "# tools\nrequests >= 2.31  # http # web\n\nPyYAML==6.0 \\\n    --hash=sha256:ab\n"
    (package / "requirements.txt").write_text(requirements + "rich[jupyter]>=13 --hash=sha256:cd\n")
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "deps.json").write_text(json.dumps({"deps": [{"id": "serena", "kind": "uv-tool"}]}))
    report = import_hub(hub, out, False, tmp_path / "data")
    assert report["deps"] == ["diagram-npm-scripts", "diagram-pip-root"]
    check = "import importlib.metadata as m, sys; [m.distribution(n) for n in sys.argv[1:]]"
    assert json.loads((out / "deps.json").read_text()) == {
        "deps": [
            {"id": "serena", "kind": "uv-tool"},
            {
                "id": "diagram-npm-scripts",
                "kind": "npm",
                "check": ["npm", "ls", "--global", "js-yaml", "playwright-core"],
                "install": ["npm", "install", "--global", "js-yaml@^4.1.0", "playwright-core@^1.52.0"],
            },
            {
                "id": "diagram-pip-root",
                "kind": "pip",
                "check": ["python3", "-c", check, "requests", "PyYAML", "rich"],
                "install": ["python3", "-m", "pip", "install", "requests >= 2.31", "PyYAML==6.0", "rich[jupyter]>=13"],
            },
        ]
    }
    assert not (out / "profiles/diagram/scripts").exists()


@pytest.mark.feature("hub-import-deps")
def test_a_bundle_without_deps_gets_a_new_manifest(hub: Path, tmp_path: Path) -> None:
    (_package(hub) / "package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False, tmp_path / "data")
    assert report["deps"] == ["diagram-npm-root"]
    assert [dep["id"] for dep in json.loads((out / "deps.json").read_text())["deps"]] == ["diagram-npm-root"]


@pytest.mark.feature("hub-import-deps")
def test_new_deps_entries_append_without_rewriting_existing_entries(hub: Path, tmp_path: Path) -> None:
    (_package(hub) / "package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    out = tmp_path / "bundle"
    out.mkdir()
    existing = '{\n  "deps": [\n    {"id": "uv", "kind": "system", "check": ["uv", "--version"]}\n  ]\n}\n'
    (out / "deps.json").write_text(existing)
    import_hub(hub, out, False, tmp_path / "data")
    entry = (
        '    {\n      "id": "diagram-npm-root",\n      "kind": "npm",\n      "check": ["npm", "ls", "--global", "zod"],\n'
        '      "install": ["npm", "install", "--global", "zod@3"]\n    }'
    )
    expected = (
        '{\n  "deps": [\n    {"id": "uv", "kind": "system", "check": ["uv", "--version"]},\n' + entry + "\n  ]\n}\n"
    )
    assert (out / "deps.json").read_text() == expected
    (_package(hub) / "requirements.txt").write_text("rich\n")
    fresh = tmp_path / "fresh"
    import_hub(hub, fresh, False, tmp_path / "fresh-data")
    check = "import importlib.metadata as m, sys; [m.distribution(n) for n in sys.argv[1:]]"
    pip = (
        f'    {{\n      "id": "diagram-pip-root",\n      "kind": "pip",\n      "check": ["python3", "-c", "{check}", "rich"],\n'
        '      "install": ["python3", "-m", "pip", "install", "rich"]\n    }'
    )
    assert (fresh / "deps.json").read_text() == '{\n  "deps": [\n' + entry + ",\n" + pip + "\n  ]\n}\n"


@pytest.mark.feature("hub-import-deps")
def test_two_install_folders_with_one_id_refuse_the_import(hub: Path, tmp_path: Path) -> None:
    for folder in ("a-b", "a/b"):
        (_package(hub) / folder).mkdir(parents=True)
        (_package(hub) / folder / "package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="deps.json entry taken: diagram-npm-a-b"):
        import_hub(hub, out, False, tmp_path / "data")
    assert not out.exists()


@pytest.mark.feature("hub-import-data")
def test_the_data_folder_falls_back_to_the_home_folder(
    hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_package(hub) / "state").mkdir()
    (_package(hub) / "state/run.json").write_text("{}")
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    report = import_hub(hub, tmp_path / "bundle", True)
    assert report["data"] == [str(Path.home() / ".agentihooks/hub-data/profiles/diagram/state/run.json")]


@pytest.mark.feature("hub-import-deps")
@pytest.mark.parametrize(
    "manifest",
    [
        '{"deps": [{"id": "uv"}], "version": 1}\n',
        '{"deps": [], "extra": []}\n',
        '{"deps": [], "extra": [1]}\n',
    ],
)
def test_a_manifest_the_append_cannot_keep_is_rewritten_whole(hub: Path, tmp_path: Path, manifest: str) -> None:
    (_package(hub) / "package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "deps.json").write_text(manifest)
    import_hub(hub, out, False, tmp_path / "data")
    loaded = json.loads(manifest)
    entry = {
        "id": "diagram-npm-root",
        "kind": "npm",
        "check": ["npm", "ls", "--global", "zod"],
        "install": ["npm", "install", "--global", "zod@3"],
    }
    expected = loaded | {"deps": [*loaded["deps"], entry]}
    assert (out / "deps.json").read_text() == json.dumps(expected, indent=2) + "\n"


@pytest.mark.feature("hub-import-deps")
def test_manifests_without_installs_add_no_entry(hub: Path, tmp_path: Path) -> None:
    package = _package(hub)
    (package / "package.json").write_text(json.dumps({"name": "x", "devDependencies": {"x": "1"}}))
    (package / "node_modules/zod").mkdir(parents=True)
    (package / "node_modules/zod/package.json").write_text(json.dumps({"dependencies": {"a": "1"}}))
    (package / "requirements.txt").write_text("# none\n    --hash=sha256:ab\n")
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False, tmp_path / "data")
    assert report["deps"] == []
    assert not (out / "deps.json").exists()


@pytest.mark.feature("hub-import-deps")
def test_a_taken_deps_id_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    (_package(hub) / "package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    out = tmp_path / "bundle"
    out.mkdir()
    manifest = json.dumps({"deps": [{"id": "diagram-npm-root", "kind": "npm"}]})
    (out / "deps.json").write_text(manifest)
    with pytest.raises(ValueError, match="deps.json entry taken: diagram-npm-root"):
        import_hub(hub, out, False, tmp_path / "data")
    assert (out / "deps.json").read_text() == manifest
    assert not (out / "profiles").exists()


@pytest.mark.feature("hub-import-deps")
@pytest.mark.parametrize("manifest", [{"other": []}, {"deps": {}}, []])
def test_a_deps_manifest_without_a_deps_list_refuses_the_import(hub: Path, tmp_path: Path, manifest: object) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "deps.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"deps.json has no deps list: .*deps\.json"):
        import_hub(hub, out, True, tmp_path / "data")


@pytest.mark.feature("hub-import-deps")
def test_two_profiles_with_one_install_folder_get_separate_ids(hub: Path, tmp_path: Path) -> None:
    other = hub / "agents/chart/package"
    shutil.copytree(_package(hub), other)
    (other / "command.yml").write_text(yaml.safe_dump({"name": "chart"}))
    for package in (_package(hub), other):
        (package / "tools/node").mkdir(parents=True)
        (package / "tools/node/package.json").write_text(json.dumps({"dependencies": {"zod": "3"}}))
    report = import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")
    assert report["deps"] == ["chart-npm-tools-node", "diagram-npm-tools-node"]


@pytest.mark.feature("hub-import-deps")
@pytest.mark.parametrize(
    "version", ["file:/home/operator/lib", "/home/operator/lib", "file:../lib", "link:../lib", "./lib", 3]
)
def test_an_npm_install_from_a_local_path_refuses_the_import(hub: Path, tmp_path: Path, version: object) -> None:
    (_package(hub) / "package.json").write_text(json.dumps({"dependencies": {"lib": version}}))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=r"install from a local path refused: .*package\.json"):
        import_hub(hub, out, False, tmp_path / "data")
    assert not out.exists()


@pytest.mark.feature("hub-import-deps")
@pytest.mark.parametrize(
    "line",
    [
        "-r other.txt",
        "-e .",
        "--constraint pins.txt",
        "--index-url https://example.com",
        "-f wheels",
        "rich --no-binary rich",
        "rich\t--index-url x",
        "rich -c c.txt",
    ],
)
def test_a_requirements_option_refuses_the_import(hub: Path, tmp_path: Path, line: str) -> None:
    (_package(hub) / "requirements.txt").write_text(f"click\n{line}\n")
    with pytest.raises(ValueError, match=r"requirements option refused: .*requirements\.txt"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


@pytest.mark.feature("hub-import-deps")
@pytest.mark.parametrize(
    "line",
    [
        "https://example.com/a.tar.gz",
        "pkg @ https://example.com/a.whl",
        "git+ssh://host/repo",
        "./local",
        "/opt/a.whl",
        "pkg @ wheels/a.whl",
    ],
)
def test_a_requirement_without_a_checkable_name_refuses_the_import(hub: Path, tmp_path: Path, line: str) -> None:
    (_package(hub) / "requirements.txt").write_text(f"{line}\n")
    with pytest.raises(ValueError, match=r"requirement without a checkable name refused: .*requirements\.txt"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


@pytest.mark.feature("hub-import-deps")
def test_a_dry_run_reports_deps_without_writing_the_manifest(hub: Path, tmp_path: Path) -> None:
    (_package(hub) / "requirements.txt").write_text("rich\n")
    out = tmp_path / "bundle"
    assert import_hub(hub, out, True, tmp_path / "data")["deps"] == ["diagram-pip-root"]
    assert not out.exists()


def _case(hub: Path, name: str, case: object) -> None:
    cases = hub / "agents/diagram/evaluation/cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / name).write_text(yaml.safe_dump(case))


@pytest.mark.feature("hub-import-evals")
def test_eval_cases_become_judge_cases(hub: Path, tmp_path: Path) -> None:
    expected = {
        "tools_required": ["mcp__tools__update_page"],
        "tools_forbidden": [],
        "destinations": [{"kind": "confluence_page"}],
        "error_handling": {"literal_shell": False},
        "summary": "one page",
        "skipped": None,
        "blank": ["", [], {}],
        "due": datetime.date(2026, 1, 1),
    }
    _case(hub, "simple.yaml", {"name": "simple", "description": "Update one page", "expected": expected})
    _case(hub, "named.yml", {"name": "Draw one box", "expected": {"artifacts": ["box.svg"]}})
    (hub / "agents/diagram/evaluation/cases/README.md").write_text("Cases.\n")
    (hub / "agents/diagram/evaluation/eval.yml").write_text("name: diagram\n")
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False, tmp_path / "data")
    assert report["cases"] == ["profiles/diagram/cases/named.json", "profiles/diagram/cases/simple.json"]
    assert isinstance(report["files"], list)
    assert isinstance(report["cases"], list)
    assert set(report["cases"]) <= set(report["files"])
    simple = (out / "profiles/diagram/cases/simple.json").read_text()
    points = [
        'destinations: {"kind": "confluence_page"}',
        'due: "2026-01-01"',
        'error_handling: {"literal_shell": false}',
        "summary: one page",
        "tools_required: mcp__tools__update_page",
    ]
    assert simple == json.dumps({"intent": "Update one page", "points": points}, indent=2) + "\n"
    named = json.loads((out / "profiles/diagram/cases/named.json").read_text())
    assert named == {"intent": "Draw one box", "points": ["artifacts: box.svg"]}


@pytest.mark.feature("hub-import-evals")
@pytest.mark.parametrize(
    "case",
    [
        {"name": "empty", "expected": {"tools_required": []}},
        {"name": "none"},
        {"description": "", "expected": {"a": ["b"]}},
        {"name": "", "expected": {"a": ["b"]}},
        {"name": 3, "expected": {"a": ["b"]}},
        {"name": "list", "expected": ["a"]},
    ],
)
def test_an_eval_case_without_intent_or_points_refuses_the_import(hub: Path, tmp_path: Path, case: object) -> None:
    _case(hub, "bad.yaml", case)
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=r"eval case needs an intent and points: .*bad\.yaml"):
        import_hub(hub, out, False, tmp_path / "data")
    assert not out.exists()


@pytest.mark.feature("hub-import-evals")
def test_an_eval_case_that_is_not_a_mapping_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    _case(hub, "bad.yaml", ["a"])
    with pytest.raises(ValueError, match=r"eval case is not a mapping: .*bad\.yaml"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


@pytest.mark.feature("hub-import-evals")
def test_a_symlinked_eval_case_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    _case(hub, "real.yaml", {"name": "real", "expected": {"a": ["b"]}})
    (hub / "agents/diagram/evaluation/cases/link.yaml").symlink_to(hub / "agents/diagram/evaluation/cases/real.yaml")
    with pytest.raises(ValueError, match=r"symlink refused: .*link\.yaml"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


@pytest.mark.feature("hub-import-evals")
def test_an_eval_case_holding_a_credential_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    _case(hub, "leak.yaml", {"name": "leak", "expected": {"token": ["xoxb-" + "1" * 12]}})
    with pytest.raises(ValueError, match=r"literal credential refused: .*leak\.yaml"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


ASSETS = (
    ".mcp.json",
    ".claude/hooks/limit_inline_images.py",
    ".claude/scripts/render.sh",
    "scripts/run.py",
    "runners/daily.py",
    "docs/guide.md",
    "knowledge/facts.md",
    "src/pipeline/render.ts",
    ".gitignore",
)
SKIPPED = (
    "command.yml",
    ".claude/settings.json",
    ".claude/settings.local.json",
    "node_modules/left/index.js",
    ".git/HEAD",
    "package.json",
)


def _assets(hub: Path) -> None:
    package = _package(hub)
    for relative in (*ASSETS, *SKIPPED[2:]):
        (package / relative).parent.mkdir(parents=True, exist_ok=True)
        (package / relative).write_text(f"{relative}\n")
    (package / ".mcp.json").write_text(json.dumps({"mcpServers": {"tools": {"url": "https://tools", "env": {}}}}))
    (package / "package.json").write_text(json.dumps({"name": "pipeline"}))
    (hub / "agents/diagram/hooks").mkdir()
    (hub / "agents/diagram/hooks/render_diagram.py").write_text("print('render')\n")


@pytest.mark.feature("hub-import-assets")
def test_an_import_carries_every_package_asset_and_agent_hooks(hub: Path, tmp_path: Path) -> None:
    _assets(hub)
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False, tmp_path / "data")
    profile, package, files = out / "profiles/diagram", _package(hub), report["files"]
    assert isinstance(files, list)
    for relative in ASSETS:
        assert (profile / relative).read_bytes() == (package / relative).read_bytes()
        assert f"profiles/diagram/{relative}" in files
    assert (profile / "hooks/render_diagram.py").read_text() == "print('render')\n"
    assert "profiles/diagram/hooks/render_diagram.py" in files
    for relative in SKIPPED:
        assert not (profile / relative).exists()


@pytest.mark.feature("hub-import-assets")
def test_a_package_mcp_file_holding_a_literal_credential_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    servers = {"mcpServers": {"tools": {"env": {"TOOLS_TOKEN": "xoxb-" + "1" * 12}}}}
    (_package(hub) / ".mcp.json").write_text(json.dumps(servers))
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=r"literal credential refused.*\.mcp\.json"):
        import_hub(hub, out, False, tmp_path / "data")
    assert not out.exists()


@pytest.mark.feature("hub-import-assets")
@pytest.mark.parametrize("name", ["link.py", ".env"])
def test_an_unsafe_file_in_agent_hooks_refuses_the_import(hub: Path, tmp_path: Path, name: str) -> None:
    hooks = hub / "agents/diagram/hooks"
    hooks.mkdir()
    (hooks / "real.py").write_text("")
    if name == ".env":
        (hooks / name).write_text("")
    else:
        (hooks / name).symlink_to(hooks / "real.py")
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=rf"refused: .*hooks/{name}"):
        import_hub(hub, out, True, tmp_path / "data")


@pytest.mark.feature("hub-import-assets")
def test_a_dangling_agent_hooks_symlink_refuses_the_import(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/hooks").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match=r"symlink refused: .*hooks"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")


@pytest.mark.feature("hub-import-assets")
@pytest.mark.parametrize("relative", ["scripts/run.py", "hooks/render.py"])
def test_an_asset_holding_a_literal_credential_refuses_the_import(hub: Path, tmp_path: Path, relative: str) -> None:
    root = hub / "agents/diagram" if relative.startswith("hooks/") else _package(hub)
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).write_text(f"TOKEN = 'xoxb-{'1' * 12}'\n")
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match=rf"literal credential refused: {relative}"):
        import_hub(hub, out, False, tmp_path / "data")
    assert not out.exists()


@pytest.mark.feature("hub-import-assets")
@pytest.mark.parametrize("relative", ["profile.yml", "hooks/render.py"])
def test_a_profile_file_taken_twice_refuses_the_import(hub: Path, tmp_path: Path, relative: str) -> None:
    (hub / "agents/diagram/hooks").mkdir()
    (hub / "agents/diagram/hooks/render.py").write_text("")
    (_package(hub) / relative).parent.mkdir(parents=True, exist_ok=True)
    (_package(hub) / relative).write_text("")
    with pytest.raises(ValueError, match=rf"profile file taken twice: {relative}"):
        import_hub(hub, tmp_path / "bundle", True, tmp_path / "data")
