import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_tasks  # noqa: E402
from scripts.swarm_ledger.ledger_core import Context  # noqa: E402

PR = "https://github.com/o/r/pull/2"
REOPEN = {"state": "open", "claimed_by": ""}


def ledger(state):
    task = {"id": "t1", "title": "a", "lane": "eng", "kind": "code", "state": state, "claimed_by": "eng-1"}
    return {"tasks": [{**task, "pr_url": PR, "done": state == "done"}]}


def update(doc, fields, if_state):
    op = {"op": "task_update", "id": "u1", "by": "swarm", "item": "tasks/t1", "fields": fields, "if_state": if_state}
    ledger_tasks.check(op)
    ctx = Context({"rev": 0, "stamps": {}}, 1)
    return ledger_tasks.apply(doc, op, ctx), ctx


def test_a_guarded_reopen_leaves_a_done_task_done_and_is_not_refused():
    doc = ledger("done")
    applied, ctx = update(doc, REOPEN, ["claimed", "pr"])
    assert applied is True
    assert (doc["tasks"][0]["state"], doc["tasks"][0]["claimed_by"], doc["tasks"][0]["done"]) == ("done", "eng-1", True)
    assert (ctx.events, ctx.refused, ctx.dirty, ctx.stamps) == ([], [], False, {})


def test_a_guarded_reopen_applies_while_the_state_is_listed():
    doc = ledger("pr")
    applied, ctx = update(doc, REOPEN, ["claimed", "pr"])
    assert applied is True
    assert (doc["tasks"][0]["state"], doc["tasks"][0]["claimed_by"]) == ("open", "")
    assert [(e["kind"], e["target"]) for e in ctx.events] == [("task open", "tasks/t1")]


@pytest.mark.parametrize("if_state", [["finished"], "pr", [1], [{"state": "pr"}]])
def test_a_guard_naming_no_task_state_is_refused(if_state):
    with pytest.raises(ValueError, match="if_state"):
        update(ledger("pr"), REOPEN, if_state)


@pytest.mark.parametrize(("if_state", "after"), [(["open"], "claimed"), (["pr"], None)])
def test_a_task_with_no_state_is_guarded_as_open(if_state, after):
    doc = ledger("open")
    del doc["tasks"][0]["state"]
    update(doc, {"state": "claimed", "claimed_by": "eng-2"}, if_state)
    assert doc["tasks"][0].get("state") == after


STACK = {"branch": "engineer-323133-0001", "stacked_base": "0a86fd7a" * 5, "parked_on": ["t0"]}


def stacked_ledger():
    doc = ledger("claimed")
    doc["tasks"].insert(0, {"id": "t0", "title": "b", "lane": "eng", "kind": "code", "state": "pr"})
    return doc


def test_the_ledger_keeps_a_task_branch_stacked_base_and_parked_on():
    doc = stacked_ledger()
    applied, ctx = update(doc, STACK, [])
    assert applied is True
    assert {key: doc["tasks"][1][key] for key in STACK} == STACK
    assert sorted(ctx.stamps) == ["tasks/t1/branch", "tasks/t1/parked_on", "tasks/t1/stacked_base"]


def test_empty_values_clear_the_branch_and_park_state():
    doc = stacked_ledger()
    update(doc, STACK, [])
    applied, _ = update(doc, {"branch": "", "stacked_base": "", "parked_on": []}, [])
    assert applied is True
    assert (doc["tasks"][1]["branch"], doc["tasks"][1]["stacked_base"], doc["tasks"][1]["parked_on"]) == ("", "", [])


PARK_REFUSAL = "tasks/t1 cannot be parked without a branch and a stacked base: park it with agentihooks swarm park"


@pytest.mark.parametrize(
    "fields",
    [
        {"parked_on": ["t0"]},
        {"parked_on": ["t0"], "branch": "engineer-323133-0001"},
        {"parked_on": ["t0"], "stacked_base": "0a86fd7a" * 5},
    ],
)
def test_a_park_with_no_branch_or_stacked_base_is_refused(fields):
    doc = stacked_ledger()
    applied, ctx = update(doc, fields, [])
    assert applied is False and "parked_on" not in doc["tasks"][1]
    assert ctx.refused == [PARK_REFUSAL]


def test_clearing_a_park_with_no_branch_is_allowed():
    doc = stacked_ledger()
    doc["tasks"][1]["parked_on"] = ["t0"]
    applied, ctx = update(doc, {"parked_on": []}, [])
    assert applied is True and doc["tasks"][1]["parked_on"] == [] and ctx.refused == []


BRANCH_REFUSAL = "branch must be a git branch name, or empty to clear it"
BASE_REFUSAL = "stacked_base must be a lowercase commit hash of 7 to 64 characters, or empty to clear it"
PARKED_REFUSAL = "parked_on must list task ids"
LIST_REFUSAL = "parked_on must be a list of nonempty strings"


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"branch": "two words"}, BRANCH_REFUSAL),
        ({"branch": "tab\there"}, BRANCH_REFUSAL),
        ({"branch": "--upload-pack=touch x"}, BRANCH_REFUSAL),
        ({"stacked_base": "dev"}, BASE_REFUSAL),
        ({"stacked_base": "0a86fd"}, BASE_REFUSAL),
        ({"stacked_base": "0A86FD7A"}, BASE_REFUSAL),
        ({"stacked_base": "0" * 65}, BASE_REFUSAL),
        ({"stacked_base": "x0a86fd7a"}, BASE_REFUSAL),
        ({"stacked_base": "0a86fd7ax"}, BASE_REFUSAL),
        ({"parked_on": "t0"}, LIST_REFUSAL),
        ({"parked_on": [""]}, LIST_REFUSAL),
        ({"parked_on": [3]}, LIST_REFUSAL),
        ({"parked_on": ["tasks/t0"]}, PARKED_REFUSAL),
    ],
)
def test_bad_branch_and_park_shapes_are_refused(fields, refusal):
    with pytest.raises(ValueError) as refused:
        update(stacked_ledger(), fields, [])
    assert str(refused.value) == refusal


@pytest.mark.parametrize("fields", [{"branch": 3}, {"branch": ["engineer-1"]}, {"stacked_base": 7}])
def test_a_branch_or_stacked_base_that_is_not_a_string_is_refused(fields):
    with pytest.raises(ValueError, match="task_update may set only"):
        update(stacked_ledger(), fields, [])


RELEASE_REFUSAL = "after_release must be a release tag such as v2.17.1, or empty to clear it"


@pytest.mark.parametrize("value", ["latest", "v2.17", "v2.17.1-rc1", "xv2.17.1", "v2.17.1x", "v 2.17.1", "v2.17.1\n"])
def test_an_after_release_that_is_not_a_release_tag_is_refused(value):
    with pytest.raises(ValueError) as refused:
        update(stacked_ledger(), {"after_release": value}, [])
    assert str(refused.value) == RELEASE_REFUSAL


def test_an_after_release_that_is_not_a_string_is_refused():
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check_release({"after_release": 5})
    assert str(refused.value) == RELEASE_REFUSAL


@pytest.mark.parametrize("value", ["v2.17.1", "2.17.1", ""])
def test_a_task_can_name_the_release_it_waits_on(value):
    doc = stacked_ledger()
    applied, ctx = update(doc, {"after_release": value}, [])
    assert applied is True and ctx.refused == [] and doc["tasks"][1]["after_release"] == value


@pytest.mark.parametrize("base", ["0a86fd7", "0" * 64])
def test_a_stacked_base_of_seven_to_sixty_four_hex_characters_is_kept(base):
    doc = stacked_ledger()
    assert update(doc, {"stacked_base": base}, [])[0] is True
    assert doc["tasks"][1]["stacked_base"] == base


REPOS = {"branch_repo": "git@github.com:o/bundle.git", "parked_repos": ["https://github.com/o/r", "/srv/docs.git"]}


def test_the_ledger_keeps_a_branch_repository_and_the_parked_repositories():
    doc = stacked_ledger()
    applied, ctx = update(doc, REPOS, [])
    assert applied is True
    assert {key: doc["tasks"][1][key] for key in REPOS} == REPOS
    assert sorted(ctx.stamps) == ["tasks/t1/branch_repo", "tasks/t1/parked_repos"]
    update(doc, {"branch_repo": "", "parked_repos": []}, [])
    assert (doc["tasks"][1]["branch_repo"], doc["tasks"][1]["parked_repos"]) == ("", [])


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"branch_repo": "two words"}, "branch_repo must be a repository url or path, or empty to clear it"),
        (
            {"branch_repo": "--upload-pack=touch x"},
            "branch_repo must be a repository url or path, or empty to clear it",
        ),
        ({"parked_repos": ["a b"]}, "parked_repos must list repository urls or paths"),
        ({"parked_repos": ["-oops"]}, "parked_repos must list repository urls or paths"),
        ({"parked_repos": "https://github.com/o/r"}, "parked_repos must be a list of nonempty strings"),
        ({"parked_repos": [""]}, "parked_repos must be a list of nonempty strings"),
    ],
)
def test_bad_repository_shapes_are_refused(fields, refusal):
    with pytest.raises(ValueError) as refused:
        update(stacked_ledger(), fields, [])
    assert str(refused.value) == refusal


def test_a_branch_repository_that_is_not_a_string_is_refused():
    with pytest.raises(ValueError) as refused:
        update(stacked_ledger(), {"branch_repo": ["git@github.com:o/r.git"]}, [])
    assert str(refused.value) == (
        f"task_update may set only {ledger_tasks.UPDATABLE}, as strings, {ledger_tasks.LIST_FIELDS} as lists or "
        f"{ledger_tasks.OBJECT_FIELDS} as objects"
    )
    assert {"branch_repo", "parked_repos"} <= set(ledger_tasks.UPDATABLE)


def test_the_ledger_keeps_how_a_replacement_continued_and_from_which_watermark():
    doc = ledger("claimed")
    recovery = {"checkpoint_id": "ckpt-1", "recovery": "native", "session_id": "s-1", "watermark": 4096}
    applied, ctx = update(doc, {"recovery": recovery}, [])
    assert applied is True
    assert doc["tasks"][0]["recovery"] == recovery
    assert sorted(ctx.stamps) == ["tasks/t1/recovery"]
    assert ledger_tasks.OBJECT_FIELDS == ("contract", "proof", "recovery", "repository_branches", "pull_scopes")
    assert ledger_tasks.UPDATABLE[-1] == "recovery"


@pytest.mark.parametrize(
    ("task", "refusal"),
    [
        ({"branch": 3}, BRANCH_REFUSAL),
        ({"stacked_base": "main"}, BASE_REFUSAL),
        ({"stacked_base": 7}, BASE_REFUSAL),
        ({"parked_on": "t0"}, LIST_REFUSAL),
        ({"parked_on": ["a b"]}, PARKED_REFUSAL),
    ],
)
def test_a_task_carrying_a_bad_branch_or_park_shape_is_refused_however_it_arrives(task, refusal):
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check_task({"id": "t1", "title": "a", "lane": "eng", "kind": "code", **task})
    assert str(refused.value) == refusal


def test_a_task_without_branch_or_park_fields_passes():
    ledger_tasks.check_task({"id": "t1", "title": "a", "lane": "eng", "kind": "code"})


@pytest.mark.parametrize("fields", [{"depends_on": ["t9"]}, {"depends_on": ["t0"], "parked_on": ["t9"]}])
def test_depends_on_still_names_only_tasks_on_the_ledger(fields):
    doc = stacked_ledger()
    applied, ctx = update(doc, fields, [])
    assert applied is False and "depends_on" not in doc["tasks"][1] and ctx.dirty is False


def test_parked_on_names_only_tasks_on_the_ledger():
    doc = stacked_ledger()
    applied, ctx = update(doc, {"parked_on": ["t9"]}, [])
    assert applied is False and "parked_on" not in doc["tasks"][1] and ctx.dirty is False


APPLIED_REFUSAL = "applied_at must be a UTC time such as 2026-10-18T12:00:00Z, or empty to clear it"


@pytest.mark.parametrize(
    "value",
    ["2026-10-18", "2026-10-18T12:00Z", "2026-10-18T12:00:00", "x2026-10-18T12:00:00Z", "2026-10-18T12:00:00Zx"],
)
def test_an_applied_time_that_is_not_a_utc_time_is_refused(value):
    with pytest.raises(ValueError) as refused:
        update(stacked_ledger(), {"applied_at": value}, [])
    assert str(refused.value) == APPLIED_REFUSAL


def test_an_applied_time_that_is_not_a_string_is_refused():
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check_applied({"applied_at": 5})
    assert str(refused.value) == APPLIED_REFUSAL


@pytest.mark.parametrize("value", ["2026-10-18T12:00:00Z", ""])
def test_a_task_records_when_its_fix_was_applied(value):
    doc = stacked_ledger()
    applied, ctx = update(doc, {"applied_at": value}, [])
    assert applied is True and ctx.refused == [] and doc["tasks"][1]["applied_at"] == value


@pytest.mark.parametrize("kind", ["tune", "ops", "troubleshoot", "research"])
def test_a_read_only_task_parks_without_a_branch_or_stacked_base(kind):
    doc = stacked_ledger()
    doc["tasks"][1]["kind"] = kind
    applied, ctx = update(doc, {"parked_on": ["t0"]}, [])
    assert applied is True and ctx.refused == [] and doc["tasks"][1]["parked_on"] == ["t0"]
