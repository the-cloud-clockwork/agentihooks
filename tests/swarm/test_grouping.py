import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult, YesNo
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import grouping
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable, tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

PAGE = "scripts/swarm_ledger/static/js/render.js"


def task(task_id, territory=("scripts/swarm/tick.py",), **fields):
    return {
        "id": task_id,
        "title": f"title {task_id}",
        "description": f"spec {task_id}",
        "state": "open",
        "claimed_by": "",
        "lane": "eng",
        "kind": "code",
        "profile": "engineer",
        "difficulty": "S",
        "territory": list(territory),
        **fields,
    }


def doc(*tasks):
    return {"overview": "o", "tasks": list(tasks)}


def ids(groups):
    return [[t["id"] for t in group] for group in groups]


class GroupLedger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.groups, self.priorities = [], []

    def group_tasks(self, slug, lead, members):
        assert slug == "sw"
        self.groups.append((lead, list(members)))

    def priority(self, slug, item, text):
        assert slug == "sw"
        self.priorities.append((item, text))


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    return saved


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def answer(*probabilities):
        def decide(state, questions, **kw):
            calls.append((state, questions, kw))
            return DecisionResult(
                {name: Answer("noul", noul=p) for name, p in zip(questions, probabilities)}, "unit-test"
            )

        monkeypatch.setattr(grouping, "decide", decide)
        return calls

    return answer


def master_items(store):
    return InboxStore(store.redis).inbox(seat_address("sw", MASTER))


def test_limits():
    assert (grouping.PER_TICK, grouping.MIN_CONFIDENCE, grouping.APPLIES) == (3, 0.6, ("delegate", "full"))


def test_filter_groups_overlapping_small_tasks_and_leaves_the_rest():
    found = grouping.candidates(
        doc(
            task("a"),
            task("b", territory=["scripts/swarm"]),
            task("c", territory=["hooks/classifier"]),
            task("d", territory=["hooks/classifier/core.py"]),
            task("e", territory=["docs"]),
        )
    )
    assert ids(found) == [["a", "b"], ["c", "d"]]


def test_filter_groups_tasks_on_the_same_page_area():
    other_page = "scripts/swarm_ledger/static/css/tasks.css"
    found = grouping.candidates(doc(task("a", territory=[PAGE]), task("b", territory=[other_page])))
    assert ids(found) == [["a", "b"]]
    assert grouping.candidates(doc(task("a", territory=[PAGE]), task("b", territory=[other_page, "hooks"]))) == []


@pytest.mark.parametrize(
    "other",
    [
        task("b", lane="ci", kind="ci"),
        task("b", profile="frontend"),
        task("b", depends_on=["a"]),
        task("b", state="claimed", claimed_by="engineer@abcdef-0001"),
        task("b", claimed_by="engineer@abcdef-0001"),
        task("b", merged_into="z"),
        task("b", group_members=["z"]),
        task("b", out_of_scope=True),
        task("b", kind="ops"),
        task("b", difficulty=None),
        task("b", difficulty="L"),
        task("b", difficulty="M"),
        task("b", territory=[]),
    ],
)
def test_filter_never_pairs_tasks_that_differ_depend_or_are_taken(other):
    assert grouping.candidates(doc(task("a"), other)) == []


def test_filter_never_pairs_a_task_that_reaches_the_lead_through_a_done_task():
    found = grouping.candidates(doc(task("a"), task("b", depends_on=["x"]), task("x", state="done", depends_on=["a"])))
    assert found == []


def test_filter_groups_tasks_without_a_territory_in_the_same_phase_and_profile():
    found = grouping.candidates(doc(task("a", territory=[], phase="p1"), task("b", territory=[], phase="p1")))
    assert ids(found) == [["a", "b"]]


@pytest.mark.parametrize(
    "first, second",
    [
        (task("a", territory=[], phase="p1"), task("b", territory=[], phase="p2")),
        (task("a", territory=[]), task("b", territory=[])),
        (task("a", territory=[], phase="p1"), task("b", territory=[], phase="p1", profile="frontend")),
        (task("a", phase="p1"), task("b", territory=[], phase="p1")),
        (task("a", territory=[], phase="p1"), task("b", phase="p1")),
        (task("a", phase="p1"), task("b", territory=["docs"], phase="p1")),
    ],
)
def test_filter_never_pairs_tasks_without_a_territory_outside_a_shared_phase_and_profile(first, second):
    assert grouping.candidates(doc(first, second)) == []


def test_filter_caps_a_group_of_tasks_without_a_territory():
    found = grouping.candidates(doc(*(task(f"x{n}", territory=[], phase="p1") for n in range(7))))
    assert ids(found) == [["x0", "x1", "x2", "x3", "x4"], ["x5", "x6"]]


def test_filter_caps_a_group_at_five_tasks():
    found = grouping.candidates(doc(*(task(f"t{n}") for n in range(7))))
    assert ids(found) == [["t0", "t1", "t2", "t3", "t4"], ["t5", "t6"]]


def test_filter_keeps_the_ceiling_at_m():
    found = grouping.candidates(doc(task("a", difficulty="M"), task("b"), task("c")))
    assert ids(found) == [["b", "c"]]


def test_the_highest_ranked_task_leads():
    found = grouping.candidates(doc(task("a", rank="low"), task("b"), task("c", rank="urgent")))
    assert ids(found) == [["c", "b", "a"]]


def test_the_classifier_names_every_task_in_each_set(asked, store):
    calls = asked(0.9)
    config = store.update("sw", autonomy="delegate")
    grouping.group_pass("sw", config, store, GroupLedger([]), doc(task("a"), task("b")))
    state, questions, kw = calls[0]
    assert kw == {"purpose": "task-grouping"}
    assert questions == {
        "group_0": YesNo(
            "Tasks a titled title a, b titled title b: is this one change surface that one pull request, one review "
            "and one browser check cover?",
            true="one change surface, one review and one browser check cover every task",
            false="the tasks need separate changes, reviews or browser checks",
        )
    }
    assert state == {
        "overview": "o",
        "groups": [
            [
                {
                    "id": name,
                    "title": f"title {name}",
                    "description": f"spec {name}",
                    "difficulty": "S",
                    "territory": ["scripts/swarm/tick.py"],
                }
                for name in "ab"
            ]
        ],
    }


def test_delegate_applies_the_group_and_tells_the_master(asked, store):
    asked(0.9)
    config = store.update("sw", autonomy="delegate")
    ledger = GroupLedger([])
    actions = grouping.group_pass("sw", config, store, ledger, doc(task("a"), task("b"), task("c")))
    assert ledger.groups == [("a", ["b", "c"])] and ledger.priorities == []
    assert actions == ["grouped tasks b, c under a"]
    told = master_items(store)[0]
    assert told.fyi is True and told.text == (
        "For your information: the swarm grouped tasks b, c under task a, whose agent delivers all of them in one "
        "pull request."
    )
    assert store.redis.hget(store.key("sw", grouping.SEEN), "a,b,c") == "applied"


def test_full_autonomy_applies_too(asked, store):
    asked(0.9)
    ledger = GroupLedger([])
    grouping.group_pass("sw", store.update("sw", autonomy="full"), store, ledger, doc(task("a"), task("b")))
    assert ledger.groups == [("a", ["b"])]


@pytest.mark.parametrize("autonomy", ["manual", "assist"])
def test_lower_autonomy_raises_a_priority_and_asks_the_master(asked, store, autonomy):
    asked(0.9)
    ledger = GroupLedger([])
    tasks = doc(task("a"), task("b"), task("c"))
    actions = grouping.group_pass("sw", store.update("sw", autonomy=autonomy), store, ledger, tasks)
    assert ledger.groups == [] and ledger.priorities == [
        ("tasks/a", "Group 3 small tasks into one pull request led by this task.")
    ]
    assert actions == ["proposed grouping tasks b, c under a"]
    assert master_items(store)[0].text == (
        "The swarm proposes one pull request for tasks a and b, c, led by a. If the operator agrees, apply it with "
        "agentihooks ledger --slug sw --as <your name> task group a b c."
    )
    assert store.redis.hget(store.key("sw", grouping.SEEN), "a,b,c") == "proposed"


def test_a_set_is_asked_once(asked, store):
    calls = asked(0.9)
    config = store.update("sw", autonomy="manual")
    ledger = GroupLedger([])
    for _ in range(2):
        grouping.group_pass("sw", config, store, ledger, doc(task("a"), task("b")))
    assert len(calls) == 1 and len(ledger.priorities) == 1


@pytest.mark.parametrize("noul", [0.59, None, float("nan"), True])
def test_a_set_the_classifier_does_not_confirm_is_declined_for_good(asked, store, noul):
    calls = asked(noul)
    ledger = GroupLedger([])
    for _ in range(2):
        assert grouping.group_pass("sw", store.config("sw"), store, ledger, doc(task("a"), task("b"))) == []
    assert ledger.groups == [] and len(calls) == 1
    assert store.redis.hget(store.key("sw", grouping.SEEN), "a,b") == "declined"


def test_a_declined_set_goes_on_to_the_next_set(asked, store):
    asked(0.1, 0.9)
    ledger = GroupLedger([])
    tasks = doc(task("a"), task("b"), task("c", territory=["docs"]), task("d", territory=["docs"]))
    assert grouping.group_pass("sw", store.config("sw"), store, ledger, tasks) == ["grouped tasks d under c"]


def test_confidence_at_the_floor_confirms(asked, store):
    asked(0.6)
    ledger = GroupLedger([])
    grouping.group_pass("sw", store.config("sw"), store, ledger, doc(task("a"), task("b")))
    assert ledger.groups == [("a", ["b"])]


def test_no_classifier_forms_no_group_and_asks_again_later(monkeypatch, store, asked):
    def down(*args, **kwargs):
        raise ClassifierUnavailable("down")

    monkeypatch.setattr(grouping, "decide", down)
    ledger = GroupLedger([])
    assert grouping.group_pass("sw", store.config("sw"), store, ledger, doc(task("a"), task("b"))) == []
    asked(0.9)
    grouping.group_pass("sw", store.config("sw"), store, ledger, doc(task("a"), task("b")))
    assert ledger.groups == [("a", ["b"])]


def test_at_most_three_sets_are_asked_per_tick(asked, store):
    calls = asked(0.1, 0.1, 0.1)
    tasks = [task(f"{area}{n}", territory=[area]) for area in "pqrs" for n in range(2)]
    grouping.group_pass("sw", store.config("sw"), store, GroupLedger([]), doc(*tasks))
    assert len(calls[0][1]) == 3


def test_a_refused_group_write_goes_on_to_the_next_set(asked, store):
    asked(0.9, 0.9)

    class Refusing(GroupLedger):
        def group_tasks(self, slug, lead, members):
            if lead == "a":
                raise LedgerRefused("no")
            super().group_tasks(slug, lead, members)

    ledger = Refusing([])
    tasks = doc(task("a"), task("b"), task("c", territory=["docs"]), task("d", territory=["docs"]))
    actions = grouping.group_pass("sw", store.config("sw"), store, ledger, tasks)
    assert ledger.groups == [("c", ["d"])]
    assert actions[0] == "skipped grouping under task a: the ledger refused its write"
    assert store.redis.hget(store.key("sw", grouping.SEEN), "a,b") == "refused"


def test_a_dropped_proposal_is_recorded_as_refused(asked, store):
    asked(0.9)

    class Dropping(GroupLedger):
        def priority(self, slug, item, text):
            super().priority(slug, item, text)
            return False

    ledger = Dropping([])
    config = store.update("sw", autonomy="assist")
    actions = grouping.group_pass("sw", config, store, ledger, doc(task("a"), task("b")))
    assert actions == ["skipped grouping under task a: the ledger refused its write"]
    assert store.redis.hget(store.key("sw", grouping.SEEN), "a,b") == "refused"


def test_nothing_to_group_asks_nothing(asked, store):
    calls = asked()
    assert grouping.group_pass("sw", store.config("sw"), store, GroupLedger([]), doc(task("a"))) == []
    assert calls == []


def test_a_grouped_member_leaves_the_claim_queue(store):
    rows = {t["id"]: t for t in [task("a", group_members=["b"]), task("b", merged_into="a")]}
    assert [t["id"] for t in _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng")] == [
        "a"
    ]


def test_the_tick_groups_open_tasks(asked, store):
    asked(0.9)
    ledger = GroupLedger([task("a"), task("b")])
    actions = tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert "grouped tasks b under a" in actions


def test_the_lead_prompt_lists_every_member_spec():
    from scripts.swarm import prompt

    lead = {
        "id": "a",
        "title": "Lead",
        "group": [
            {"id": "b", "title": "title b", "description": "spec b"},
            {"id": "c", "title": "title c", "description": ""},
        ],
    }
    lines = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0003", lead).splitlines()
    start = lines.index(prompt.GROUP_LINE)
    assert lines[start + 1 : start + 3] == ["Task b: title b. spec b", "Task c: title c."]
    assert "one pull request" in prompt.GROUP_LINE and "swarm done" in prompt.GROUP_LINE


def test_a_prompt_without_a_group_says_nothing_about_members():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0003", {"id": "a", "title": "Lead", "group": []})
    assert prompt.GROUP_LINE not in text


def test_the_tick_gives_the_lead_agent_its_member_specs(store):
    store.update("sw", max_eng=1)
    ledger = GroupLedger([task("a", group_members=["b"]), task("b", merged_into="a")])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [t["id"] for t in runtime.tasks] == ["a"]
    assert runtime.tasks[0]["group"] == [{"id": "b", "title": "title b", "description": "spec b"}]


def test_a_group_applied_in_a_tick_spawns_only_its_lead_with_member_specs(asked, store):
    asked(0.9)
    store.update("sw", max_eng=2)
    ledger = GroupLedger([task("a"), task("b")])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [t["id"] for t in runtime.tasks] == ["a"]
    assert runtime.tasks[0]["group"] == [{"id": "b", "title": "title b", "description": "spec b"}]


class ReleaseLedger(GroupLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.ungrouped, self.stamps = [], {}

    def state(self, slug):
        found = super().state(slug)
        found["_meta"]["stamps"] = self.stamps
        return found

    def ungroup_tasks(self, slug, lead):
        assert slug == "sw"
        self.ungrouped.append(lead)


ENGINEER = "engineer@abcdef-0001"
GROUPED_AT, CHANGED_AFTER, CHANGED_BEFORE = 10, 20, 5


def grouped(lead_fields, state_rev=CHANGED_BEFORE):
    lead = task("a", group_members=["b", "c"], **lead_fields)
    members = [task("b", merged_into="a"), task("c", merged_into="a")]
    stamps = {"tasks/a/group_members": {"rev": GROUPED_AT}, "tasks/a/state": {"rev": state_rev}}
    return {"tasks": [lead, *members], "phases": [], "_meta": {"stamps": stamps}}


def release(store, found, claim=None, handoff=None):
    if claim:
        store.claim("sw", "a", claim, 60_000)
    if handoff:
        store.put_handoff("sw", "a", handoff)
    ledger = ReleaseLedger(found["tasks"])
    return ledger, grouping.release_pass("sw", store, ledger, found)


@pytest.mark.parametrize(
    "lead, state_rev, claim, handoff, why",
    [
        ({"state": "blocked", "claimed_by": ENGINEER}, CHANGED_AFTER, None, None, "is blocked and its agent let it go"),
        ({"state": "blocked", "claimed_by": ""}, CHANGED_AFTER, None, None, "is blocked and its agent let it go"),
        (
            {"state": "blocked", "claimed_by": ENGINEER},
            CHANGED_AFTER,
            "engineer@abcdef-0009",
            None,
            "is blocked and its agent let it go",
        ),
        ({"state": "open"}, CHANGED_AFTER, None, None, "was reopened"),
        ({"state": "done", "pr_url": ""}, CHANGED_AFTER, None, None, "closed without its pull request"),
        ({"state": "done"}, CHANGED_AFTER, None, None, "closed without its pull request"),
        ({"out_of_scope": True}, CHANGED_BEFORE, None, None, "closed without its pull request"),
    ],
)
def test_a_stopped_lead_releases_its_members(store, lead, state_rev, claim, handoff, why):
    found = grouped(lead, state_rev)
    ledger, actions = release(store, found, claim, handoff)
    assert ledger.ungrouped == ["a"]
    assert actions == [f"released tasks b, c from task a: its lead {why}"]
    rows = {t["id"]: t for t in found["tasks"]}
    assert "group_members" not in rows["a"]
    assert "merged_into" not in rows["b"] and "merged_into" not in rows["c"]


@pytest.mark.parametrize(
    "lead, state_rev, claim, handoff",
    [
        ({"state": "blocked", "claimed_by": ENGINEER}, CHANGED_AFTER, ENGINEER, None),
        ({"state": "open"}, CHANGED_BEFORE, None, None),
        ({"state": "open"}, GROUPED_AT, None, None),
        ({"state": "open"}, CHANGED_AFTER, None, "handoff for the next engineer"),
        ({"state": "claimed", "claimed_by": ENGINEER}, CHANGED_AFTER, None, None),
        ({"state": "pr", "claimed_by": ENGINEER}, CHANGED_AFTER, None, None),
        ({"state": "done", "pr_url": "https://github.com/o/r/pull/1"}, CHANGED_AFTER, None, None),
    ],
)
def test_a_working_lead_keeps_its_members(store, lead, state_rev, claim, handoff):
    found = grouped(lead, state_rev)
    ledger, actions = release(store, found, claim, handoff)
    assert (ledger.ungrouped, actions) == ([], [])
    rows = {t["id"]: t for t in found["tasks"]}
    assert rows["a"]["group_members"] == ["b", "c"] and rows["b"]["merged_into"] == "a"


@pytest.mark.parametrize("meta", [{}, None])
def test_a_lead_without_stamps_is_not_taken_for_reopened(store, meta):
    found = grouped({"state": "open"}, CHANGED_AFTER)
    found.pop("_meta")
    if meta is not None:
        found["_meta"] = meta
    ledger, actions = release(store, found)
    assert (ledger.ungrouped, actions) == ([], [])


def test_a_lead_without_a_state_stamp_is_not_taken_for_reopened(store):
    found = grouped({"state": "open"}, CHANGED_AFTER)
    del found["_meta"]["stamps"]["tasks/a/state"]
    ledger, actions = release(store, found)
    assert (ledger.ungrouped, actions) == ([], [])


def test_a_working_lead_does_not_stop_the_release_of_a_later_one(store):
    found = grouped({"state": "claimed", "claimed_by": ENGINEER}, CHANGED_AFTER)
    found["tasks"] += [task("d", state="done", group_members=["e"]), task("e", merged_into="d")]
    ledger, actions = release(store, found)
    assert ledger.ungrouped == ["d"]
    assert actions == ["released tasks e from task d: its lead closed without its pull request"]


def test_a_member_missing_from_the_ledger_is_skipped(store):
    found = grouped({"state": "done"}, CHANGED_AFTER)
    found["tasks"][0]["group_members"].append("z")
    ledger, actions = release(store, found)
    assert actions == ["released tasks b, c from task a: its lead closed without its pull request"]


def test_a_lead_without_a_grouping_stamp_is_not_taken_for_reopened(store):
    found = grouped({"state": "open"}, CHANGED_AFTER)
    del found["_meta"]["stamps"]["tasks/a/group_members"]
    ledger, actions = release(store, found)
    assert (ledger.ungrouped, actions) == ([], [])


def test_only_members_that_still_point_at_the_lead_are_released(store):
    found = grouped({"state": "done"}, CHANGED_AFTER)
    found["tasks"][2]["merged_into"] = "z"
    ledger, actions = release(store, found)
    assert actions == ["released tasks b from task a: its lead closed without its pull request"]
    assert found["tasks"][2]["merged_into"] == "z"


def test_a_refused_release_leaves_the_group_and_goes_on(store):
    class Refusing(ReleaseLedger):
        def ungroup_tasks(self, slug, lead):
            if lead == "a":
                raise LedgerRefused("refused")
            super().ungroup_tasks(slug, lead)

    found = grouped({"state": "done"}, CHANGED_AFTER)
    found["tasks"].append(task("d", state="done", group_members=["e"]))
    found["tasks"].append(task("e", merged_into="d"))
    ledger = Refusing(found["tasks"])
    actions = grouping.release_pass("sw", store, ledger, found)
    assert ledger.ungrouped == ["d"]
    assert actions == [
        "skipped releasing the group under task a: the ledger refused its write",
        "released tasks e from task d: its lead closed without its pull request",
    ]
    assert found["tasks"][1]["merged_into"] == "a"


def test_a_released_member_returns_to_the_claim_queue(store):
    found = grouped({"state": "blocked", "claimed_by": ENGINEER}, CHANGED_AFTER)
    rows = {t["id"]: t for t in found["tasks"]}
    assert _claimable("sw", store, rows, found, "eng") == []
    release(store, found)
    assert [t["id"] for t in _claimable("sw", store, rows, found, "eng")] == ["b", "c"]


def test_the_tick_releases_members_of_a_stopped_lead(store):
    ledger = ReleaseLedger(grouped({"state": "blocked", "claimed_by": ENGINEER}, CHANGED_AFTER)["tasks"])
    ledger.stamps = {"tasks/a/group_members": {"rev": GROUPED_AT}, "tasks/a/state": {"rev": CHANGED_AFTER}}
    actions = tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert ledger.ungrouped == ["a"]
    assert "released tasks b, c from task a: its lead is blocked and its agent let it go" in actions
