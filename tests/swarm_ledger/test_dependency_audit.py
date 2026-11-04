import pytest

from scripts.swarm_ledger import dependency_audit

pytestmark = pytest.mark.unit


def task(task_id, territory, depends_on=(), state="open", **fields):
    return {
        "id": task_id,
        "title": f"title {task_id}",
        "state": state,
        "territory": list(territory),
        "depends_on": list(depends_on),
        **fields,
    }


def spec(text=""):
    return lambda task: text or task.get("description", "")


def test_a_dependency_with_disjoint_territories_and_no_named_code_is_reported():
    tasks = [
        task("a", ["scripts/swarm/status.py", "changelog.d/"]),
        task("b", ["scripts/hive/daemon.py", "changelog.d/"], ["a"]),
    ]
    assert dependency_audit.audit(tasks, spec()) == [
        {"task": "b", "title": "title b", "depends_on": "a", "dependency_title": "title a"}
    ]


@pytest.mark.parametrize(
    "theirs",
    [["scripts/swarm/status.py"], ["scripts/swarm/"], ["scripts/swarm"], ["scripts/swarm/status.py/"]],
)
def test_a_dependency_whose_territories_overlap_a_changed_module_is_not_reported(theirs):
    tasks = [task("a", theirs), task("b", ["scripts/swarm/status.py", "tests/swarm/"], ["a"])]
    assert dependency_audit.audit(tasks, spec()) == []


def test_a_sibling_folder_with_a_shared_prefix_is_not_an_overlap():
    tasks = [task("a", ["scripts/swarm_ledger/"]), task("b", ["scripts/swarm/"], ["a"])]
    assert [row["task"] for row in dependency_audit.audit(tasks, spec())] == ["b"]


@pytest.mark.parametrize(
    "text",
    [
        "extend scripts/swarm/status.py with the list",
        "the report in status.py grows a key",
        "call scripts.swarm.status from the tick",
        "everything under scripts/hive moves",
        "import scripts.hive and reuse it",
    ],
)
def test_a_dependent_whose_plan_names_the_other_tasks_code_is_not_reported(text):
    tasks = [task("a", ["scripts/swarm/status.py", "scripts/hive/"]), task("b", ["docs/"], ["a"], description=text)]
    assert dependency_audit.audit(tasks, spec()) == []


def test_the_spec_is_read_only_for_the_dependent_and_only_when_territories_are_disjoint():
    read = []
    tasks = [
        task("a", ["scripts/swarm/"]),
        task("b", ["docs/"], ["a"]),
        task("c", ["scripts/swarm/status.py"], ["a"]),
    ]

    def recorded(row):
        read.append(row["id"])
        return ""

    assert [row["task"] for row in dependency_audit.audit(tasks, recorded)] == ["b"]
    assert read == ["b"]


@pytest.mark.parametrize(
    ("dependent", "dependency"),
    [
        (task("b", ["docs/"], ["a"], state="done"), task("a", ["scripts/x.py"])),
        (task("b", ["docs/"], ["a"]), task("a", ["scripts/x.py"], state="done")),
        (task("b", ["changelog.d/"], ["a"]), task("a", ["scripts/x.py"])),
        (task("b", ["docs/"], ["a"]), task("a", ["changelog.d/x.md"])),
        (task("b", [], ["a"]), task("a", ["scripts/x.py"])),
        (task("b", ["", "/"], ["a"]), task("a", ["scripts/x.py"])),
        (task("b", ["docs/"], ["missing"]), task("a", ["scripts/x.py"])),
    ],
)
def test_closed_unknown_or_codeless_dependencies_are_not_judged(dependent, dependency):
    assert dependency_audit.audit([dependency, dependent], spec()) == []


@pytest.mark.parametrize("state", ["open", "claimed", "blocked", "pr"])
def test_every_open_state_on_both_sides_is_audited(state):
    tasks = [task("a", ["scripts/x.py"], state=state), task("b", ["docs/"], ["a"], state=state)]
    assert [row["depends_on"] for row in dependency_audit.audit(tasks, spec())] == ["a"]


def test_a_task_without_a_title_is_named_by_its_id():
    tasks = [{"id": "a", "territory": ["scripts/x.py"]}, {"id": "b", "territory": ["docs/"], "depends_on": ["a"]}]
    assert dependency_audit.audit(tasks, spec()) == [
        {"task": "b", "title": "b", "depends_on": "a", "dependency_title": "a"}
    ]


def test_the_plan_text_is_the_description_and_the_exact_plan_lines(monkeypatch):
    seen = []

    def exact(doc, row):
        seen.append((doc, row["id"]))
        return "plan names scripts/x.py\n"

    monkeypatch.setattr(dependency_audit.plan_read, "exact", exact)
    doc = {"tasks": []}
    text = dependency_audit.plan_text(doc)
    assert text({"id": "b", "description": "do it", "plan_lines": "3-4"}) == "do it\nplan names scripts/x.py\n"
    assert text({"id": "c", "description": "only this"}) == "only this\n"
    assert text({"id": "d"}) == "\n"
    assert seen == [(doc, "b")]


@pytest.mark.parametrize("error", [ValueError("no range"), FileNotFoundError("gone"), OSError("down")])
def test_an_unreadable_plan_falls_back_to_the_description(monkeypatch, error):
    def exact(doc, row):
        raise error

    monkeypatch.setattr(dependency_audit.plan_read, "exact", exact)
    assert dependency_audit.plan_text({})({"id": "b", "description": "do it", "plan_lines": "3-4"}) == "do it\n"
