from pathlib import Path

import pytest

from hooks import classifier
from hooks.context import conditions, profile_chain
from hooks.filters import schema
from hooks.filters.finders import string_literals

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NAME = "pre-edit+write+multiedit-ui_slop.filter.yaml"
PACKAGE = ROOT / "profiles/package/conditions" / NAME
PROJECT = ROOT / ".agentihooks/conditions/pre-edit+write+multiedit-ledger_ui.filter.yaml"
REAL_LAYERS = conditions.layer_dirs
INTENT = (
    "User-facing text states the value or state its label names. "
    "It never explains why, how it was computed, or adds detail nobody asked for."
)
TAIL = "reason += f\"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats\""
CLEAN = 'label = "changed 9m ago"'


@pytest.fixture
def shipped_filters(filters_dir, project_filters, monkeypatch):
    monkeypatch.setattr(conditions, "layer_dirs", REAL_LAYERS)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", ROOT / "profiles")
    monkeypatch.setattr(conditions, "repo_root", lambda cwd=None: ROOT)
    monkeypatch.setattr(conditions, "directory_trust", lambda root, state: (True, "test"))


def _edit(text, path="scripts/swarm/capacity.py", tool="Edit"):
    value = {"file_path": str(ROOT / path)}
    if tool == "Write":
        value["content"] = text
    elif tool == "MultiEdit":
        value["edits"] = [{"old_string": "", "new_string": text}]
    else:
        value.update(old_string="", new_string=text)
    return {"session_id": "ui-slop-replay", "tool_name": tool, "tool_input": value, "cwd": str(ROOT)}


def test_shipped_filters_have_the_requested_contract():
    for path in (PACKAGE, PROJECT):
        spec = schema.load(path)
        assert spec.intent == INTENT
        assert spec.mode == "both"
        assert spec.action == "send-back"
        assert [finder.script for finder in spec.finders] == ["string_literals", "explanation_tail"]


def test_capacity_tail_edit_is_sent_back(shipped_filters, stub):
    fake = stub()
    effect = conditions.pre_effect(_edit(TAIL))
    assert effect is not None and effect.block
    assert "; Claude has" in effect.block
    assert len(fake.calls) == 1
    assert fake.calls[0]["state"]["intent"] == INTENT


def test_changed_age_edit_passes(shipped_filters, stub):
    fake = stub(yes=False)
    effect = conditions.pre_effect(_edit(CLEAN))
    assert effect is not None and not effect.block
    assert len(fake.calls) == 1


@pytest.mark.parametrize("text", [TAIL, CLEAN])
def test_classifier_outage_passes_both_edits(shipped_filters, stub, text):
    fake = stub(error=classifier.ClassifierError("classifier down"))
    effect = conditions.pre_effect(_edit(text))
    assert effect is not None and not effect.block
    assert len(fake.calls) == 1


@pytest.mark.parametrize("suffix", ["html", "htm", "jsx", "tsx", "vue", "svelte"])
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_package_filter_catches_front_end_text(shipped_filters, stub, suffix, tool):
    fake = stub()
    effect = conditions.pre_effect(
        _edit('label = "value; because quota is available"', f"frontend/card.{suffix}", tool)
    )
    assert effect is not None and effect.block
    assert len(fake.calls) == 1


@pytest.mark.parametrize("folder", ["components", "pages", "ui"])
@pytest.mark.parametrize("suffix", ["js", "ts"])
def test_package_filter_covers_ui_script_folders(shipped_filters, stub, folder, suffix):
    fake = stub()
    effect = conditions.pre_effect(
        _edit('label = "value; because quota is available"', f"web/{folder}/nested/card.{suffix}")
    )
    assert effect is not None and effect.block
    assert len(fake.calls) == 1


@pytest.mark.parametrize("path", ["scripts/swarm_ledger/page.js", "scripts/swarm_ledger/static/page.ts"])
def test_project_filter_covers_ledger_producers(shipped_filters, stub, path):
    fake = stub()
    effect = conditions.pre_effect(_edit('label = "value; because quota is available"', path))
    assert effect is not None and effect.block
    assert len(fake.calls) == 1


@pytest.mark.parametrize("path", ["scripts/swarm/other.py", "web/server.js", "scripts/swarm_ledger_other/page.py"])
def test_unrelated_sources_pass_without_classifying(shipped_filters, stub, path):
    fake = stub()
    effect = conditions.pre_effect(_edit(TAIL, path))
    assert effect is not None and not effect.block
    assert fake.calls == []


@pytest.mark.parametrize("source", ["bundle", "profile", "runtime", "directory"])
def test_package_conditions_are_lowest_precedence(tmp_path, monkeypatch, source):
    monkeypatch.setattr(conditions, "layer_dirs", REAL_LAYERS)
    builtin = tmp_path / "profiles"
    package = builtin / "package/conditions"
    package.mkdir(parents=True)
    (package / NAME).write_text("mode: finders\n")
    bundle = tmp_path / "bundle"
    profile = bundle / "profiles/default"
    profile.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    directories = {
        "bundle": bundle / ".claude/conditions",
        "profile": profile / ".claude/conditions",
        "runtime": runtime,
        "directory": repo / ".agentihooks/conditions",
    }
    override = directories[source]
    override.mkdir(parents=True, exist_ok=True)
    (override / NAME).write_text("mode: classifier\n")
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", builtin)
    monkeypatch.setattr(conditions, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(conditions, "repo_root", lambda cwd=None: repo)
    state = {"bundle": {"path": str(bundle)}, "targets": {"global": {"claude": {"profile": "default"}}}}
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    layers, _ = conditions.layer_dirs(state, repo)
    assert layers == [
        ("package", package),
        ("bundle", directories["bundle"]),
        ("profile:default", directories["profile"]),
        ("runtime", runtime),
        ("directory", directories["directory"]),
    ]
    entries, invalid = conditions.scan_layers(layers)
    assert invalid == []
    assert len(entries) == 1
    assert entries[0]["path"] == str(override / NAME)


def test_htm_literals_are_found():
    text = '<span title="value; because quota is available">changed 9m ago</span>'
    findings = string_literals.find(text, "card.htm", "Edit")
    assert [item["text"] for item in findings] == ["value; because quota is available", "changed 9m ago"]
    assert all(text[item["start"] : item["end"]] == item["text"] for item in findings)


def test_old_condition_cache_discovers_the_package_filter(shipped_filters, tmp_path, monkeypatch):
    import json

    cache = tmp_path / "conditions-index.json"
    old_layers, probed = conditions.layer_dirs(profile_chain.read_state(), ROOT)
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "state": conditions._sig(profile_chain.state_path()),
                "probed": [[str(path), path.is_dir()] for path in probed],
                "dirs": [[str(path), conditions._sig(path)] for source, path in old_layers if source != "package"],
                "index": conditions.build_index([]),
            }
        )
    )
    monkeypatch.setattr(conditions, "_cache_path", lambda root=None: cache)
    entries = conditions.matching("pre", "Edit", {}, cwd=ROOT)
    assert any(entry["path"] == str(PACKAGE) for entry in entries)


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
@pytest.mark.parametrize(
    "text, context",
    [
        (TAIL, TAIL),
        (f"unrelated = 1\n{TAIL}\nunrelated = 2", TAIL),
        ('reason = """value;\nextra explanation"""\nother = 1', 'reason = """value;\nextra explanation"""'),
    ],
)
def test_finding_questions_include_the_enclosing_source(shipped_filters, stub, tool, text, context):
    fake = stub()
    conditions.pre_effect(_edit(text, tool=tool))
    questions = fake.calls[0]["questions"].values()
    assert any(f"Context: {context}" in question.instructions for question in questions)
    assert all(question.instructions.rsplit("\nContext: ", 1)[1] == context for question in questions)
