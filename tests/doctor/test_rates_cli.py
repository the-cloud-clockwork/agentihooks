import json
import time

import pytest

from scripts.doctor import cli as doctor
from scripts.doctor import rates, rates_read
from scripts.swarm import cli as swarm_cli
from tests.doctor.test_doctor_cli import DOCTOR, WATCHED, env  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
HOUR = 3_600_000
NOW = 1_791_295_200_000
AT = "2026-10-06T10:00Z"
AT_MS = 1_791_280_800_000


@pytest.fixture
def loaded(env, monkeypatch):  # noqa: F811
    seen = {}

    def load(store, ledger, slug, span):
        seen.update(slug=slug, span=span, store=store, ledger=ledger)
        events = [
            {"at": AT_MS - HOUR, "kind": "comment added", "target": "phases/p1", "by": "sw-eng-1"},
            {"at": AT_MS + HOUR, "kind": "task done", "target": "tasks/t1", "by": "swarm"},
        ]
        log = [{"gate": "talk", "kind": "deny", "at": AT_MS + HOUR}]
        tasks = {"t1": {"kind": "ops", "proof": {"command": "check", "output": "verified"}}}
        return rates.Records(events, tasks, {}, {}, log, [], [], {})

    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    monkeypatch.setattr(rates_read, "load", load)
    return seen


def test_rates_prints_the_window_before_and_after_a_time_as_json(loaded, env, capsys):  # noqa: F811
    assert doctor.main([WATCHED, "rates", "--at", AT, "--hours", "2", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["windows"] == {
        "before": {"start": AT_MS - 2 * HOUR, "end": AT_MS},
        "after": {"start": AT_MS, "end": AT_MS + 2 * HOUR},
    }
    assert loaded["slug"] == WATCHED
    assert loaded["span"] == rates.Window(AT_MS - 2 * HOUR, AT_MS + 2 * HOUR)
    assert loaded["store"] is env[0]
    assert isinstance(loaded["ledger"], swarm_cli.LedgerClient)
    assert found["rates"]["before"]["ceremony"] == {"talk writes": 1, "outcomes": 0, "talk per outcome": None}
    assert found["rates"]["after"]["ceremony"] == {"talk writes": 0, "outcomes": 1, "talk per outcome": 0.0}
    assert found["gates"] == {
        "before": {},
        "after": {"talk": {"deny": 1, "observe": 0, "lift": 0, "fail-open": 0, "count": 0}},
    }
    assert found["events from"] == AT_MS - HOUR


def test_rates_prints_both_windows_as_a_table_with_zero_for_a_gate_quiet_in_one(loaded, capsys):
    assert doctor.main([WATCHED, "rates", "--at", AT, "--hours", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:4] == [
        "window before 2026-10-06T08:00Z to 2026-10-06T10:00Z",
        "window after 2026-10-06T10:00Z to 2026-10-06T12:00Z",
        "ledger events kept from 2026-10-06T09:00Z",
        "",
    ]
    assert lines[4].split() == ["failure", "number", "before", "after"]
    assert all(line == line.rstrip() for line in lines)
    rows = {tuple(line.split()[:-2]): line.split()[-2:] for line in lines[5:]}
    assert rows[("ceremony", "talk", "per", "outcome")] == ["none", "0.0"]
    assert rows[("gate", "talk", "deny")] == ["0", "1"]
    assert rows[("gate", "talk", "lift")] == ["0", "0"]


def test_rates_takes_a_window_under_an_hour(loaded, capsys):
    assert doctor.main([WATCHED, "rates", "--hours", "0.5", "--json"]) == 0
    assert loaded["span"] == rates.Window(NOW - HOUR // 2, NOW)


def test_watched_maps_a_doctor_swarm_with_no_watched_swarm_left_to_its_name_without_the_suffix():
    class Config:
        template = doctor.TEMPLATE

    class Store:
        def slugs(self):
            return [f"gone{doctor.SUFFIX}"]

        def config(self, slug):
            return Config()

    assert doctor._watched(Store(), f"gone{doctor.SUFFIX}") == "gone"
    assert doctor._watched(Store(), "plain") == "plain"


def test_rates_without_a_time_prints_one_window_ending_now_as_a_table(loaded, capsys):
    assert doctor.main([WATCHED, "rates"]) == 0
    out = capsys.readouterr().out
    assert loaded["span"] == rates.Window(NOW - 24 * HOUR, NOW)
    lines = out.splitlines()
    assert lines[0] == "window now 2026-10-05T14:00Z to 2026-10-06T14:00Z"
    assert lines[1] == "ledger events kept from 2026-10-06T09:00Z"
    assert lines[3].split() == ["failure", "number", "now"]
    assert "talk writes" in out and "premature completion" in out
    assert [line.split()[-1] for line in lines if line.startswith("ceremony")] == ["1", "1", "1.0"]
    assert [line.split()[-2:] for line in lines if line.startswith("gate talk")] == [
        ["deny", "1"],
        ["observe", "0"],
        ["lift", "0"],
        ["fail-open", "0"],
        ["count", "0"],
    ]


def test_rates_on_a_doctor_ledger_measures_the_swarm_it_watches(loaded, env, capsys):  # noqa: F811
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    assert doctor.main([DOCTOR, "rates", "--json"]) == 0
    assert loaded["slug"] == WATCHED


def test_rates_on_a_swarm_without_a_doctor_measures_it(loaded, capsys):
    assert doctor.main(["other-swarm", "rates", "--json"]) == 0
    assert loaded["slug"] == "other-swarm"


@pytest.mark.parametrize(
    "flags,error",
    [
        (["--at", "yesterday"], "--at takes an ISO time such as 2026-10-06T12:00Z, not yesterday"),
        (["--at", "2026-10-06T14:00Z"], "--at must be in the past: the after window ends now"),
        (["--at", "2030-01-01T00:00"], "--at must be in the past: the after window ends now"),
        (["--hours", "0"], "--hours must be more than zero"),
    ],
)
def test_rates_refuses_a_bad_window(loaded, capsys, flags, error):
    assert doctor.main([WATCHED, "rates", *flags]) == 1
    assert capsys.readouterr().err == f"doctor: {error}\n"
    assert loaded == {}


def test_a_time_without_a_zone_is_read_as_utc():
    assert doctor._at_ms("2026-10-06T10:00") == AT_MS == doctor._at_ms("2026-10-06T12:00+02:00")


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_read_and_print_as_utc_whatever_the_host_zone(berlin):
    assert doctor._at_ms("2026-10-06T10:00") == AT_MS
    assert rates._when(AT_MS) == "2026-10-06T10:00Z"


def test_the_after_window_never_runs_past_now():
    assert rates.windows(NOW, 24, NOW - HOUR) == {
        "before": rates.Window(NOW - 25 * HOUR, NOW - HOUR),
        "after": rates.Window(NOW - HOUR, NOW),
    }
    assert rates.windows(NOW, 1.5) == {"now": rates.Window(NOW - 90 * 60_000, NOW)}


def test_a_report_with_no_events_has_no_start_and_prints_none():
    found = rates.report(rates.Records([], {}, {}, {}, [], [], [], {}), {"now": rates.Window(0, HOUR)})
    assert found["events from"] is None
    assert "ledger events kept from none" in rates.table(found)
