import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hooks.classifier import Answer, DecisionResult
from hooks.context import conditions, profile_chain
from hooks.filters.check import ALLOW, FilterOutcome
from scripts import cli_delegates
from scripts.evidence import schema
from scripts.headless import runner
from scripts.headless.request import SEALED, HeadlessError
from scripts.judge import cli, record
from tests.filters.conftest import filters_dir, project_filters  # noqa: F401

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
REAL_LAYERS = conditions.layer_dirs
CUED = "engineer@323133-0662 (opus, attempt 2) clearly verified the cache keeps one entry per key"
CUES = ("engineer@323133-0662", "opus", "attempt 2", "clearly")
CASE = {"intent": "The cache keeps one entry per key", "points": ["a second write replaces the first"]}
PASS = {"verdict": "pass", "notes": []}


def _item(index, claim):
    return {
        "id": f"e{index}",
        "claim": claim,
        "source": "tests/test_cache.py",
        "excerpt": "assert len(cache) == 1",
        "retrieved_at": "now",
    }


def _evidence(*claims):
    return {"items": [_item(i, claim) for i, claim in enumerate(claims)]}


class FakeRunner:
    def __init__(self, reply=PASS, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def __call__(self, profile_prompt, user_input, schema, clamp, tier, effort, harness="auto"):
        self.calls.append(
            {
                "profile_prompt": profile_prompt,
                "user_input": user_input,
                "schema": schema,
                "clamp": clamp,
                "tier": tier,
                "effort": effort,
                "harness": harness,
            }
        )
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def files(tmp_path):
    def write(case=CASE, evidence=None):
        case_path = tmp_path / "case.json"
        evidence_path = tmp_path / "evidence.json"
        case_path.write_text(json.dumps(case))
        evidence_path.write_text(json.dumps(evidence or _evidence("the cache keeps one entry")))
        return case_path, evidence_path

    return write


@pytest.fixture
def fake_runner(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.delenv("AGENTIHOOKS_TIER_HIGH_CLAUDE", raising=False)

    def install(**kwargs):
        fake = FakeRunner(**kwargs)
        monkeypatch.setattr(runner, "run", fake)
        return fake

    return install


@pytest.fixture
def filters(monkeypatch):
    def install(outcome):
        seen = []
        monkeypatch.setattr(cli, "check", lambda tool, tool_input: seen.append((tool, tool_input)) or outcome)
        return seen

    return install


def test_a_valid_verdict_is_printed_and_recorded(files, fake_runner, filters, capsys, tmp_path):
    case_path, evidence_path = files()
    fake = fake_runner()
    seen = filters(ALLOW)
    out = tmp_path / "records" / "verdict.json"

    code = cli.main(["--case", str(case_path), "--evidence", str(evidence_path), "--out", str(out)])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == PASS
    entry = json.loads(out.read_text())
    assert {key: value for key, value in entry.items() if key != "at"} == {
        "case_sha256": record.digest(CASE),
        "evidence_sha256": record.digest(_evidence("the cache keeps one entry")),
        "verdict": "pass",
        "notes": [],
        "harness": "claude",
        "model": "opus",
    }
    assert datetime.fromisoformat(entry["at"]).tzinfo is not None
    assert seen == [("judge", {"case": CASE, "evidence": _evidence("the cache keeps one entry")})]
    [call] = fake.calls
    assert (call["clamp"], call["tier"], call["effort"], call["harness"]) == (SEALED, "high", "medium", "claude")
    assert call["profile_prompt"] == (ROOT / "profiles/package/roles/judge/CLAUDE.md").read_text()
    assert call["schema"] == json.loads((schema.SCHEMAS / "verdict.schema.json").read_text())
    assert json.loads(call["user_input"]) == {"case": CASE, "evidence": _evidence("the cache keeps one entry")}


def test_the_record_defaults_next_to_the_case_and_follows_tier_and_harness(files, fake_runner, filters, monkeypatch):
    case_path, evidence_path = files()
    fake = fake_runner(reply={"verdict": "enhance", "notes": [{"point": "p", "missing": "m"}]})
    filters(ALLOW)
    monkeypatch.setenv("AGENTIHOOKS_TIER_LOW_CODEX", "gpt-test")

    code = cli.main(["--case", str(case_path), "--evidence", str(evidence_path), "--tier", "low", "--harness", "codex"])

    assert code == 0
    entry = json.loads(case_path.with_suffix(".verdict.json").read_text())
    assert (entry["harness"], entry["model"], entry["verdict"]) == ("codex", "gpt-test", "enhance")
    assert entry["notes"] == [{"point": "p", "missing": "m"}]
    assert (fake.calls[0]["tier"], fake.calls[0]["harness"]) == ("low", "codex")


def test_a_filter_deny_exits_three_with_the_findings(files, fake_runner, filters, capsys):
    case_path, evidence_path = files()
    fake = fake_runner()
    filters(FilterOutcome("deny", "pre-judge-bias: argues for one outcome\nobviously the best"))

    code = cli.main(["--case", str(case_path), "--evidence", str(evidence_path)])

    assert code == 3
    assert json.loads(capsys.readouterr().out) == {
        "sent_back": True,
        "findings": ["pre-judge-bias: argues for one outcome", "obviously the best"],
    }
    assert fake.calls == []
    assert not case_path.with_suffix(".verdict.json").exists()


def test_the_judge_reads_the_rewritten_input(files, fake_runner, filters):
    case_path, evidence_path = files(evidence=_evidence("kept", "dropped"))
    fake = fake_runner()
    filters(FilterOutcome("rewrite", "", {"case": CASE, "evidence": _evidence("kept")}))

    assert cli.main(["--case", str(case_path), "--evidence", str(evidence_path)]) == 0
    assert json.loads(fake.calls[0]["user_input"])["evidence"] == _evidence("kept")
    entry = json.loads(case_path.with_suffix(".verdict.json").read_text())
    assert entry["evidence_sha256"] == record.digest(_evidence("kept", "dropped"))


def test_the_evidence_is_shuffled_before_the_judge(files, fake_runner, filters, monkeypatch):
    case_path, evidence_path = files(evidence=_evidence("a", "b", "c"))
    fake = fake_runner()
    filters(ALLOW)
    monkeypatch.setattr(schema, "shuffled", lambda items: list(reversed(items)))

    assert cli.main(["--case", str(case_path), "--evidence", str(evidence_path)]) == 0
    claims = [item["claim"] for item in json.loads(fake.calls[0]["user_input"])["evidence"]["items"]]
    assert claims == ["c", "b", "a"]


@pytest.mark.parametrize(
    "runner_kwargs",
    [
        {"reply": {"verdict": "maybe", "notes": []}},
        {"reply": {"verdict": "pass", "notes": [{"point": "p", "missing": "m"}]}},
        {"error": HeadlessError("claude returned no reply matching the schema")},
    ],
)
def test_an_invalid_verdict_exits_non_zero_and_writes_no_record(files, fake_runner, filters, capsys, runner_kwargs):
    case_path, evidence_path = files()
    fake_runner(**runner_kwargs)
    filters(ALLOW)

    code = cli.main(["--case", str(case_path), "--evidence", str(evidence_path)])

    assert code == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err.startswith("judge: ")) == ("", True)
    assert not case_path.with_suffix(".verdict.json").exists()


@pytest.mark.parametrize(
    ("case", "evidence", "message"),
    [
        ({"intent": "x"}, None, "case document"),
        (CASE, {"items": []}, "evidence items"),
    ],
)
def test_an_invalid_case_or_evidence_exits_two_before_the_filters(
    files, fake_runner, filters, capsys, case, evidence, message
):
    case_path, evidence_path = files(case=case)
    if evidence is not None:
        evidence_path.write_text(json.dumps(evidence))
    fake = fake_runner()
    seen = filters(ALLOW)

    code = cli.main(["--case", str(case_path), "--evidence", str(evidence_path)])

    assert code == 2
    assert message in capsys.readouterr().err
    assert (seen, fake.calls) == ([], [])


def test_a_missing_case_file_exits_two(tmp_path, fake_runner, filters, capsys):
    filters(ALLOW)
    fake_runner()
    assert cli.main(["--case", str(tmp_path / "none.json"), "--evidence", str(tmp_path / "none.json")]) == 2
    assert capsys.readouterr().err.startswith("judge: ")


class CueJudge:
    def __call__(self, state, questions, **kwargs):
        cue = state["filter"] == "pre-judge-blind.filter.yaml"
        answers = {name: Answer(type="noul", noul=0.9 if cue else 0.1) for name in questions}
        return DecisionResult(answers=answers, source="stub")


@pytest.fixture
def shipped(filters_dir, project_filters, monkeypatch):  # noqa: F811
    monkeypatch.setattr(conditions, "layer_dirs", REAL_LAYERS)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", ROOT / "profiles")
    monkeypatch.setattr(conditions, "repo_root", lambda cwd=None: ROOT)
    monkeypatch.setattr(conditions, "directory_trust", lambda root, state: (True, "test"))
    monkeypatch.setattr("hooks.classifier.decide", CueJudge())


def test_the_judge_input_carries_no_cue_words(shipped, files, fake_runner):
    case_path, evidence_path = files(evidence=_evidence(CUED))
    fake = fake_runner()

    assert cli.main(["--case", str(case_path), "--evidence", str(evidence_path)]) == 0
    user_input = fake.calls[0]["user_input"]
    for cue in CUES:
        assert cue not in user_input
    assert "verified the cache keeps one entry per key" in user_input


def test_the_record_build_is_fixed_by_its_inputs():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    entry = record.build(CASE, _evidence("a"), PASS, "claude", "opus", now)
    assert entry == {
        "case_sha256": record.digest(CASE),
        "evidence_sha256": record.digest(_evidence("a")),
        "verdict": "pass",
        "notes": [],
        "harness": "claude",
        "model": "opus",
        "at": "2026-10-26T12:00:00+00:00",
    }
    assert record.digest({"b": 1, "a": 2}) == record.digest({"a": 2, "b": 1})
    assert len(record.digest(CASE)) == 64


def test_the_cli_delegates_judge_to_this_module():
    assert cli_delegates.delegated_cli(["judge", "--case", "x"]) is cli.main
