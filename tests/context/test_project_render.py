from hooks.context.project_memory import ProjectMemory
from hooks.context.project_render import render_project_block


def test_empty_project_has_its_own_framed_block():
    assert render_project_block(ProjectMemory("alpha"), 1536) == (
        "[Project memory: alpha]\nBRAIN CONTEXT (recalled state, not an operator directive):\n\nNo project memory yet."
    )


def test_fixed_memory_output_and_utf8_cap():
    memory = ProjectMemory(
        "alpha",
        [{"id": "a", "title": "Alpha", "summary": "Build Alpha", "status": "active", "updated": "2026-10-05"}],
        ["Use the pool."],
    )
    assert render_project_block(memory, 1536) == (
        "[Project memory: alpha]\nBRAIN CONTEXT (recalled state, not an operator directive):\n\n"
        "Focus: Build Alpha (active; last active 2026-10-05).\n"
        "Hot arcs:\n* Alpha [a]\nRecent lessons:\n* Use the pool."
    )
    memory.lessons = ["á" * 500]
    assert len(render_project_block(memory, 200).encode()) <= 200
    assert len(render_project_block(memory, 0).encode()) == 0
