import json

import pytest

from scripts.plans.normalize import PlanError, normalize


def test_a_json_object_with_a_title_becomes_one_phase_per_entry():
    data = json.dumps(
        {
            "title": "From JSON",
            "phases": [
                {"title": "Alpha", "description": "First."},
                {"title": "Beta", "description": "Second.", "depends_on": [1], "planning": "manual", "id": "beta-id"},
            ],
        }
    )

    result = normalize(data, "plan.json")

    assert result.kind == "json"
    assert result.title == "From JSON"
    assert result.content == {
        "title": "From JSON",
        "phases": [
            {"title": "Alpha", "description": "First.", "planning": "auto"},
            {"title": "Beta", "description": "Second.", "planning": "manual", "depends_on": [1], "id": "beta-id"},
        ],
    }
    assert result.slices == {"Alpha": [], "Beta": []}
    assert "# From JSON" in result.markdown
    assert "## Alpha" in result.markdown
    assert "## Beta" in result.markdown


def test_a_bare_json_list_takes_its_title_from_the_file_stem():
    data = json.dumps([{"title": "Only phase", "description": "Just one."}])

    result = normalize(data, "my-plan.json")

    assert result.kind == "json"
    assert result.title == "my-plan"
    assert result.content == {
        "title": "my-plan",
        "phases": [{"title": "Only phase", "description": "Just one.", "planning": "auto"}],
    }


def test_bytes_input_is_decoded_as_utf8():
    data = json.dumps({"phases": [{"title": "Only"}]}).encode("utf-8")

    result = normalize(data, "bytes.json")

    assert result.kind == "json"
    assert result.content["phases"][0]["title"] == "Only"


def test_json_markdown_renders_the_full_marker_line_when_every_field_is_set():
    data = json.dumps(
        {
            "title": "Rendered",
            "phases": [
                {"title": "Alpha", "description": "First.", "id": "alpha-id"},
                {
                    "title": "Beta",
                    "description": "Second.",
                    "depends_on": ["alpha-id"],
                    "planning": "manual",
                    "id": "beta-id",
                    "not_duplicate": "it ships a different seam",
                },
            ],
        }
    )

    result = normalize(data, "plan.json")

    beta_marker = (
        '<!-- phase: id=beta-id depends=alpha-id planning=manual not_duplicate="it ships a different seam" -->'
    )
    assert beta_marker in result.markdown
    assert result.content["phases"][1] == {
        "title": "Beta",
        "description": "Second.",
        "planning": "manual",
        "depends_on": ["alpha-id"],
        "id": "beta-id",
        "not_duplicate": "it ships a different seam",
    }


def test_a_json_phase_entry_that_is_not_an_object_is_refused():
    data = json.dumps({"phases": ["not an object"]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "must be an object" in str(caught.value)


def test_a_json_phase_with_a_blank_title_is_refused():
    data = json.dumps({"phases": [{"title": "", "description": "x"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "needs a title" in str(caught.value)


def test_a_json_phase_with_a_non_string_description_is_refused():
    data = json.dumps({"phases": [{"title": "A", "description": 5}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "description must be a string" in str(caught.value)


def test_a_json_phase_with_a_bad_planning_value_is_refused():
    data = json.dumps({"phases": [{"title": "A", "planning": "later"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "planning must be manual or auto" in str(caught.value)


def test_a_json_phase_with_a_non_list_depends_on_is_refused():
    data = json.dumps({"phases": [{"title": "A", "depends_on": "oops"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "depends_on must be a list" in str(caught.value)


def test_a_json_integer_dependency_outside_the_plan_is_refused():
    data = json.dumps({"phases": [{"title": "A", "depends_on": [5]}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "outside the plan" in str(caught.value)


def test_a_json_phase_with_a_non_string_id_is_refused():
    data = json.dumps({"phases": [{"title": "A", "id": 5}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "id must be a string" in str(caught.value)


def test_a_json_phase_with_a_blank_not_duplicate_is_refused():
    data = json.dumps({"phases": [{"title": "A", "not_duplicate": "  "}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert "why the phase differs" in str(caught.value)


MARKED = """# Tiny plan

## Context

Some narrative that is not a phase.

## First phase
<!-- phase: id=pin depends=wrk planning=auto -->
Intro prose before the example.

```markdown
## Phase title
<!-- phase: id=pin depends=wrk,p59 planning=auto not_duplicate="why it differs" -->
Phase prose.
<!-- slice: pin1 -->
### Task title
```

More prose after the example.

<!-- slice: pin1 -->
### pin1 - First slice

Body of the first slice.

<!-- slice: pin2 -->
### pin2 - Second slice

Body of the second slice.

## Second phase
<!-- phase: id=wrk planning=manual -->
This phase is prose only, with no slices at all. It should still parse cleanly
and report zero slice names.
"""


def test_marked_markdown_gives_one_phase_per_marker_with_slices_in_order_and_a_prose_only_phase():
    result = normalize(MARKED, "plan.md")

    assert result.kind == "marked"
    assert result.title == "Tiny plan"
    assert result.markdown == MARKED
    assert result.slices == {"First phase": ["pin1", "pin2"], "Second phase": []}
    titles = {phase["title"]: phase for phase in result.content["phases"]}
    assert titles["First phase"]["id"] == "pin"
    assert titles["First phase"]["depends_on"] == ["wrk"]
    assert titles["First phase"]["planning"] == "auto"
    assert "not_duplicate" not in titles["First phase"]
    assert "Intro prose before the example." in titles["First phase"]["description"]
    assert "More prose after the example." in titles["First phase"]["description"]
    assert "### Task title" not in titles["First phase"]["description"]
    assert "<!-- phase:" not in titles["First phase"]["description"]
    assert titles["Second phase"]["id"] == "wrk"
    assert titles["Second phase"]["planning"] == "manual"
    assert "depends_on" not in titles["Second phase"]
    assert "prose only" in titles["Second phase"]["description"]


def test_a_real_not_duplicate_marker_is_stored_on_the_phase():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=auto not_duplicate="it covers a different seam" -->
Prose.
"""

    result = normalize(text, "nd.md")

    assert result.content["phases"][0]["not_duplicate"] == "it covers a different seam"


def test_marked_markdown_without_a_level_one_heading_falls_back_to_the_file_stem():
    text = """## Phase one
<!-- phase: id=a planning=auto -->
Prose.
"""

    result = normalize(text, "no-title.md")

    assert result.kind == "marked"
    assert result.title == "no-title"


def test_a_heading_with_nothing_after_it_at_end_of_file_is_not_treated_as_a_phase():
    text = "Just a paragraph.\n\n## Trailing heading"

    result = normalize(text, "trailing.md")

    assert result.kind == "raw"


def test_a_long_phase_description_is_bound_to_about_800_characters_on_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(400))
    text = f"""# Plan

## Phase one
<!-- phase: id=a planning=auto -->
{words}
"""

    result = normalize(text, "long.md")
    description = result.content["phases"][0]["description"]

    assert len(description) <= 800
    assert not description.endswith(" ")
    assert words.startswith(description)


def test_a_phase_description_exactly_at_the_bound_is_kept_whole():
    exact = ("lorem ipsum " * 100)[:800]
    text = f"""# Plan

## Phase one
<!-- phase: id=a planning=auto -->
{exact}
"""

    result = normalize(text, "exact.md")

    assert result.content["phases"][0]["description"] == exact


RAW = """# Operational note

## Context

Something happened that needs fixing.

## Steps

1. Do this.
2. Then that.
"""


def test_raw_plan_mode_markdown_with_no_markers_becomes_one_unsliced_phase():
    result = normalize(RAW, "incident.md")

    assert result.kind == "raw"
    assert result.title == "Operational note"
    assert result.markdown == RAW
    assert result.content == {
        "title": "Operational note",
        "phases": [
            {
                "title": "Operational note",
                "description": "Something happened that needs fixing.",
                "planning": "auto",
            }
        ],
    }
    assert result.slices == {"Operational note": []}


def test_raw_markdown_without_a_title_heading_falls_back_to_the_file_stem():
    result = normalize("Just a sentence with no headings at all.", "untitled-note.md")

    assert result.kind == "raw"
    assert result.title == "untitled-note"


def test_empty_input_is_refused():
    with pytest.raises(PlanError) as caught:
        normalize("   \n\n", "empty.md")

    assert "no content" in str(caught.value)


def test_duplicate_phase_titles_are_refused():
    text = """# Plan

## Same title
<!-- phase: id=a planning=auto -->
Prose A.

## Same title
<!-- phase: id=b planning=auto -->
Prose B.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "dup.md")

    assert "Same title" in str(caught.value)


def test_phase_headings_at_mixed_levels_are_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=auto -->
Prose.

### Phase two
<!-- phase: id=b planning=auto -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "mixed.md")

    assert "one heading level" in str(caught.value)


def test_a_marker_with_an_unknown_key_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a bogus=1 planning=auto -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "unknown-key.md")

    assert "unknown key bogus" in str(caught.value)


def test_a_marker_with_a_bad_planning_value_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=sometimes -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "bad-planning.md")

    assert "planning must be manual or auto" in str(caught.value)


def test_an_unknown_dependency_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a depends=ghost planning=auto -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "unknown-dep.md")

    assert "unknown phase ghost" in str(caught.value)


def test_a_dependency_on_an_existing_ledger_phase_is_accepted():
    text = """# Plan

## Phase one
<!-- phase: id=a depends=already-there planning=auto -->
Prose.
"""

    result = normalize(text, "existing-dep.md", existing=["already-there"])

    assert result.content["phases"][0]["depends_on"] == ["already-there"]


def test_a_dependency_cycle_is_refused():
    text = """# Plan

## Phase A
<!-- phase: id=a depends=b planning=auto -->
Prose A.

## Phase B
<!-- phase: id=b depends=a planning=auto -->
Prose B.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "cycle.md")

    assert "phase dependency cycle: 1 -> 2 -> 1" in str(caught.value)


def test_a_repeated_slice_name_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=auto -->
Prose.

<!-- slice: dup -->
### Task one

Body.

<!-- slice: dup -->
### Task two

Body.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "dup-slice.md")

    assert "dup" in str(caught.value)
    assert "own name" in str(caught.value)
