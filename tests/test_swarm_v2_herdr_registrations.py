import dataclasses
import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2.herdr import catalog, reconcile
from scripts.swarm_v2.herdr.capabilities import Incompatible, Unreachable
from scripts.swarm_v2.herdr.catalog import Catalog, Machine
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused, Scope
from scripts.swarm_v2.herdr.reconcile import Record, Registrations, Report, Store
from tests import sv2_hdr03_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-HDR-03"
EXE_A = "exe-" + "a" * 32
EXE_B = "exe-" + "b" * 32
NAME_A = f"swarm-{EXE_A}-terminal"
TARGET_A = f"ssh://{NAME_A}.swarm-workers.svc.cluster.local"
LISTED = {
    "id": "dfc82003987b56c2d3094030456bbd11",
    "label": "hdr01",
    "target": "ssh://worker@127.0.0.1:22488",
    "session": "default",
    "enabled": True,
    "selected": False,
}


def refusal(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


class Recorder:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(command)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def done(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Verdicts:
    def __init__(self, outcome=True) -> None:
        self.outcome = outcome
        self.asked: list[tuple[str, str]] = []

    def verdict(self, target: str, incarnation: str):
        self.asked.append((target, incarnation))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return SimpleNamespace(compatible=self.outcome)


def world(tmp_path, machines=(), ready=None):
    desired = cases.endpoints()
    herdr = cases.Herdr(list(machines), {e.target for e in desired} if ready is None else ready)
    return Registrations(Catalog(herdr), Store(tmp_path / "registrations.json")), herdr, desired


def machine(profile_id: str, label: str, target: str, session: str = "default") -> dict:
    return {"id": profile_id, "label": label, "target": target, "session": session, "enabled": True}


def test_a_listing_parses_into_machines_with_opaque_profile_ids():
    assert catalog.parse(json.dumps([LISTED, LISTED | {"id": "p2", "enabled": False}])) == (
        Machine("dfc82003987b56c2d3094030456bbd11", "hdr01", "ssh://worker@127.0.0.1:22488", "default", True),
        Machine("p2", "hdr01", "ssh://worker@127.0.0.1:22488", "default", False),
    )
    assert catalog.parse("[]") == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "herdr machine list is not JSON"),
        ("{}", "herdr machine list is not a list"),
        ('["x"]', "herdr machine list holds an entry that is not an object"),
        (
            json.dumps([{k: v for k, v in LISTED.items() if k != "id"}]),
            "herdr machine list holds an incomplete machine",
        ),
        (json.dumps([LISTED | {"label": ""}]), "herdr machine list holds an incomplete machine"),
        (json.dumps([LISTED | {"target": 7}]), "herdr machine list holds an incomplete machine"),
        (json.dumps([LISTED | {"session": None}]), "herdr machine list holds an incomplete machine"),
        (json.dumps([LISTED | {"enabled": "true"}]), "herdr machine list holds an incomplete machine"),
        (json.dumps([LISTED | {"enabled": 1}]), "herdr machine list holds an incomplete machine"),
    ],
)
def test_an_unreadable_listing_is_refused_not_read_as_empty(text, message):
    assert refusal(lambda: catalog.parse(text)) == ("catalog_unreadable", message)


def test_machines_lists_through_the_documented_command():
    run = Recorder(done(stdout=json.dumps([LISTED])))
    assert Catalog(run).machines()[0].profile_id == LISTED["id"]
    assert run.commands == [["herdr", "machine", "list", "--json"]]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (done(1, "[]"), ("catalog_unreadable", "herdr machine list failed")),
        (OSError("gone"), ("catalog_unreachable", "herdr machine list failed: herdr did not answer")),
        (
            subprocess.TimeoutExpired(["herdr"], 30),
            ("catalog_unreachable", "herdr machine list failed: herdr did not answer"),
        ),
    ],
)
def test_a_failed_listing_is_refused(reply, expected):
    assert refusal(lambda: Catalog(Recorder(reply)).machines()) == expected


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("Saved SSH machine p-1_A. Remote server is ready.\nOpen Herdr clients connect automatically.\n", ""),
        ("", "Saved SSH machine p-1_A.\n"),
    ],
)
def test_add_saves_with_an_explicit_remote_session_and_returns_the_profile_id(stdout, stderr):
    run = Recorder(done(0, stdout, stderr))
    assert Catalog(run).add(NAME_A, TARGET_A, "default") == "p-1_A"
    assert run.commands == [["herdr", "machine", "add", "--label", NAME_A, "--remote-session", "default", TARGET_A]]


def test_an_add_that_names_no_profile_id_is_ambiguous():
    assert refusal(lambda: Catalog(Recorder(done(0, "saved"))).add("w", "ssh://w", "default")) == (
        "catalog_ambiguous",
        "herdr saved w without naming its profile id",
    )


def test_a_refused_add_names_the_label():
    assert refusal(lambda: Catalog(Recorder(done(1, "", cases.NOT_READY))).add("w", "ssh://w", "s")) == (
        "machine_refused",
        "herdr could not save w",
    )


def test_an_unanswered_add_is_unreachable_not_refused():
    reply = subprocess.TimeoutExpired(["herdr"], 30)
    assert refusal(lambda: Catalog(Recorder(reply)).add("w", "ssh://w", "s")) == (
        "catalog_unreachable",
        "herdr could not save w: herdr did not answer",
    )


def test_rename_and_remove_address_one_profile_id():
    run = Recorder(done(), done())
    Catalog(run).rename("p1", "new")
    Catalog(run).remove("p1")
    assert run.commands == [
        ["herdr", "machine", "rename", "p1", "--label", "new"],
        ["herdr", "machine", "remove", "p1"],
    ]


def test_refused_rename_and_remove_name_the_profile():
    assert refusal(lambda: Catalog(Recorder(done(1))).rename("p1", "x")) == (
        "rename_refused",
        "herdr could not rename p1",
    )
    assert refusal(lambda: Catalog(Recorder(done(1))).remove("p1")) == ("remove_refused", "herdr could not remove p1")


def test_the_store_round_trips_a_private_sorted_document(tmp_path):
    store = Store(tmp_path / "home" / "registrations.json")
    assert store.load() == {}
    records = {
        EXE_B: Record(EXE_B, 1, "b", "ssh://b", "default", "pb"),
        EXE_A: Record(EXE_A, 2, "a", "ssh://a", "default", None),
    }
    store.path.parent.mkdir()
    (store.path.parent / ".registrations.json.tmp").write_text("stale")
    store.save(records)
    assert store.load() == records
    document = json.loads(store.path.read_text())
    assert document == {
        "version": 1,
        "registrations": [
            {"execution_id": EXE_A, "generation": 2, "label": "a", "target": "ssh://a", "session": "default"}
            | {"profile_id": None, "before": []},
            {"execution_id": EXE_B, "generation": 1, "label": "b", "target": "ssh://b", "session": "default"}
            | {"profile_id": "pb", "before": []},
        ],
    }
    assert store.path.read_text().endswith("}\n")
    assert store.path.read_text().startswith('{\n  "version": 1,')
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not (store.path.parent / ".registrations.json.tmp").exists()


def test_the_store_creates_a_private_parent(tmp_path):
    store = Store(tmp_path / "new" / "deeper" / "registrations.json")
    store.save({})
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load() == {}


@pytest.mark.parametrize(
    "text",
    [
        "{",
        '{"version": 2, "registrations": []}',
        '{"registrations": []}',
        '{"version": 1}',
        '{"version": 1, "registrations": [{"execution_id": "x"}]}',
    ],
)
def test_an_unreadable_ownership_record_refuses(tmp_path, text):
    path = tmp_path / "registrations.json"
    path.write_text(text)
    assert refusal(lambda: Store(path).load()) == (
        "ownership_unreadable",
        "the registration ownership record is unreadable",
    )


def test_owned_finds_a_recorded_profile_by_id_only():
    record = Record(EXE_A, 2, NAME_A, TARGET_A, "default", "p1")
    other = Machine("p2", NAME_A, TARGET_A, "default", True)
    mine = Machine("p1", "renamed", "ssh://elsewhere", "other", False)
    assert reconcile.owned(None, [mine]) is None
    assert reconcile.owned(record, [other, mine]) is mine
    assert reconcile.owned(record, [other]) is None


@pytest.mark.parametrize(
    "found",
    [
        Machine("p1", "x", TARGET_A, "default", True),
        Machine("p1", NAME_A, "ssh://x", "default", True),
        Machine("p1", NAME_A, TARGET_A, "x", True),
    ],
)
def test_a_pending_record_adopts_only_an_exact_label_target_and_session_match(found):
    pending = Record(EXE_A, 2, NAME_A, TARGET_A, "default", None)
    exact = Machine("p2", NAME_A, TARGET_A, "default", True)
    assert reconcile.owned(pending, [found]) is None
    assert reconcile.owned(pending, [found, exact]) is exact


def test_a_pending_record_never_adopts_a_profile_listed_before_its_add():
    pending = Record(EXE_A, 2, NAME_A, TARGET_A, "default", None, ("m1",))
    manual = Machine("m1", NAME_A, TARGET_A, "default", True)
    added = Machine("p2", NAME_A, TARGET_A, "default", True)
    assert reconcile.owned(pending, [manual]) is None
    assert reconcile.owned(pending, [manual, added]) is added


def test_an_add_records_every_profile_listed_before_it(tmp_path):
    listed = [machine("m2", "devbox", "ssh://devbox"), machine("m1", "other", "ssh://other")]
    registrations, herdr, desired = world(tmp_path, listed)
    herdr.unanswered.add(TARGET_A)
    refusal(lambda: registrations.register(desired[0]))
    assert registrations.store.load()[EXE_A].before == ("m1", "m2")


def test_a_pending_record_saved_twice_is_ambiguous():
    pending = Record(EXE_A, 2, NAME_A, TARGET_A, "default", None)
    twice = [Machine(f"p{n}", NAME_A, TARGET_A, "default", True) for n in (1, 2)]
    assert refusal(lambda: reconcile.owned(pending, twice)) == (
        "catalog_ambiguous",
        f"{NAME_A} was saved more than once after an interrupted add",
    )


def test_newest_keeps_the_highest_generation_of_each_execution():
    old, new = cases.endpoints(2)[0], cases.endpoints(3)[0]
    same = cases.endpoints(3)[0]
    other = cases.endpoints()[1]
    assert reconcile.newest([new, other, old]) == {EXE_A: new, EXE_B: other}
    assert reconcile.newest([old, new])[EXE_A] is new
    assert reconcile.newest([new, same])[EXE_A] is new


def test_registrations_select_the_default_remote_session(tmp_path):
    registrations, _, _ = world(tmp_path)
    assert registrations.session == "default" == catalog.REMOTE_SESSION


def test_register_adds_a_missing_endpoint_and_records_its_profile(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    profile = registrations.register(desired[0])
    assert herdr.machines == [machine(profile, NAME_A, TARGET_A) | {"selected": False}]
    assert registrations.store.load() == {EXE_A: Record(EXE_A, 2, NAME_A, TARGET_A, "default", profile)}
    assert registrations.drift == {"missing": 1}
    assert registrations.register(desired[0]) == profile
    assert herdr.adds(NAME_A) == 1
    assert registrations.drift == {"missing": 1}


def test_a_stale_generation_is_refused_without_touching_the_newer_record(tmp_path):
    registrations, herdr, _ = world(tmp_path)
    profile = registrations.register(cases.endpoints(3)[0])
    assert refusal(lambda: registrations.register(cases.endpoints(2)[0])) == (
        "stale_generation",
        f"{NAME_A} is registered for generation 3",
    )
    assert registrations.store.load()[EXE_A].generation == 3
    assert registrations.register(cases.endpoints(3)[0]) == profile


def test_a_drifted_managed_label_is_renamed_back(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    profile = registrations.register(desired[0])
    herdr.machines[0]["label"] = "by-hand"
    herdr.machines.append(machine("m1", "devbox", TARGET_A))
    assert registrations.register(desired[0]) == profile
    assert herdr.commands[-1] == ["herdr", "machine", "rename", profile, "--label", NAME_A]
    assert herdr.machines[0]["label"] == NAME_A
    assert registrations.drift == {"missing": 1, "label": 1}


def test_a_rename_onto_another_machines_label_is_refused(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    registrations.register(desired[0])
    herdr.machines[0]["label"] = "by-hand"
    herdr.machines.append(machine("m1", NAME_A, "ssh://devbox"))
    assert refusal(lambda: registrations.register(desired[0])) == (
        "label_collision",
        f"{NAME_A} is the label of a machine this controller does not own; audit it and remove it with herdr machine remove before this endpoint registers",
    )
    assert not any(command[:3] == ["herdr", "machine", "rename"] for command in herdr.commands)
    assert registrations.drift == {"missing": 1, "collision": 1}


def test_a_target_already_saved_unmanaged_is_refused(tmp_path):
    registrations, herdr, desired = world(tmp_path, [machine("m1", "devbox", TARGET_A)])
    assert refusal(lambda: registrations.register(desired[0])) == (
        "target_collision",
        f"{NAME_A} is already saved by a machine this controller does not own; audit it and remove it with herdr machine remove before this endpoint registers",
    )
    assert herdr.mutating() == []
    assert not registrations.store.path.exists()
    assert registrations.drift == {"missing": 1, "collision": 1}


def test_a_refused_add_drops_its_pending_record(tmp_path):
    registrations, herdr, desired = world(tmp_path, ready=set())
    assert refusal(lambda: registrations.register(desired[0]))[0] == "machine_refused"
    assert registrations.store.load() == {}
    assert registrations.store.path.exists()


def test_an_unanswered_add_keeps_its_pending_record(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    herdr.unanswered.add(TARGET_A)
    assert refusal(lambda: registrations.register(desired[0]))[0] == "catalog_unreachable"
    assert registrations.store.load() == {EXE_A: Record(EXE_A, 2, NAME_A, TARGET_A, "default", None)}
    herdr.unanswered.clear()
    assert registrations.register(desired[0]) == herdr.machines[0]["id"]
    assert herdr.adds(NAME_A) == 1
    assert registrations.drift == {"missing": 1, "interrupted": 1}


def test_an_ambiguous_add_keeps_its_pending_record(tmp_path):
    run = Recorder(done(0, "[]"), done(0, "saved"))
    registrations = Registrations(Catalog(run), Store(tmp_path / "r.json"))
    assert refusal(lambda: registrations.register(cases.endpoints()[0]))[0] == "catalog_ambiguous"
    assert registrations.store.load()[EXE_A].profile_id is None


def test_reconcile_registers_desired_endpoints_in_execution_order(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    verdicts = Verdicts()
    report = registrations.reconcile(list(reversed(desired)), verdicts)
    assert list(report.registered) == [EXE_A, EXE_B]
    assert verdicts.asked == [(endpoint.name, endpoint.incarnation) for endpoint in desired]
    assert (report.refused, report.retired, report.drift) == ({}, [], {"missing": 2})
    assert report.herdr_registration_drift() == 2
    again = registrations.reconcile(desired, verdicts)
    assert (again.registered, again.drift, again.herdr_registration_drift()) == (report.registered, {}, 0)


def test_an_empty_report_has_no_drift():
    assert Report().herdr_registration_drift() == 0
    assert Report(drift={"missing": 2, "label": 3}).herdr_registration_drift() == 5


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [(False, "incompatible"), (Incompatible("replaced"), "incompatible"), (Unreachable("x"), "unreachable")],
)
def test_a_server_that_does_not_qualify_is_unregistered(tmp_path, outcome, reason):
    registrations, herdr, desired = world(tmp_path)
    report = registrations.reconcile(desired[:1], Verdicts(outcome))
    assert (report.registered, registrations.store.load(), herdr.machines) == ({}, {}, [])
    assert report.refused == {EXE_A: reason}
    message = refusal(lambda: registrations._admit(desired[0], {}, (), Verdicts(outcome)))
    assert message == (reason, f"{NAME_A} did not qualify: {reason}")


def test_reconcile_retires_only_owned_profiles_and_counts_those_still_saved(tmp_path):
    registrations, herdr, desired = world(tmp_path, [machine("m1", "devbox", "ssh://devbox")])
    registrations.reconcile(desired, Verdicts())
    gone = Record("exe-" + "c" * 32, 1, "x", "ssh://x", "default", "gone")
    registrations.store.save(registrations.store.load() | {gone.execution_id: gone})
    report = registrations.reconcile([], Verdicts())
    assert report.retired == [EXE_A, EXE_B, "exe-" + "c" * 32]
    assert report.drift == {"retired": 2}
    assert herdr.machines == [machine("m1", "devbox", "ssh://devbox")]
    assert registrations.store.load() == {}


def test_a_failed_retirement_keeps_its_record_for_the_next_pass(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    registrations.reconcile(desired[:1], Verdicts())
    herdr._remove = lambda command: subprocess.CompletedProcess(command, 1, "", "")
    report = registrations.reconcile([], Verdicts())
    assert (report.retired, report.refused) == ([], {EXE_A: "remove_refused"})
    assert list(registrations.store.load()) == [EXE_A]


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_an_add_reply_without_an_id_after_the_marker_is_ambiguous():
    assert refusal(lambda: Catalog(Recorder(done(0, "Saved SSH machine .\n"))).add("w", "ssh://w", "s"))[0] == (
        "catalog_ambiguous"
    )


def test_the_profile_id_on_standard_output_wins():
    run = Recorder(done(0, "Saved SSH machine out1.\n", "Saved SSH machine err1.\n"))
    assert Catalog(run).add("w", "ssh://w", "s") == "out1"


def test_machines_and_records_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Machine("p", "l", "t", "s", True).label = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        Record(EXE_A, 1, "l", "t", "s", None).profile_id = "x"


@pytest.mark.parametrize(
    "entry",
    [
        {"generation": "2"},
        {"generation": True},
        {"label": ""},
        {"target": 7},
        {"session": None},
        {"execution_id": ""},
        {"profile_id": 3},
        {"before": ["m1", ""]},
        {"before": [1]},
        {"before": 5},
        {"before": "m1"},
        {"generation": 0},
        {"profile_id": ""},
    ],
)
def test_an_ownership_record_with_a_mistyped_field_is_unreadable(tmp_path, entry):
    good = {"execution_id": EXE_A, "generation": 2, "label": NAME_A, "target": TARGET_A, "session": "default"}
    path = tmp_path / "registrations.json"
    path.write_text(json.dumps({"version": 1, "registrations": [good | {"profile_id": "p1"} | entry]}))
    assert refusal(lambda: Store(path).load())[0] == "ownership_unreadable"


def test_an_ownership_record_keeps_its_snapshot_as_a_tuple(tmp_path):
    store = Store(tmp_path / "registrations.json")
    store.save({EXE_A: Record(EXE_A, 2, NAME_A, TARGET_A, "default", None, ("m1", "m2"))})
    assert store.load()[EXE_A].before == ("m1", "m2")
    assert json.loads(store.path.read_text())["registrations"][0]["before"] == ["m1", "m2"]


def test_an_ownership_path_that_cannot_be_read_is_unreadable(tmp_path):
    (tmp_path / "registrations.json").mkdir()
    assert refusal(lambda: Store(tmp_path / "registrations.json").load())[0] == "ownership_unreadable"


def test_the_store_lock_is_exclusive_and_private(tmp_path):
    store = Store(tmp_path / "home" / "registrations.json")
    lock = tmp_path / "home" / ".registrations.json.lock"
    with store.locked():
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        other = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    other = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(other)


def test_retirement_runs_in_execution_order(tmp_path):
    registrations, herdr, _ = world(tmp_path)
    ids = [f"exe-{letter * 32}" for letter in "fedcba9"]
    registrations.store.save({exe: Record(exe, 1, exe, f"ssh://{exe}", "default", None) for exe in ids})
    assert registrations.reconcile([], Verdicts()).retired == sorted(ids)


def test_withdraw_removes_only_a_recorded_registration(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    registrations.withdraw(desired[0])
    assert herdr.commands == []
    profile = registrations.register(desired[0])
    registrations.withdraw(desired[0])
    assert herdr.commands[-1] == ["herdr", "machine", "remove", profile]
    assert registrations.store.load() == {}
    assert registrations.drift == {"missing": 1, "retired": 1}


def test_a_managed_profile_on_another_target_is_replaced(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    profile = registrations.register(desired[0])
    herdr.machines[0]["target"] = "ssh://old-namespace"
    replaced = registrations.register(desired[0])
    assert replaced != profile
    assert ["herdr", "machine", "remove", profile] in herdr.commands
    assert [m["target"] for m in herdr.machines] == [TARGET_A]
    assert registrations.drift == {"missing": 1, "moved": 1}


def test_the_store_syncs_the_record_before_and_its_folder_after_the_replace(tmp_path, monkeypatch):
    store = Store(tmp_path / "registrations.json")
    seen = []

    def fsync(handle: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{handle}"))
        seen.append((path, path.read_text() if path.is_file() else None, store.path.exists()))

    monkeypatch.setattr(reconcile.os, "fsync", fsync)
    store.save({})
    assert seen == [
        (tmp_path / ".registrations.json.tmp", '{\n  "version": 1,\n  "registrations": []\n}\n', False),
        (tmp_path, None, True),
    ]


def test_a_verified_registration_survives_an_unreachable_probe(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    first = registrations.reconcile(desired[:1], Verdicts())
    later = registrations.reconcile(desired[:1], Verdicts(Unreachable("x")))
    assert (later.registered, later.unverified, later.refused) == (first.registered, [EXE_A], {})
    assert first.unverified == []
    assert herdr.mutating()[-1][:3] == ["herdr", "machine", "add"]


def test_recorded_names_the_committed_profile_only(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    assert registrations.recorded(desired[0]) is None
    herdr.unanswered.add(TARGET_A)
    refusal(lambda: registrations.register(desired[0]))
    assert registrations.recorded(desired[0]) is None
    herdr.unanswered.clear()
    assert registrations.recorded(desired[0]) is None
    assert registrations.recorded(desired[0]) != registrations.register(desired[0])
    assert registrations.recorded(desired[0]) == herdr.machines[0]["id"]


def test_every_label_and_target_collision_is_counted(tmp_path):
    desired = cases.endpoints()
    listed = [machine("m1", desired[0].name, "ssh://devbox"), machine("m2", "devbox", desired[1].target)]
    registrations, herdr, _ = world(tmp_path, listed)
    for endpoint in desired + desired:
        refusal(lambda endpoint=endpoint: registrations.register(endpoint))
    assert registrations.drift == {"missing": 4, "collision": 4}
    assert herdr.mutating() == []


def test_every_moved_profile_is_counted(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    registrations.register(desired[0])
    for old_target in ("ssh://old-1", "ssh://old-2"):
        herdr.machines[0]["target"] = old_target
        registrations.register(desired[0])
    assert registrations.drift == {"missing": 1, "moved": 2}


def test_every_relabelled_profile_is_counted(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    registrations.register(desired[0])
    for label in ("by-hand-1", "by-hand-2"):
        herdr.machines[0]["label"] = label
        registrations.register(desired[0])
    assert registrations.drift == {"missing": 1, "label": 2}


def test_every_adopted_interrupted_add_is_counted(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    herdr.unanswered.update(endpoint.target for endpoint in desired)
    first = registrations.reconcile(desired, Verdicts())
    herdr.unanswered.clear()
    second = registrations.reconcile(desired, Verdicts())
    assert first.refused == {EXE_A: "catalog_unreachable", EXE_B: "catalog_unreachable"}
    assert (second.drift, sorted(second.registered)) == ({"interrupted": 2}, [EXE_A, EXE_B])


def test_an_endpoint_without_a_grant_or_operator_scope_is_refused(tmp_path):
    registrations, herdr, desired = world(tmp_path)
    scope = desired[0].scope
    controller = Endpoint(Scope("controller", scope.swarm, scope.execution_id, scope.generation), "swarm-workers")
    assert refusal(lambda: registrations.register(controller)) == (
        "unauthorized",
        f"{NAME_A} has no launch grant or operator scope",
    )
    assert herdr.commands == [["herdr", "machine", "list", "--json"]]
    assert not registrations.store.path.exists()


@pytest.mark.parametrize("actor", ["grant/lgr-1", "operator/nestor"])
def test_grant_and_operator_scopes_may_register(tmp_path, actor):
    registrations, herdr, desired = world(tmp_path)
    scope = desired[0].scope
    endpoint = Endpoint(Scope(actor, scope.swarm, scope.execution_id, scope.generation), "swarm-workers")
    assert registrations.register(endpoint) == herdr.machines[0]["id"]
