import json
import re
from pathlib import Path

import pytest
import yaml

from tests.test_profile_render import world as render_world

world = render_world
SKILL = Path(__file__).resolve().parents[1] / "profiles/package/skills/read-docs"
SITE = "https://the-cloud-clockwork.github.io/agentihooks/"


@pytest.fixture
def lookup():
    from scripts import read_docs

    return read_docs


@pytest.fixture
def index():
    return {
        "0": {
            "doc": "Swarm",
            "title": "Watch gate",
            "content": "Refuses excessive watching.",
            "url": "/agentihooks/docs/pillars/swarm/#watch-gate",
        },
        "1": {
            "doc": "Swarm",
            "title": "Priorities",
            "content": "Operator decisions waiting for an answer.",
            "url": "/agentihooks/docs/pillars/swarm/#priorities",
        },
        "2": {
            "doc": "Swarm",
            "title": "Autonomy",
            "content": "Manual, assist, delegate and full govern who approves work.",
            "url": "/agentihooks/docs/pillars/swarm/#autonomy",
        },
        "3": {
            "doc": "Configuration",
            "title": "Brain",
            "content": "BRAIN_REFRESH_TOOL_CALLS controls refresh cadence.",
            "url": "/agentihooks/docs/reference/configuration.html#brain",
        },
        "4": {
            "doc": "Swarm",
            "title": "Table of contents",
            "content": "Watch gate Priorities Autonomy",
            "url": "/agentihooks/docs/pillars/swarm/#table-of-contents",
        },
    }


@pytest.mark.parametrize(
    "question,heading",
    [
        ("What does the watch gate mean?", "Watch gate"),
        ("What is the ledger Priorities section?", "Priorities"),
        ("How does the swarm autonomy setting work?", "Autonomy"),
        ("What does BRAIN_REFRESH_TOOL_CALLS mean?", "Brain"),
    ],
)
def test_question_routes_to_section(lookup, index, question, heading):
    result = lookup.candidates(index, question)
    assert result[0]["heading"] == heading
    assert result[0]["url"].startswith(SITE + "docs/")
    assert "text" not in result[0]
    assert all(r["heading"] != "Table of contents" for r in result)


def test_unknown_subject_has_no_candidates(lookup, index):
    assert lookup.candidates(index, "What does the hyperdrive swarm setting mean?") == []


def test_reads_only_selected_section(lookup, index):
    result = lookup.section(index, SITE + "docs/pillars/swarm/#priorities")
    assert result == {
        "page": "Swarm",
        "heading": "Priorities",
        "url": SITE + "docs/pillars/swarm/#priorities",
        "text": "Operator decisions waiting for an answer.",
    }


def test_skill_gate():
    _, front, body = (SKILL / "SKILL.md").read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == SKILL.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert 0 < len(meta["description"]) <= 1024
    assert not re.search(r"[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL / "evals/evals.json").read_text())
    assert len(evals) >= 3
    assert all(e["query"] and e["expected_behavior"] for e in evals)
    assert "follow up" in body and "never invent" in body.lower()


@pytest.mark.parametrize("role", ["master", "engineer", "cicd", "planner", "qa", "rb-other"])
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_every_role_gets_skill_and_instruction_in_any_project(world, monkeypatch, tmp_path, role, target):
    from scripts.profiles import render

    monkeypatch.chdir(tmp_path)
    out = render.render_claude(role) if target == "claude" else render.render_codex(role)
    assert (out / "skills/read-docs/SKILL.md").resolve() == SKILL / "SKILL.md"
    instruction = out / ("CLAUDE.md" if target == "claude" else "AGENTS.md")
    assert "read-docs" in instruction.read_text()


def test_candidates_rank_headings_then_page_names_and_limit_reading(lookup, index):
    index["5"] = {
        "doc": "Priorities guide",
        "title": "Overview",
        "content": "Priorities",
        "url": "/agentihooks/docs/a/",
    }
    index["6"] = {"doc": "Guide", "title": "Overview", "content": "Priorities", "url": "/agentihooks/docs/b/"}
    index["7"] = {"doc": "Guide", "title": "Overview", "content": "Priorities", "url": "/agentihooks/docs/c/"}
    result = lookup.candidates(index, "Priorities unknown")
    assert [r["url"] for r in result] == [SITE + "docs/pillars/swarm/#priorities", SITE + "docs/a/", SITE + "docs/b/"]
    assert all(r["missing_terms"] == ["unknown"] and r["matched_terms"] == ["priorities"] for r in result)


def test_excludes_external_pages_and_empty_sections(lookup, index):
    index["8"] = {"doc": "Watch", "title": "Watch", "content": "Watch", "url": "https://example.com/watch"}
    index["9"] = {"doc": "Watch", "title": "Watch", "content": "", "url": "/agentihooks/docs/empty/"}
    result = lookup.candidates(index, "watch")
    assert all(r["url"].startswith(SITE + "docs/") for r in result)
    assert lookup.section(index, "https://example.com/watch") is None
    assert lookup.section(index, SITE + "docs/empty/") is None
    assert lookup.section(index, SITE + "docs/absent/") is None


def test_loads_published_index_with_bounded_request(lookup, index, monkeypatch):
    from io import BytesIO

    calls = []

    def open_index(url, timeout):
        calls.append((url, timeout))
        return BytesIO(json.dumps(index).encode())

    monkeypatch.setattr(lookup, "urlopen", open_index)
    assert lookup.load_index() == index
    assert calls == [(SITE + "assets/js/search-data.json", 20)]


@pytest.mark.parametrize("bad", [[], {"x": []}, {"x": {"doc": "Swarm", "title": "Gate", "content": 1, "url": "/"}}])
def test_invalid_index_reports_schema(lookup, monkeypatch, bad):
    from io import BytesIO

    monkeypatch.setattr(lookup, "urlopen", lambda *a, **kw: BytesIO(json.dumps(bad).encode()))
    with pytest.raises(ValueError, match="Published docs index"):
        lookup.load_index()


@pytest.mark.parametrize(
    "argv,status,key",
    [
        (["--question", "watch"], "candidates", "candidates"),
        (["--section", SITE + "docs/pillars/swarm/#priorities"], "section", "section"),
        (["--question", "hyperdrive"], "missing", "next_step"),
        (["--section", SITE + "docs/absent/"], "missing", "next_step"),
    ],
)
def test_cli_returns_section_or_explicit_gap(lookup, index, monkeypatch, capsys, argv, status, key):
    monkeypatch.setattr(lookup, "load_index", lambda: index)
    assert lookup.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status
    assert result["source"] == SITE + "assets/js/search-data.json"
    assert key in result
    if status == "missing":
        assert "follow up" in result[key] and "never invent" in result[key]


def test_cli_unavailable_is_not_a_content_gap(lookup, monkeypatch, capsys):
    from urllib.error import URLError

    def fail():
        raise URLError("site unavailable")

    monkeypatch.setattr(lookup, "load_index", fail)
    assert lookup.main(["--question", "watch"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable"
    assert "site unavailable" in result["error"]
    assert "reachable" in result["next_step"]


def test_cli_uses_process_arguments(lookup, monkeypatch, index, capsys):
    import sys

    monkeypatch.setattr(lookup, "load_index", lambda: index)
    monkeypatch.setattr(sys, "argv", ["lookup.py", "--question", "Priorities"])
    assert lookup.main() == 0
    assert json.loads(capsys.readouterr().out)["candidates"][0]["heading"] == "Priorities"


@pytest.mark.parametrize("question", ["How does the swarm work?", "How does the swarm behave?", "What is the ledger?"])
def test_generic_subject_is_still_searchable(lookup, index, question):
    index["5"] = {
        "doc": "Swarm",
        "title": "Ledger",
        "content": "Swarm behavior.",
        "url": "/agentihooks/docs/swarm/#ledger",
    }
    assert lookup.candidates(index, question)[0]["page"] == "Swarm"
