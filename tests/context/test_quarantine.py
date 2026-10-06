"""Correction quarantine: proposals wait for the operator, a confirmed correction withholds its directive."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import conditions, enforcement, injection_trace, operator_words, quarantine, trace_sweep
from scripts.trace_cli import main as trace

SID = "sess-quarantine-1"
TEXT = "Never run the test suite on this machine, the shared runner owns it"
OTHER = "Keep every pull request small and focused on one change"
SAID = "please confirm the correction on the test rule"
AGENT = "engineer@a1b2c3-0001"


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    for name in (
        "AGENTIHOOKS_SWARM",
        "AGENTIHOOKS_AGENT_NAME",
        "AGENTIHOOKS_SWARM_TASK",
        "AGENTIHOOKS_GATE_QUARANTINE",
        "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def filed():
    found = []
    with patch.object(trace_sweep, "_file_followup", side_effect=lambda *args: found.append(args)):
        yield found


@pytest.fixture()
def swarm(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", AGENT)


def _received(source="bad-1", text=TEXT, layer="bundle", locator=None):
    injection_trace.record(SID, layer, source, text, locator or {"store": "/s", "id": source})


def _wrong(source="bad-1"):
    return trace([SID, "--wrong", source, "--repo", "/repos/qitp", "--reason", "it belongs to qitp"])


def _confirmed(source="bad-1", text=TEXT, layer="bundle", locator=None, at=None):
    _received(source, text, layer, locator)
    with patch.object(injection_trace, "_now", return_value=at or injection_trace._now()):
        return injection_trace.correct(SID, source, "/repos/qitp", "it belongs to qitp")


def _entries():
    return [
        {"id": "bad-1", "message": TEXT, "cadence": 5, "source": "bundle"},
        {"id": "keep-1", "message": OTHER, "cadence": 5, "source": "bundle"},
    ]


def _start(session=SID, entries=None):
    with patch.object(enforcement, "load_all_enforcements", return_value=entries or _entries()):
        return enforcement.get_session_start_enforcements(session) or ""


def _withheld(session=SID):
    return [row for row in injection_trace.trace(session) if row["layer"] == quarantine.WITHHELD]


def _held_sources():
    return [h.correction["source"] for h in quarantine.index()]


@pytest.mark.parametrize(
    "value,mode",
    [(None, "enforce"), (" Observe ", "observe"), ("off", "off"), ("loud", "enforce"), ("enforce", "enforce")],
)
def test_mode_defaults_to_enforce_and_reads_the_switch(value, mode):
    assert quarantine.mode({} if value is None else {"AGENTIHOOKS_GATE_QUARANTINE": value}) == mode


def test_an_agent_correction_in_a_swarm_is_proposed_and_raised(swarm, filed, capsys):
    _received()

    assert _wrong() == 0

    (row,) = injection_trace.corrections()
    assert row["status"] == quarantine.PROPOSED
    assert filed == [
        (
            "sw",
            "A correction waits for your confirmation: an agent marked a bundle directive wrong for the qitp repo: "
            "it belongs to qitp. It stays in force until you tell the master to confirm the correction.",
            "--needs-operator",
        )
    ]
    assert "raised in the operator's Priorities on sw" in capsys.readouterr().out
    assert quarantine.index() == []
    assert "bad-1" in _start("sess-later")


def test_a_correction_outside_a_swarm_is_confirmed_at_once(filed):
    _received()

    assert _wrong() == 0

    assert "status" not in injection_trace.corrections()[0]
    assert filed == []
    assert _held_sources() == ["bad-1"]


def test_a_direct_correction_carries_no_status():
    _received()
    assert "status" not in injection_trace.correct(SID, "bad-1", "/r", "why")


def test_a_failed_raise_keeps_the_proposal_and_says_so(swarm, capsys):
    _received()
    with patch.object(trace_sweep, "_file_followup", side_effect=OSError("ledger down")):
        assert _wrong() == 0
    assert "the priority was not raised (ledger down)" in capsys.readouterr().out
    assert injection_trace.corrections()[0]["status"] == quarantine.PROPOSED


def test_an_agent_cannot_confirm_its_own_proposal(swarm, filed, capsys):
    _received()
    _wrong()
    capsys.readouterr()

    with patch("hooks.context.ledger_request.find", return_value=None):
        assert trace(["confirm", "bad-1", "--quote", "confirm the correction now"]) == 2

    assert "only the operator confirms a correction" in capsys.readouterr().err
    assert quarantine.index() == []


def test_the_operators_quoted_words_confirm_and_run_the_sweep(swarm, filed, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-x")
    _received()
    _wrong()
    operator_words.record(AGENT, SAID)
    report = {"plan": [], "applied": [], "closed": [], "proposed": []}

    with patch.object(trace_sweep, "sweep", return_value=report) as sweep:
        assert trace(["confirm", "bad-1", "--quote", "confirm the correction"]) == 0

    sweep.assert_called_once_with(str(Path.home() / "dev"), apply=True, session_id="sid-x", ledger="sw", only="bad-1")
    assert _held_sources() == ["bad-1"]
    (entry,) = injection_trace._read(injection_trace._home() / "injection_corrections_confirmed.jsonl")
    assert set(entry) == {"at", "correction", "by", "words"}
    assert (entry["by"], entry["words"]) == ("words", "confirm the correction")


def test_confirm_closes_what_the_sweep_no_longer_finds(filed, swarm, tmp_path, capsys):
    _received()
    _wrong()
    operator_words.record(AGENT, SAID)
    capsys.readouterr()
    with patch.object(trace_sweep, "find", return_value=[]):
        assert trace(["confirm", "bad-1", "--quote", "confirm the correction", "--root", str(tmp_path)]) == 0
    assert "closed\tbad-1" in capsys.readouterr().out
    assert _held_sources() == []


def test_confirm_takes_only_the_named_source(swarm, filed):
    _received()
    _received("other-1", OTHER)
    _wrong()
    _wrong("other-1")
    operator_words.record(AGENT, SAID)
    with patch.object(trace_sweep, "find", return_value=[trace_sweep.Hit("x", "y", "z", "k", "held")]):
        assert trace(["confirm", "bad-1", "--quote", "confirm the correction"]) == 0
    keys = quarantine.confirmed_keys()
    proposed = [row["source"] for row in injection_trace.corrections() if quarantine.is_proposed(row, keys)]
    assert proposed == ["other-1"]


def test_confirm_outside_a_swarm_records_empty_words():
    _received()
    with patch.object(injection_trace, "_now", return_value="2026-10-06T00:00:00Z"):
        injection_trace.correct(SID, "bad-1", "/repos/qitp", "why", status=quarantine.PROPOSED)
    with patch.object(trace_sweep, "find", return_value=[]):
        assert trace(["confirm", "bad-1"]) == 0
    (entry,) = injection_trace._read(quarantine._confirmations_path())
    assert (entry["by"], entry["words"]) == ("operator", "")


@pytest.mark.parametrize("verb", ["confirm", "release"])
def test_each_verb_needs_its_source_and_names_itself(verb, capsys):
    with pytest.raises(SystemExit):
        trace([verb])
    assert f"usage: agentihooks trace {verb}" in capsys.readouterr().err


def test_an_operator_comment_on_the_task_confirms(swarm, filed):
    _received()
    _wrong()
    with (
        patch(
            "hooks.context.ledger_request.find", side_effect=lambda asks, env: ("ledger", "c1") if asks(SAID) else None
        ),
        patch.object(trace_sweep, "find", return_value=[trace_sweep.Hit("bad-1", "x", "y", "z", "held")]),
    ):
        assert trace(["confirm", "bad-1"]) == 0
    assert _held_sources() == ["bad-1"]
    assert injection_trace._read(quarantine._confirmations_path())[0]["by"] == "ledger"


def test_confirm_needs_a_waiting_proposal(capsys):
    assert trace(["confirm", "bad-1"]) == 2
    assert "no proposed correction of bad-1 waits" in capsys.readouterr().err


@pytest.mark.parametrize(
    "words,ok",
    [
        ("confirm the correction", True),
        ("CONFIRMED, the Correction stands", True),
        ("confirm the tests pass", False),
        ("the correction is wrong", False),
        ("", False),
    ],
)
def test_only_words_naming_confirm_and_correction_count(words, ok):
    assert quarantine._asks("confirm")(words) is ok


def test_short_quotes_never_count(swarm):
    operator_words.record(AGENT, "confirm correction")
    with patch("hooks.context.ledger_request.find", return_value=None):
        assert quarantine.operator_said("confirm", "confirm correction") == ""
        assert quarantine.operator_said("confirm", "confirm the correction") == ""
    assert quarantine.operator_said("confirm", "", {}) == "operator"


def test_the_ledger_lookup_reads_the_given_environment():
    env = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_TASK": "t1"}
    with patch(
        "hooks.context.ledger_request.find", side_effect=lambda asks, given: ("relay", "c2") if given is env else None
    ):
        assert quarantine.operator_said("confirm", "", env) == "relay"


def test_release_needs_the_operator_and_records_who(swarm, capsys):
    with patch("hooks.context.ledger_request.find", return_value=None):
        assert trace(["release", "bundle/rules/tests.md", "--quote", "release the correction"]) == 2
    assert "only the operator releases a correction" in capsys.readouterr().err
    operator_words.record(AGENT, "go ahead and release the correction on tests")

    assert trace(["release", "bundle/rules/tests.md", "--quote", "release the correction"]) == 0

    assert capsys.readouterr().out == "released\tbundle/rules/tests.md\n"
    (entry,) = injection_trace._read(injection_trace._home() / "injection_corrections_released.jsonl")
    assert {k: entry[k] for k in ("source", "by", "words")} == {
        "source": "bundle/rules/tests.md",
        "by": "words",
        "words": "release the correction",
    }
    assert entry["at"]


def test_sweep_apply_leaves_a_proposed_correction_alone(swarm, filed):
    _received()
    _wrong()
    hit = trace_sweep.Hit("bad-1", "enforcement", "/s", "bad-1", "clear")
    with patch.object(trace_sweep, "find", return_value=[hit]), patch.object(trace_sweep, "_clear") as clear:
        report = trace_sweep.sweep("/nowhere", apply=True)
    clear.assert_not_called()
    assert report["applied"] == [] and report["closed"] == []
    assert [row["source"] for row in report["proposed"]] == ["bad-1"]
    assert "proposed\tbad-1\t/repos/qitp\twaits for the operator to confirm" in trace_sweep.plan_rows(report)


def test_sweep_plans_only_unless_asked_to_apply():
    _confirmed()
    hit = trace_sweep.Hit("bad-1", "enforcement", "/s", "bad-1", "clear")
    with patch.object(trace_sweep, "find", return_value=[hit]), patch.object(trace_sweep, "_clear") as clear:
        report = trace_sweep.sweep("/nowhere")
    clear.assert_not_called()
    assert report["plan"] == [hit] and report["closed"] == []


def test_sweep_without_a_ledger_leaves_the_followup_to_file():
    _confirmed()
    hit = trace_sweep.Hit("bad-1", "rule", "/r/bundle/x.md", "bad-1", "pr", "/r/bundle", "x.md")
    with patch.object(trace_sweep, "find", side_effect=[[hit], [hit]]):
        report = trace_sweep.sweep("/nowhere", apply=True)
    ((_, outcome),) = report["applied"]
    assert "follow up to file: One rule directive in the bundle repo" in outcome


def test_sweep_only_touches_the_named_source():
    _confirmed()
    _confirmed("other-1", OTHER)
    with patch.object(trace_sweep, "find", return_value=[]):
        report = trace_sweep.sweep("/nowhere", only="other-1")
    assert [row["source"] for row in report["closed"]] == ["other-1"]


def test_a_confirmed_enforcement_is_withheld_and_logged_once():
    _confirmed()

    first, second = _start("sess-a"), _start("sess-a")

    assert "bad-1" not in first and "keep-1" in first
    assert "bad-1" not in second
    (row,) = _withheld("sess-a")
    assert row["source"] == "bad-1"
    key = quarantine._key(injection_trace.corrections()[0])
    assert row["locator"] == {"layer": "enforcement", "correction": key, "mode": "enforce"}
    assert row["text"] == "withheld under the correction of bad-1: it belongs to qitp"


def test_an_enforcement_is_withheld_by_its_id_alone():
    _confirmed()
    entries = [{"id": "bad-1", "message": "reworded since the correction", "cadence": 5}]
    assert _start("sess-id", entries) == ""
    assert [row["source"] for row in _withheld("sess-id")] == ["bad-1"]


def test_the_same_text_under_another_id_is_withheld_too():
    _confirmed()
    entries = [{"id": "copy-9", "message": f"Note: {TEXT}.", "cadence": 5}]
    assert _start("sess-b", entries) == ""
    assert [row["source"] for row in _withheld("sess-b")] == ["copy-9"]


def test_a_rule_enforcement_is_matched_by_its_path():
    path = "/bundle/rules/never-run-the-suite-here.md"
    _confirmed("rule-1", path)
    entries = [{"id": "rule-2", "type": "rule", "path": path, "cadence": 5}, _entries()[1]]
    out = _start("sess-r", entries)
    assert "rule-2" not in out and "keep-1" in out


def test_observe_mode_logs_and_still_injects(monkeypatch):
    _confirmed()
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "observe")

    assert "bad-1" in _start("sess-c")

    (row,) = _withheld("sess-c")
    assert row["locator"]["mode"] == "observe"


def test_off_mode_neither_withholds_nor_logs(monkeypatch):
    _confirmed()
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "off")

    assert "bad-1" in _start("sess-d")
    assert _withheld("sess-d") == []


def test_a_closed_correction_releases_its_directive():
    row = _confirmed()
    trace_sweep.close(row)
    assert "bad-1" in _start("sess-e")


def test_no_corrections_file_means_no_index():
    assert quarantine.index() == []
    assert quarantine.digest() == ""


@pytest.fixture()
def broadcasts(tmp_path):
    path = tmp_path / "broadcast.json"
    stamp = {"created_at": "2026-10-05T00:00:00Z", "ttl_seconds": 3600, "expires_at": "2999-01-01T00:00:00Z"}
    messages = [
        {"id": "bc-keep", "message": OTHER, "severity": "critical", "persistent": True, "source": "op", **stamp},
        {
            "id": "bc-brain-2",
            "message": f"[Operator Intent]\n{TEXT}",
            "severity": "critical",
            "persistent": True,
            "source": "brain-adapter",
            "origin": {"id": "operator-intent"},
            **stamp,
        },
    ]
    path.write_text(json.dumps(messages))
    with patch("hooks.context.broadcast._broadcast_path", return_value=path):
        yield path


def test_a_regenerated_brain_broadcast_is_withheld_by_its_origin(broadcasts):
    from hooks.context import broadcast

    _confirmed("bc-brain-1", "unrelated words that never match anything here", "brain", {"id": "operator-intent"})

    for getter in (
        broadcast.get_pending_broadcasts,
        broadcast.get_critical_broadcasts,
        broadcast.get_unseen_broadcasts,
        broadcast.get_pretool_broadcasts,
    ):
        assert [m["id"] for m in getter("sess-f")] == ["bc-keep"]
    (row,) = _withheld("sess-f")
    assert (row["source"], row["locator"]["layer"]) == ("bc-brain-2", "broadcast")


def test_a_delivered_persistent_broadcast_is_withheld_on_the_pretool_path(broadcasts):
    from hooks.context import broadcast

    messages = json.loads(broadcasts.read_text())
    for message in messages:
        message["delivered_to"] = ["sess-p"]
    broadcasts.write_text(json.dumps(messages))
    _confirmed("bc-brain-2", TEXT, "brain", {"id": "operator-intent"})

    with patch.object(broadcast, "BROADCAST_CRITICAL_ON_PRETOOL", True):
        assert [m["id"] for m in broadcast.get_pretool_broadcasts("sess-p")] == ["bc-keep"]
    assert [row["source"] for row in _withheld("sess-p")] == ["bc-brain-2"]


def _merge(session, *runs):
    entries = [({"file": name, "path": f"/x/{name}"}, {"returncode": 0, "stdout": out}) for name, out in runs]
    payload = {"tool_name": "Bash", **({"session_id": session} if session else {})}
    return conditions.merge("pre", payload, entries)


def test_a_condition_context_under_correction_is_dropped():
    _confirmed("pre-bash.guard.sh", "mind the runner", "condition", {"file": "/x/pre-bash.guard.sh"})

    result = _merge("sess-g", ("pre-bash.guard.sh", "mind the runner"), ("pre-bash.other.sh", "fine"))

    assert result.contexts == ["[condition pre-bash.other.sh]\nfine"]
    (row,) = _withheld("sess-g")
    assert (row["source"], row["locator"]["layer"]) == ("pre-bash.guard.sh", "condition")
    kept = [row for row in injection_trace.trace("sess-g") if row["layer"] == "condition"]
    assert [(row["source"], row["text"]) for row in kept] == [("pre-bash.other.sh", "fine")]


def test_a_condition_context_is_matched_by_its_text():
    _confirmed()
    result = _merge("sess-h", ("pre-bash.renamed.sh", f"Remember: {TEXT}"))
    assert result.contexts == []
    assert [row["source"] for row in _withheld("sess-h")] == ["pre-bash.renamed.sh"]


def test_a_payload_without_a_session_writes_no_trace():
    _confirmed()
    before = sorted(p.name for p in (injection_trace._home() / "injections").iterdir())
    assert _merge("", ("pre-bash.renamed.sh", TEXT)).contexts == []
    assert sorted(p.name for p in (injection_trace._home() / "injections").iterdir()) == before


def _file_correction(tmp_path, quote=None, reason="it belongs to qitp", at=None):
    repo = tmp_path / "bundle"
    rules = repo / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "tests.md").write_text(f"# Tests\n\n{TEXT}\nAnd keep going.\n")
    locator = {"repo": str(repo), "path": "rules/tests.md", "blob": "x"}
    injection_trace.record(SID, "rule", "bundle/rules/tests.md", "# Tests", locator)
    with patch.object(injection_trace, "_now", return_value=at or injection_trace._now()):
        row = injection_trace.correct(SID, "bundle/rules/tests.md", "/repos/qitp/", reason, quote or "")
    return rules / "tests.md", row


NOTICE = "> CORRECTION: the passage above is marked wrong for the qitp repo: it belongs to qitp. Do not follow it."


def test_render_puts_a_notice_after_the_quoted_passage(tmp_path):
    path, _ = _file_correction(tmp_path, "the test suite on this machine")

    out = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")

    assert out == f"# Tests\n\n{TEXT}\n\n{NOTICE}\n\nAnd keep going.\n"
    assert quarantine.annotate("nothing quoted here\n", "") == "nothing quoted here\n"


def test_a_passage_split_across_lines_and_at_the_end_is_found(tmp_path):
    _file_correction(tmp_path, "the test suite on this machine")
    out = quarantine.annotate("Never run the test\nsuite on this machine", "")
    assert out == f"Never run the test\nsuite on this machine\n\n{NOTICE}\n"


def test_an_unquoted_file_correction_heads_the_file(tmp_path):
    path, _ = _file_correction(tmp_path)

    out = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")

    assert out == (
        "> CORRECTION: this file is marked wrong for the qitp repo: it belongs to qitp. Do not follow it.\n\n"
        + path.read_text()
    )
    assert quarantine.annotate("other file\n", "bundle/rules/other.md") == "other file\n"


def test_a_later_file_correction_is_annotated_after_another_layer(tmp_path):
    _confirmed()
    path, _ = _file_correction(tmp_path, "the test suite on this machine")
    assert NOTICE in quarantine.annotate(path.read_text(), "bundle/rules/tests.md")


def test_observe_mode_leaves_the_render_alone(tmp_path, monkeypatch):
    path, _ = _file_correction(tmp_path, "the test suite on this machine")
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "observe")
    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") == path.read_text()


HELD = (
    "> CORRECTION: bundle/rules/tests.md was marked wrong more than once and is held whole until the operator "
    "releases it.\n"
)


def test_a_source_confirmed_twice_is_held_whole_until_released(tmp_path):
    path, first = _file_correction(tmp_path, "the test suite on this machine", at="2026-10-06T00:00:00Z")
    trace_sweep.close(first)
    _file_correction(tmp_path, "And keep going.", "still wrong", at="2026-10-06T00:00:01Z")

    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") == HELD
    assert _held_sources() == ["bundle/rules/tests.md", "bundle/rules/tests.md"]

    quarantine.release("bundle/rules/tests.md", "operator", "")
    released = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")
    assert "And keep going.\n\n> CORRECTION" in released
    assert "the test suite on this machine\n\n> CORRECTION" not in released


def test_two_new_corrections_after_a_release_hold_the_source_again(tmp_path):
    path, _ = _file_correction(tmp_path, "the test suite on this machine", at="2026-10-06T00:00:00Z")
    with patch.object(injection_trace, "_now", return_value="2026-10-06T00:00:01Z"):
        quarantine.release("bundle/rules/tests.md", "operator", "")
    _file_correction(tmp_path, "And keep going.", at="2026-10-06T00:00:01Z")
    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") != HELD
    _file_correction(tmp_path, "# Tests", at="2026-10-06T00:00:02Z")
    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") != HELD
    _file_correction(tmp_path, "# Tests", at="2026-10-06T00:00:03Z")
    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") == HELD


def test_the_digest_moves_with_each_confirmed_correction(swarm, filed):
    _received()
    _wrong()
    assert quarantine.digest() == ""
    quarantine.confirm(injection_trace.corrections(), "operator", "")
    one = quarantine.digest()
    _confirmed("other-1", OTHER, at="2999-01-01T00:00:00Z")
    assert len(one) == 12 and quarantine.digest() not in ("", one)


def test_the_sweep_plans_a_pull_request_for_a_file_still_holding_the_passage(tmp_path):
    path, row = _file_correction(tmp_path, "the test suite on this machine")

    (hit,) = trace_sweep._file_source_hits(row)
    assert hit == trace_sweep.Hit(
        "bundle/rules/tests.md", "rule", str(path), "bundle/rules/tests.md", "pr", str(path.parent), "tests.md"
    )
    assert trace_sweep._file_source_hits({**row, "layer": "bundle"}) == []
    assert trace_sweep._file_source_hits({**row, "locator": {}}) == []

    path.write_text("# Tests\n\nAnd keep going.\n")
    assert trace_sweep._file_source_hits(row) == []
    assert len(trace_sweep._file_source_hits({**row, "quote": ""})) == 1
    path.unlink()
    assert trace_sweep._file_source_hits(row) == []


def test_a_pull_request_hit_files_a_plain_followup():
    texts = []
    hit = trace_sweep.Hit("x", "rule", "/r/bundle/rules/t.md", "x", "pr", "/r/bundle", "rules/t.md")
    with patch.object(trace_sweep, "_file_followup", side_effect=lambda ledger, text: texts.append((ledger, text))):
        outcome = trace_sweep._apply_one(hit, {"repo": "/repos/qitp", "reason": "wrong"}, SID, "sw")
    assert outcome.endswith("follow up filed on sw")
    assert texts == [
        (
            "sw",
            "One rule directive in the bundle repo is marked wrong for the qitp repo: wrong. "
            "Remove it by pull request into dev.",
        )
    ]


def test_followups_go_through_the_ledger_cli_as_this_agent(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", AGENT)
    with patch.object(subprocess, "run") as run:
        trace_sweep._file_followup("sw", "plain words", "--needs-operator")
    run.assert_called_once_with(
        ["agentihooks", "ledger", "--slug", "sw", "--as", AGENT, "followup", "add", "plain words", "--needs-operator"],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _priming_correction(layer="learned", text="Push straight to dev because it is faster", number=1):
    source = f"{layer}:eng-2@sw#{number}"
    locator = {"seat": "eng-2@sw", "note": number} if layer == "learned" else {"swarm": "sw", "line": number}
    injection_trace.record(SID, layer, source, text, locator)
    return injection_trace.correct(SID, source, "/repos/qitp", "never push to dev")


class _Store:
    def __init__(self, notes=None, culture=None):
        self.asked = []
        store = self

        class Memory:
            def learned(self, seat):
                store.asked.append(seat)
                return [{"text": t} for t in (notes or {}).get(seat, [])]

        class Culture:
            def get(self, slug):
                store.asked.append(slug)
                return (culture or {}).get(slug, "")

        self.memory, self.culture = Memory(), Culture()


def test_the_sweep_holds_a_priming_correction_while_its_note_lives():
    row = _priming_correction()
    store = _Store({"eng-2@sw": ["Remember: Push straight to dev because it is faster, always"]})
    with patch("scripts.swarm.store.connect", return_value=store):
        (hit,) = trace_sweep._priming_hits(row)
    source = "learned:eng-2@sw#1"
    assert hit == trace_sweep.Hit(source, "learned", source, source, "held")
    assert store.asked == ["eng-2@sw"]
    assert trace_sweep._apply_one(hit, row, SID, "") == "withheld from priming until its source is fixed"
    with patch("scripts.swarm.store.connect", return_value=_Store({"eng-2@sw": ["something else entirely"]})):
        assert trace_sweep._priming_hits(row) == []


def test_the_sweep_finds_a_short_culture_line_by_its_whole_text():
    row = _priming_correction("culture", "- merge fast", 2)
    store = _Store(culture={"sw": "# How\n- merge fast\n"})
    with patch("scripts.swarm.store.connect", return_value=store):
        assert len(trace_sweep._priming_hits(row)) == 1
    assert store.asked == ["sw"]
    with patch("scripts.swarm.store.connect", return_value=_Store(culture={"sw": "# How\n- merge fast now\n"})):
        assert trace_sweep._priming_hits(row) == []


def test_find_reaches_the_priming_layers(tmp_path):
    row = _priming_correction()
    store = _Store({"eng-2@sw": ["Remember: Push straight to dev because it is faster, always"]})
    with (
        patch("scripts.swarm.store.connect", return_value=store),
        patch.object(trace_sweep, "_stores", return_value=([], [])),
        patch.object(trace_sweep.broadcast, "list_broadcasts", return_value=[]),
    ):
        assert [hit.action for hit in trace_sweep.find(row, tmp_path)] == ["held"]


def test_an_unreadable_store_keeps_the_correction_open():
    row = _priming_correction()
    with patch("scripts.swarm.store.connect", side_effect=ConnectionError("no redis")):
        assert len(trace_sweep._priming_hits(row)) == 1
    assert trace_sweep._priming_hits({**row, "layer": "bundle"}) == []


def test_patch_refusal_names_the_open_correction():
    _confirmed()
    assert quarantine.patch_refusal(f"Remember: {TEXT} because") == (
        "this text carries a directive under correction (bad-1: it belongs to qitp); fix it at its source instead "
        "of restating it here"
    )
    assert quarantine.patch_refusal(OTHER) == ""


def test_patch_refusal_follows_a_quoted_passage(tmp_path):
    _file_correction(tmp_path, "the test suite on this machine")
    assert quarantine.patch_refusal("never touch the test suite on this machine because") != ""
    assert quarantine.patch_refusal("# Tests because it is a header") == ""


def test_patch_refusal_matches_a_short_directive_whole():
    _confirmed("short-1", "go fast because ok")
    assert "short-1" in quarantine.patch_refusal("go  fast because ok")
    assert quarantine.patch_refusal("go fast because ok and more") == ""
