"""Correction quarantine: proposals wait for the operator, a confirmed correction withholds its directive."""

import json
from unittest.mock import patch

import pytest

from hooks.context import conditions, enforcement, injection_trace, quarantine, trace_sweep
from scripts.trace_cli import main as trace

SID = "sess-quarantine-1"
TEXT = "Never run the test suite on this machine, the shared runner owns it"
OTHER = "Keep every pull request small and focused on one change"
SAID = "please confirm the correction on the test rule"


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    for name in (
        "AGENTIHOOKS_SWARM",
        "AGENTIHOOKS_AGENT_NAME",
        "AGENTIHOOKS_SWARM_TASK",
        "AGENTIHOOKS_GATE_QUARANTINE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def filed():
    found = []
    with patch.object(trace_sweep, "_file_followup", side_effect=lambda ledger, text, *flags: found.append(flags)):
        yield found


@pytest.fixture()
def swarm(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")


def _received(source="bad-1", text=TEXT, layer="bundle", locator=None):
    injection_trace.record(SID, layer, source, text, locator or {"store": "/s", "id": source})


def _wrong(source="bad-1", *extra):
    return trace([SID, "--wrong", source, "--repo", "/repos/qitp", "--reason", "it belongs to qitp", *extra])


def _confirmed(source="bad-1", text=TEXT, layer="bundle", locator=None):
    _received(source, text, layer, locator)
    return injection_trace.correct(SID, source, "/repos/qitp", "it belongs to qitp")


def _entries():
    return [
        {"id": "bad-1", "message": TEXT, "cadence": 5, "source": "bundle"},
        {"id": "keep-1", "message": OTHER, "cadence": 5, "source": "bundle"},
    ]


def _start(session=SID):
    with patch.object(enforcement, "load_all_enforcements", return_value=_entries()):
        return enforcement.get_session_start_enforcements(session) or ""


def _withheld(session=SID):
    return [row for row in injection_trace.trace(session) if row["layer"] == quarantine.WITHHELD]


def test_mode_defaults_to_enforce_and_reads_the_switch():
    assert quarantine.mode({}) == "enforce"
    assert quarantine.mode({"AGENTIHOOKS_GATE_QUARANTINE": " Observe "}) == "observe"
    assert quarantine.mode({"AGENTIHOOKS_GATE_QUARANTINE": "off"}) == "off"
    assert quarantine.mode({"AGENTIHOOKS_GATE_QUARANTINE": "loud"}) == "enforce"


def test_an_agent_correction_in_a_swarm_is_proposed_and_raised(swarm, filed, capsys):
    _received()

    assert _wrong() == 0

    (row,) = injection_trace.corrections()
    assert row["status"] == quarantine.PROPOSED
    assert filed == [("--needs-operator",)]
    assert "raised in the operator's Priorities on sw" in capsys.readouterr().out
    assert quarantine.index() == []
    assert "bad-1" in _start("sess-later")


def test_a_correction_outside_a_swarm_is_confirmed_at_once(filed):
    _received()

    assert _wrong() == 0

    assert "status" not in injection_trace.corrections()[0]
    assert filed == []
    assert [h.correction["source"] for h in quarantine.index()] == ["bad-1"]


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

    assert trace(["confirm", "bad-1", "--quote", "confirm the correction now"]) == 2

    assert "only the operator confirms a correction" in capsys.readouterr().err
    assert quarantine.index() == []


def test_the_operators_quoted_words_confirm_and_run_the_sweep(swarm, filed, tmp_path, capsys):
    from hooks.context import operator_words

    _received()
    _wrong()
    operator_words.record("engineer@a1b2c3-0001", SAID)
    capsys.readouterr()

    with patch.object(trace_sweep, "find", return_value=[]) as find:
        assert trace(["confirm", "bad-1", "--quote", "confirm the correction", "--root", str(tmp_path)]) == 0

    assert find.call_count == 1
    assert [h.correction["source"] for h in quarantine.index()] == []
    out = capsys.readouterr().out
    assert "closed\tbad-1" in out
    (entry,) = injection_trace._read(quarantine._confirmations_path())
    assert (entry["by"], entry["words"]) == ("words", "confirm the correction")


def test_an_operator_comment_on_the_task_confirms(swarm, filed, tmp_path):
    _received()
    _wrong()
    with (
        patch(
            "hooks.context.ledger_request.find", side_effect=lambda asks, env: ("ledger", "c1") if asks(SAID) else None
        ),
        patch.object(trace_sweep, "find", return_value=[trace_sweep.Hit("bad-1", "x", "y", "z", "held")]),
    ):
        assert trace(["confirm", "bad-1", "--root", str(tmp_path)]) == 0
    assert [h.correction["source"] for h in quarantine.index()] == ["bad-1"]
    assert injection_trace._read(quarantine._confirmations_path())[0]["by"] == "ledger"


def test_confirm_needs_a_waiting_proposal(capsys):
    assert trace(["confirm", "bad-1"]) == 2
    assert "no proposed correction of bad-1 waits" in capsys.readouterr().err


@pytest.mark.parametrize(
    "words,ok",
    [
        ("confirm the correction", True),
        ("Confirmed, the CORRECTION stands", True),
        ("confirm the tests pass", False),
        ("the correction is wrong", False),
        ("", False),
    ],
)
def test_only_words_naming_confirm_and_correction_count(words, ok):
    assert quarantine._asks("confirm")(words) is ok


def test_short_quotes_never_count(swarm):
    from hooks.context import operator_words

    operator_words.record("engineer@a1b2c3-0001", "confirm correction")
    with patch("hooks.context.ledger_request.find", return_value=None):
        assert quarantine.operator_said("confirm", "confirm correction") == ""
        assert quarantine.operator_said("confirm", "confirm the correction") == ""
    assert quarantine.operator_said("confirm", "", {}) == "operator"


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
    assert row["locator"]["layer"] == "enforcement"
    assert row["locator"]["mode"] == "enforce"
    assert row["text"] == "withheld under the correction of bad-1: it belongs to qitp"


def test_the_same_text_under_another_id_is_withheld_too():
    _confirmed()
    entries = [{"id": "copy-9", "message": f"Note: {TEXT}.", "cadence": 5}]
    with patch.object(enforcement, "load_all_enforcements", return_value=entries):
        assert enforcement.get_session_start_enforcements("sess-b") is None


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
        yield


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
    assert [row["source"] for row in _withheld("sess-f")] == ["bc-brain-2"]


def test_a_condition_context_under_correction_is_dropped():
    _confirmed("pre-bash.guard.sh", TEXT, "condition", {"file": "/x/pre-bash.guard.sh"})
    entry = {"file": "pre-bash.guard.sh", "path": "/x/pre-bash.guard.sh"}
    kept = {"file": "pre-bash.other.sh", "path": "/x/pre-bash.other.sh"}

    result = conditions.merge(
        "pre",
        {"session_id": "sess-g", "tool_name": "Bash"},
        [(entry, {"returncode": 0, "stdout": "mind the runner"}), (kept, {"returncode": 0, "stdout": "fine"})],
    )

    assert result.contexts == ["[condition pre-bash.other.sh]\nfine"]
    assert [row["source"] for row in _withheld("sess-g")] == ["pre-bash.guard.sh"]


def _file_correction(tmp_path, quote=None):
    repo = tmp_path / "bundle"
    rules = repo / "rules"
    rules.mkdir(parents=True)
    (repo / ".git").mkdir()
    (rules / "tests.md").write_text(f"# Tests\n\n{TEXT}\nAnd keep going.\n")
    locator = {"repo": str(repo), "path": "rules/tests.md", "blob": "x"}
    injection_trace.record(SID, "rule", "bundle/rules/tests.md", "# Tests", locator)
    row = injection_trace.correct(SID, "bundle/rules/tests.md", "/repos/qitp", "it belongs to qitp", quote or "")
    return rules / "tests.md", row


def test_render_puts_a_notice_after_the_quoted_passage(tmp_path):
    path, _ = _file_correction(tmp_path, "the test suite on this machine")

    out = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")

    assert out == (
        f"# Tests\n\n{TEXT}\n\n"
        "> CORRECTION: the passage above is marked wrong for the qitp repo: it belongs to qitp. Do not follow it.\n"
        "\nAnd keep going.\n"
    )
    assert quarantine.annotate("nothing quoted here\n") == "nothing quoted here\n"


def test_a_passage_split_across_lines_is_still_found(tmp_path):
    _file_correction(tmp_path, "the test suite on this machine")
    out = quarantine.annotate("Never run the test\nsuite on this machine")
    assert out.endswith(
        "machine\n\n> CORRECTION: the passage above is marked wrong for the qitp repo: it belongs to qitp. Do not follow it.\n"
    )


def test_an_unquoted_file_correction_heads_the_file(tmp_path):
    path, _ = _file_correction(tmp_path)

    out = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")

    assert out.startswith("> CORRECTION: this file is marked wrong for the qitp repo: it belongs to qitp.")
    assert out.endswith(path.read_text())
    assert quarantine.annotate("other file\n", "bundle/rules/other.md") == "other file\n"


def test_observe_mode_leaves_the_render_alone(tmp_path, monkeypatch):
    path, _ = _file_correction(tmp_path, "the test suite on this machine")
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "observe")
    assert quarantine.annotate(path.read_text(), "bundle/rules/tests.md") == path.read_text()


def test_a_source_confirmed_twice_is_held_whole_until_released(tmp_path):
    path, first = _file_correction(tmp_path, "the test suite on this machine")
    trace_sweep.close(first)
    with patch.object(injection_trace, "_now", return_value="2999-01-01T00:00:00Z"):
        injection_trace.correct(SID, "bundle/rules/tests.md", "/repos/qitp", "still wrong", "And keep going.")

    held = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")
    assert held == (
        "> CORRECTION: bundle/rules/tests.md was marked wrong 2 times and is held whole until the operator "
        "releases it.\n"
    )

    with patch.object(injection_trace, "_now", return_value="2999-01-01T00:00:01Z"):
        assert trace(["release", "bundle/rules/tests.md"]) == 0
    released = quarantine.annotate(path.read_text(), "bundle/rules/tests.md")
    assert "And keep going.\n\n> CORRECTION" in released
    assert "the test suite on this machine\n\n> CORRECTION" not in released


def test_an_agent_cannot_release(swarm, capsys):
    with patch("hooks.context.ledger_request.find", return_value=None):
        assert trace(["release", "bundle/rules/tests.md"]) == 2
    assert "only the operator releases a correction" in capsys.readouterr().err
    assert injection_trace._read(quarantine._releases_path()) == []


def test_the_digest_moves_when_a_correction_is_confirmed(swarm, filed):
    _received()
    _wrong()
    assert quarantine.digest() == ""
    quarantine.confirm(injection_trace.corrections(), "operator")
    assert len(quarantine.digest()) == 12


def test_the_sweep_plans_a_pull_request_for_a_file_still_holding_the_passage(tmp_path):
    path, row = _file_correction(tmp_path, "the test suite on this machine")

    (hit,) = trace_sweep._file_source_hits(row)
    assert (hit.layer, hit.action, hit.location, hit.relpath) == ("rule", "pr", str(path), "tests.md")

    path.write_text("# Tests\n\nAnd keep going.\n")
    assert trace_sweep._file_source_hits(row) == []
    path.unlink()
    assert trace_sweep._file_source_hits(row) == []
    assert trace_sweep._file_source_hits({**row, "layer": "bundle"}) == []


def test_a_pull_request_hit_files_a_plain_followup(filed):
    texts = []
    hit = trace_sweep.Hit("x", "rule", "/r/bundle/rules/t.md", "x", "pr", "/r/bundle", "rules/t.md")
    with patch.object(trace_sweep, "_file_followup", side_effect=lambda ledger, text: texts.append(text)):
        outcome = trace_sweep._apply_one(hit, {"repo": "/repos/qitp", "reason": "wrong"}, SID, "sw")
    assert outcome.endswith("follow up filed on sw")
    assert texts == [
        "One rule directive in the bundle repo is marked wrong for the qitp repo: wrong. Remove it by pull request into dev."
    ]
    assert "follow up to file" in trace_sweep._apply_one(hit, {"repo": "/q", "reason": "w"}, SID, "")


def _priming_correction(layer="learned", text="Push straight to dev because it is faster"):
    source = f"{layer}:eng-2@sw#1"
    locator = {"seat": "eng-2@sw", "note": 1} if layer == "learned" else {"swarm": "sw", "line": 1}
    injection_trace.record(SID, layer, source, text, locator)
    return injection_trace.correct(SID, source, "/repos/qitp", "never push to dev")


class _Store:
    def __init__(self, notes=(), culture=""):
        self.memory = type("M", (), {"learned": lambda _, seat: [{"text": t} for t in notes]})()
        self.culture = type("C", (), {"get": lambda _, slug: culture})()


def test_the_sweep_holds_a_priming_correction_while_its_note_lives():
    row = _priming_correction()
    with patch("scripts.swarm.store.connect", return_value=_Store(["Push straight to dev because it is faster"])):
        (hit,) = trace_sweep._priming_hits(row, quarantine.needle(row))
    assert (hit.layer, hit.action) == ("learned", "held")
    assert trace_sweep._apply_one(hit, row, SID, "") == "withheld from priming until its source is fixed"
    with patch("scripts.swarm.store.connect", return_value=_Store(["something else entirely"])):
        assert trace_sweep._priming_hits(row, quarantine.needle(row)) == []


def test_the_sweep_finds_a_short_culture_line_by_its_whole_text():
    row = _priming_correction("culture", "- merge fast")
    with patch("scripts.swarm.store.connect", return_value=_Store(culture="# How\n- merge fast\n")):
        assert len(trace_sweep._priming_hits(row, quarantine.needle(row))) == 1


def test_an_unreadable_store_keeps_the_correction_open():
    row = _priming_correction()
    with patch("scripts.swarm.store.connect", side_effect=ConnectionError("no redis")):
        assert len(trace_sweep._priming_hits(row, "x")) == 1
    assert trace_sweep._priming_hits({**row, "layer": "bundle"}, "x") == []


def test_patch_refusal_names_the_open_correction():
    _confirmed()
    assert quarantine.patch_refusal(f"Remember: {TEXT} because") == (
        "this text carries a directive under correction (bad-1: it belongs to qitp); fix it at its source instead "
        "of restating it here"
    )
    assert quarantine.patch_refusal(OTHER) == ""
