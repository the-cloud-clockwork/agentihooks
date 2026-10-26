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


def test_a_bare_json_list_takes_its_title_from_the_file_stem_and_renders_exact_markdown():
    data = json.dumps([{"title": "Only phase", "description": "Just one."}])

    result = normalize(data, "my-plan.json")

    assert result.kind == "json"
    assert result.title == "my-plan"
    assert result.content == {
        "title": "my-plan",
        "phases": [{"title": "Only phase", "description": "Just one.", "planning": "auto"}],
    }
    assert result.markdown == "# my-plan\n\n## Only phase\n\n<!-- phase: planning=auto -->\n\nJust one.\n"


def test_bytes_input_is_decoded_as_utf8_and_an_absent_description_defaults_to_empty():
    data = json.dumps({"phases": [{"title": "Only"}]}).encode("utf-8")

    result = normalize(data, "bytes.json")

    assert result.kind == "json"
    assert result.content["phases"][0]["title"] == "Only"
    assert result.content["phases"][0]["description"] == ""


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


def test_json_markdown_joins_multiple_dependencies_with_a_comma():
    data = json.dumps(
        {
            "phases": [
                {"title": "Alpha", "id": "alpha-id"},
                {"title": "Gamma", "id": "gamma-id"},
                {"title": "Beta", "depends_on": ["alpha-id", "gamma-id"]},
            ]
        }
    )

    result = normalize(data, "multi-dep.json")

    assert "depends=alpha-id,gamma-id" in result.markdown


def test_a_json_dependency_on_the_last_position_is_accepted():
    data = json.dumps({"phases": [{"title": "A", "depends_on": [3]}, {"title": "B"}, {"title": "C"}]})

    result = normalize(data, "boundary-dep.json")

    assert result.content["phases"][0]["depends_on"] == [3]


def test_a_json_boolean_dependency_is_refused():
    data = json.dumps({"phases": [{"title": "A", "depends_on": [True]}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bool-dep.json")

    assert caught.value.errors == ["phase 1: depends_on must be a list of positions or ids"]


def test_a_json_phases_list_that_is_empty_is_refused():
    data = json.dumps({"phases": []})

    with pytest.raises(PlanError) as caught:
        normalize(data, "empty-phases.json")

    assert caught.value.errors == ["the plan file needs a nonempty phases list"]


def test_a_json_phase_entry_that_is_not_an_object_is_refused():
    data = json.dumps({"phases": ["not an object"]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: must be an object"]


def test_a_json_phase_with_a_blank_title_is_refused():
    data = json.dumps({"phases": [{"title": "", "description": "x"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: needs a one line title"]


def test_a_json_phase_with_a_non_string_description_is_refused():
    data = json.dumps({"phases": [{"title": "A", "description": 5}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: description must be a string"]


def test_a_json_phase_with_a_bad_planning_value_is_refused():
    data = json.dumps({"phases": [{"title": "A", "planning": "later"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: planning must be manual or auto"]


def test_a_json_phase_with_a_non_list_depends_on_is_refused():
    data = json.dumps({"phases": [{"title": "A", "depends_on": "oops"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: depends_on must be a list of positions or ids"]


def test_a_json_integer_dependency_outside_the_plan_is_refused():
    data = json.dumps({"phases": [{"title": "A", "depends_on": [5]}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1 depends on position 5, outside the plan"]


def test_a_json_phase_with_a_non_string_id_is_refused():
    data = json.dumps({"phases": [{"title": "A", "id": 5}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == [
        "phase 1: id must start with a letter and hold only letters, digits, dots, dashes or underscores"
    ]


def test_a_json_phase_with_a_blank_not_duplicate_is_refused():
    data = json.dumps({"phases": [{"title": "A", "not_duplicate": "  "}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "bad.json")

    assert caught.value.errors == ["phase 1: not_duplicate must say in plain words why the phase differs"]


def test_json_phases_sharing_a_title_are_refused():
    data = json.dumps({"phases": [{"title": "Same"}, {"title": "Same"}]})

    with pytest.raises(PlanError) as caught:
        normalize(data, "dup.json")

    assert "Same" in str(caught.value)


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
    assert result.content["title"] == "Tiny plan"
    assert result.slices == {"First phase": ["pin1", "pin2"], "Second phase": []}
    titles = {phase["title"]: phase for phase in result.content["phases"]}
    assert titles["First phase"]["id"] == "pin"
    assert titles["First phase"]["depends_on"] == ["wrk"]
    assert titles["First phase"]["planning"] == "auto"
    assert "not_duplicate" not in titles["First phase"]
    assert "Intro prose before the example." in titles["First phase"]["description"]
    assert "the example. More prose" in titles["First phase"]["description"]
    assert "### Task title" not in titles["First phase"]["description"]
    assert "<!-- phase:" not in titles["First phase"]["description"]
    assert "Body of the first slice" not in titles["First phase"]["description"]
    assert "pin1 - First slice" not in titles["First phase"]["description"]
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


def test_a_blank_not_duplicate_marker_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=auto not_duplicate="   " -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "blank-nd.md")

    assert caught.value.errors == ["phase 'Phase one': not_duplicate must say in plain words why the phase differs"]


def test_a_marker_without_a_planning_key_defaults_to_auto():
    text = """# Plan

## Phase one
<!-- phase: id=a -->
Prose.
"""

    result = normalize(text, "default-planning.md")

    assert result.content["phases"][0]["planning"] == "auto"


def test_a_marker_with_multiple_dependencies_splits_on_comma():
    text = """# Plan

## Phase one
<!-- phase: id=a depends=x,y planning=auto -->
Prose.
"""

    result = normalize(text, "multi.md", existing=["x", "y"])

    assert result.content["phases"][0]["depends_on"] == ["x", "y"]


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


def test_a_prose_line_immediately_before_a_marker_is_not_treated_as_a_phase():
    text = """# Plan

Some prose line immediately above a marker.
<!-- phase: id=x planning=auto -->
Should not be its own phase.

## Real phase
<!-- phase: id=a planning=auto -->
Prose.
"""

    result = normalize(text, "prose-marker.md")

    assert [phase["title"] for phase in result.content["phases"]] == ["Real phase"]


def test_the_marker_is_the_first_non_blank_line_after_the_heading():
    text = """# Plan

## Spaced phase
{whitespace}
<!-- phase: id=spaced planning=manual -->
Spaced prose.

## Not a phase
Prose first.
<!-- phase: id=sneaky planning=auto -->
""".format(whitespace="   ")

    result = normalize(text, "neighbors.md")

    assert result.kind == "marked"
    assert result.content["phases"] == [
        {"title": "Spaced phase", "planning": "manual", "id": "spaced", "description": "Spaced prose."}
    ]


def test_has_marker_uses_the_markers_own_line_not_the_heading_after_it():
    text = """# Plan

## First phase
<!-- phase: id=a planning=auto -->
## Second phase
<!-- phase: id=b planning=auto -->
Prose.
"""

    result = normalize(text, "adjacent.md")

    assert [phase["title"] for phase in result.content["phases"]] == ["First phase", "Second phase"]
    assert result.content["phases"][0]["description"] == ""


def test_an_unknown_marker_key_does_not_stop_scanning_and_errors_join_with_semicolons():
    text = """# Plan

## Phase one
<!-- phase: bogus1=1 id=a bogus2=2 planning=auto -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "two-unknown.md")

    assert caught.value.errors == [
        "phase 'Phase one': unknown key bogus1 in phase marker",
        "phase 'Phase one': unknown key bogus2 in phase marker",
    ]
    assert str(caught.value) == (
        "phase 'Phase one': unknown key bogus1 in phase marker; phase 'Phase one': unknown key bogus2 in phase marker"
    )


def test_a_long_phase_description_is_bound_to_about_800_characters_on_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(400))
    text = f"""# Plan

## Phase one
<!-- phase: id=a planning=auto -->
{words}
"""

    result = normalize(text, "long.md")
    description = result.content["phases"][0]["description"]

    assert 790 < len(description) <= 800
    assert not description.endswith(" ")
    assert words.startswith(description)


def test_bound_searches_up_to_and_including_the_800th_character():
    tokens = "123456789 " * 85
    text = f"""# Plan

## Phase one
<!-- phase: id=a planning=auto -->
{tokens}
"""

    result = normalize(text, "boundary.md")

    assert len(result.content["phases"][0]["description"]) == 799


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

    assert caught.value.errors == ["the plan has no content"]


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

    assert caught.value.errors == ["phase headings must all sit at one heading level"]


def test_a_marker_with_an_unknown_key_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a bogus=1 planning=auto -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "unknown-key.md")

    assert caught.value.errors == ["phase 'Phase one': unknown key bogus in phase marker"]


def test_a_marker_with_a_bad_planning_value_is_refused():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=sometimes -->
Prose.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "bad-planning.md")

    assert caught.value.errors == ["phase 'Phase one': planning must be manual or auto"]


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


def test_a_dependency_list_with_an_existing_id_still_checks_the_rest():
    text = """# Plan

## Phase A
<!-- phase: id=a depends=already-there,b planning=auto -->
Prose A.

## Phase B
<!-- phase: id=b depends=a planning=auto -->
Prose B.
"""

    with pytest.raises(PlanError) as caught:
        normalize(text, "existing-then-cycle.md", existing=["already-there"])

    assert "dependency cycle" in str(caught.value)


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


def test_a_marker_on_the_last_line_of_the_file_marks_its_phase():
    result = normalize("# Plan\n\n## Only phase\n<!-- phase: planning=manual -->", "last-line.md")

    assert result.kind == "marked"
    assert result.content["phases"] == [{"title": "Only phase", "planning": "manual", "description": ""}]


def test_subheadings_without_any_slice_anchor_need_no_slice_markers():
    text = """# Plan

## Phase one
<!-- phase: id=a planning=auto -->
Prose.

### Step one

Body.

### Step two

Body.
"""

    result = normalize(text, "unsliced.md")

    assert result.slices == {"Phase one": []}
    assert result.content["phases"][0]["description"] == "Prose."


def test_json_markdown_normalizes_back_to_the_same_marked_phases():
    plan = {
        "title": "Round trip",
        "phases": [
            {"id": "a", "title": "First", "description": "One.", "planning": "manual"},
            {"id": "b", "title": "Second", "description": "Two.", "depends_on": ["a"]},
        ],
    }

    rendered = normalize(json.dumps(plan), "plan.json")
    again = normalize(rendered.markdown, "plan.md")

    assert again.kind == "marked"
    assert again.title == "Round trip"
    assert again.content == rendered.content


def test_a_long_description_without_spaces_is_cut_at_the_bound():
    token = "x" * 900
    text = f"# Plan\n\n## Phase one\n<!-- phase: id=a planning=auto -->\n{token}\n"

    result = normalize(text, "nospace.md")

    assert result.content["phases"][0]["description"] == "x" * 800


def test_a_long_description_with_one_early_space_keeps_only_the_first_word():
    text = f"# Plan\n\n## Phase one\n<!-- phase: id=a planning=auto -->\na {'y' * 900}\n"

    result = normalize(text, "early-space.md")

    assert result.content["phases"][0]["description"] == "a"


def test_a_word_ending_exactly_at_the_bound_is_kept():
    description = f"a {'x' * 798} {'y' * 50}"
    text = f"# Plan\n\n## Phase one\n<!-- phase: id=a planning=auto -->\n{description}\n"

    result = normalize(text, "word-at-bound.md")

    assert result.content["phases"][0]["description"] == f"a {'x' * 798}"


def test_a_space_just_past_the_bound_is_not_used_as_the_cut():
    text = f"# Plan\n\n## Phase one\n<!-- phase: id=a planning=auto -->\n{'x' * 801} y\n"

    result = normalize(text, "space-past-bound.md")

    assert result.content["phases"][0]["description"] == "x" * 800


def test_a_three_phase_cycle_names_every_phase_in_order():
    plan = [
        {"title": "A", "depends_on": [3]},
        {"title": "B", "depends_on": [1]},
        {"title": "C", "depends_on": [2]},
    ]

    with pytest.raises(PlanError) as caught:
        normalize(json.dumps(plan), "cycle.json")

    assert caught.value.errors == ["phase dependency cycle: 1 -> 3 -> 2 -> 1"]


def test_json_position_dependencies_survive_the_canonical_markdown():
    plan = {"title": "Positions", "phases": [{"title": "A"}, {"title": "B", "depends_on": [1]}]}

    rendered = normalize(json.dumps(plan), "positions.json")
    again = normalize(rendered.markdown, "positions.md")

    assert "<!-- phase: depends=1 planning=auto -->" in rendered.markdown
    assert again.content == rendered.content


@pytest.mark.parametrize(
    ("phase", "error"),
    [
        ({"title": "A\nB"}, "phase 1: needs a one line title"),
        ({"title": "A", "description": "Text.\n\n## Sneaky"}, "phase 1: description must not hold headings"),
        (
            {"title": "A", "id": "my phase"},
            "phase 1: id must start with a letter and hold only letters, digits, dots, dashes or underscores",
        ),
        (
            {"title": "A", "not_duplicate": 'a "quoted" reason'},
            "phase 1: " + "not_duplicate must say in plain words why the phase differs",
        ),
        (
            {"title": "A", "not_duplicate": "ends --> early"},
            "phase 1: " + "not_duplicate must say in plain words why the phase differs",
        ),
        (
            {"title": "A", "not_duplicate": "two\nlines"},
            "phase 1: " + "not_duplicate must say in plain words why the phase differs",
        ),
    ],
)
def test_json_values_that_cannot_render_as_one_marked_phase_are_refused(phase, error):
    with pytest.raises(PlanError) as caught:
        normalize(json.dumps([phase]), "bad.json")

    assert caught.value.errors == [error]


def test_two_phases_with_one_id_are_refused():
    plan = [{"title": "A", "id": "same"}, {"title": "B", "id": "same"}]

    with pytest.raises(PlanError) as caught:
        normalize(json.dumps(plan), "dup-id.json")

    assert caught.value.errors == ["each phase needs its own id: same"]


def test_a_marker_id_with_an_unclosed_quote_is_refused():
    text = '# Plan\n\n## Phase one\n<!-- phase: id="abc planning=auto -->\nProse.\n'

    with pytest.raises(PlanError) as caught:
        normalize(text, "unclosed.md")

    assert caught.value.errors == [
        "phase 'Phase one': id must start with a letter and hold only letters, digits, dots, dashes or underscores"
    ]


def test_bytes_that_are_not_utf8_are_refused():
    with pytest.raises(PlanError) as caught:
        normalize(b"\xff\xfe plan", "binary.md")

    assert caught.value.errors == ["the plan is not UTF-8 text"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("# Plan\n\n<!-- slice: one -->\n### Task\n\nBody.\n", "slice markers need phase markers above them"),
        (
            "# Plan\n\n<!-- slice: dup -->\nEarly.\n\n## Phase\n<!-- phase: planning=auto -->\n\n"
            "<!-- slice: dup -->\n### Task\n\nBody.\n",
            "each slice marker needs its own name in the file: dup",
        ),
        ("---\ntitle: x\n# not a title\n---\n\n# Plan\n\nBody.\n", "the plan must not start with YAML front matter"),
        ('{"title": "No phases"}', "a JSON plan needs a phases list"),
    ],
)
def test_sources_that_break_the_marker_rules_are_refused(text, error):
    with pytest.raises(PlanError) as caught:
        normalize(text, "bad.md")

    assert caught.value.errors == [error]
