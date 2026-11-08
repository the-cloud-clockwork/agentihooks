import hashlib
import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.plans import cli, ingest
from scripts.plans.normalize import normalize
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_link, ledger_publish
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind

plan_ledger = test_plan_kind.plan_ledger

MARKED = (
    "# Ingest proof\n\n"
    "## Alpha\n<!-- phase: id=alpha planning=auto -->\nAlpha prose.\n\n"
    "<!-- slice: a1 -->\n### First slice\nDo it.\n\n"
    "## Beta\n<!-- phase: id=beta depends=alpha planning=manual -->\nBeta prose.\n"
)
SEAT = {"AGENTIHOOKS_SWARM": "plan-proof", "AGENTIHOOKS_AGENT_NAME": "planner"}


class NoSwarm:
    def config(self, slug):
        raise SwarmError(f"no swarm {slug}")


@pytest.fixture
def served(plan_ledger, monkeypatch):
    sent = []

    def call(slug, ops=None, service=False):
        state, rejected = core.sync(slug, ops=ops)
        sent.append([op["op"] for op in ops or []])
        return {**state, "applied": [op["id"] for op in ops or [] if op["id"] not in rejected], "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr(ledger, "resource", lambda slug, key, collection=True: core.sync(slug)[0].get(key, []))
    monkeypatch.setattr(
        ledger,
        "upload_artifact",
        lambda slug, name, path, request: ledger_artifacts.store(slug, "plan.md", open(path, "rb").read()),
    )
    monkeypatch.setattr(ledger_publish, "has_issues", lambda repo, run=None: False)
    monkeypatch.setattr(ledger_link, "remote", lambda environ=None, slug="": False)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda environ=None: NoSwarm())
    notes = []
    monkeypatch.setattr("scripts.inbox.cli.main", lambda argv: notes.append(argv) or 0)
    return {"slug": plan_ledger, "sent": sent, "notes": notes}


def run(tmp_path, capsys, *extra, text=MARKED, env=SEAT):
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    assert cli.main(["ingest", str(path), "--ledger", "plan-proof", *extra], env) == 0
    return json.loads(capsys.readouterr().out)


def test_author_is_the_bound_seat_or_the_operator_on_the_host():
    assert ingest.author("planner", "plan-proof", "plan-proof", remote=True) == ingest.Author("planner", True)
    assert ingest.author("", "", "plan-proof", remote=False) == ingest.Author("operator", False)
    assert ingest.author("planner", "", "plan-proof", remote=False) == ingest.Author("operator", False)


def test_author_refuses_another_swarms_seat_and_an_unbound_remote_session():
    with pytest.raises(ingest.IngestError) as raised:
        ingest.author("planner", "other", "plan-proof", remote=False)
    assert str(raised.value) == "a seat of swarm other cannot ingest a plan into ledger plan-proof"
    with pytest.raises(ingest.IngestError) as raised:
        ingest.author("", "", "plan-proof", remote=True)
    assert str(raised.value) == (
        "an unbound session cannot ingest into a remote ledger; the operator credential stays on its host"
    )


def test_batch_links_every_phase_and_adds_a_slice_per_anchor():
    plan = normalize(MARKED, "plan.md")
    phases = ingest.entries(plan, ["p1"])
    links = ingest.Links("plan-abc", {"id": "f.md"}, "https://l/a", "https://l/u", "t1", {"harness": "claude"})
    ops = ingest.batch(plan, phases, links, "operator")
    assert [op["op"] for op in ops] == ["artifact_add", "plan_add", "phase_append", "phase_append", "slice_add"]
    assert [op["id"] for op in ops] == [f"{op['op']}-plan-abc-{n}" for n, op in enumerate(ops)]
    assert {op["by"] for op in ops} == {"operator"}
    assert ops[0] == {
        "op": "artifact_add",
        "task": "t1",
        "title": "Ingest proof",
        "file": {"id": "f.md"},
        "plan": True,
        "id": "artifact_add-plan-abc-0",
        "by": "operator",
    }
    assert ops[1]["source"] == {"harness": "claude"}
    assert (ops[1]["plan"], ops[1]["artifact"], ops[1]["url"]) == ("plan-abc", "https://l/a", "https://l/u")
    [alpha] = ops[2]["phases"]
    assert alpha["phase"] == "alpha"
    assert (alpha["plan"], alpha["plan_url"]) == ("plans/plan-abc", "https://l/u")
    assert alpha["plan_ref"] == {"artifact": "https://l/a", "lines": "3-10"}
    assert ops[3]["phases"][0]["depends_on"] == ["alpha"]
    assert (ops[4]["phase"], ops[4]["anchor"]) == ("phases/alpha", "a1")


def test_batch_adds_a_not_duplicate_phase_with_phase_add_and_omits_an_empty_source():
    text = MARKED.replace("planning=manual", 'planning=manual not_duplicate="a different build"')
    plan = normalize(text, "plan.md")
    ops = ingest.batch(plan, ingest.entries(plan, []), ingest.Links("plan-abc", {}, "https://l/a", "https://l/u"), "x")
    assert "source" not in ops[1]
    assert ops[3]["op"] == "phase_add"
    assert (ops[3]["phase"], ops[3]["not_duplicate"]) == ("beta", "a different build")


def test_batch_refuses_more_than_the_operation_cap():
    text = "# Big\n\n## Only\n<!-- phase: id=only -->\n\n" + "".join(
        f"<!-- slice: s{n} -->\n### Slice {n}\nx\n\n" for n in range(99)
    )
    plan = normalize(text, "plan.md")
    with pytest.raises(ingest.IngestError) as raised:
        ingest.batch(plan, ingest.entries(plan, []), ingest.Links("plan-abc", {}, "https://l/a", "https://l/u"), "x")
    assert str(raised.value) == "the plan needs 102 operations; one ingest holds at most 100"


def test_planning_override_applies_to_every_phase():
    plan = normalize(MARKED, "plan.md")
    assert [entry["planning"] for entry in ingest.entries(plan, [], "manual")] == ["manual", "manual"]
    assert [entry["planning"] for entry in ingest.entries(plan, [])] == ["auto", "manual"]


def test_dry_run_writes_nothing(served, tmp_path, capsys):
    before = core.sync(served["slug"])[0]
    result = run(tmp_path, capsys, "--dry-run")
    assert result == {
        "dry_run": True,
        "plan": ingest.plan_of(MARKED),
        "by": "planner",
        "phases": ["alpha", "beta"],
        "ops": 5,
    }
    after = core.sync(served["slug"])[0]
    assert (after["phases"], after.get("plans", []), after.get("artifacts", [])) == (
        before["phases"],
        before.get("plans", []),
        before.get("artifacts", []),
    )
    assert served["sent"] == [[]] * len(served["sent"])


def test_apply_links_phases_and_slices_then_a_rerun_is_already_ingested(served, tmp_path, capsys):
    plan = ingest.plan_of(MARKED)
    result = run(tmp_path, capsys)
    url = result.pop("plan_url")
    assert result == {
        "plan": plan,
        "phases": ["alpha", "beta"],
        "lifecycle": {"alpha": "to_plan", "beta": "waiting"},
        "swarm": "absent",
        "ingested": True,
        "published_to": "artifact",
    }
    state = core.sync(served["slug"])[0]
    assert [row["id"] for row in state["plans"]] == [plan]
    assert url == state["plans"][0]["url"]
    assert [row["anchor"] for row in state["slices"]] == ["a1"]
    assert {p["id"]: p.get("plan") for p in state["phases"]} == {
        "p1": None,
        "alpha": f"plans/{plan}",
        "beta": f"plans/{plan}",
    }
    assert served["notes"] == [
        ["send", "master@plan-proof", "--fyi", "planner ingested the plan Ingest proof as 2 phases."]
    ]
    again = run(tmp_path, capsys)
    assert again == {
        "plan": plan,
        "phases": ["alpha", "beta"],
        "lifecycle": {"alpha": "to_plan", "beta": "waiting"},
        "swarm": "absent",
        "ingested": False,
        "note": "already ingested",
    }
    assert len(core.sync(served["slug"])[0]["phases"]) == 3


def test_a_master_author_sends_no_note(served, tmp_path, capsys):
    run(tmp_path, capsys, env={**SEAT, "AGENTIHOOKS_AGENT_NAME": "master@plan-proof"})
    assert served["notes"] == []


def test_a_duplicate_phase_rolls_back_every_row(served, tmp_path, capsys):
    text = MARKED.replace("id=beta", "id=p1")
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, text=text)
    assert "phase id p1 is already taken" in str(raised.value)
    state = core.sync(served["slug"])[0]
    assert [p["id"] for p in state["phases"]] == ["p1"]
    assert (state.get("plans", []), state.get("slices", [])) == ([], [])


def test_the_command_refuses_another_swarms_seat_before_reading(served, tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, env={**SEAT, "AGENTIHOOKS_SWARM": "other"})
    assert str(raised.value) == "a seat of swarm other cannot ingest a plan into ledger plan-proof"


def test_the_command_refuses_a_plan_the_normalizer_refuses(served, tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, text="   \n")
    assert str(raised.value) == "the plan has no content"


def test_plan_read_still_routes_through_the_plan_command(monkeypatch):
    seen = []
    monkeypatch.setattr("scripts.swarm_ledger.plan_read.main", lambda argv, environ: seen.append(argv) or 0)
    assert cli.main(["read", "--task", "t1"], {}) == 0
    assert seen == [["read", "--task", "t1"]]


@pytest.mark.parametrize("path", ["-", "plan.md"])
@pytest.mark.parametrize("env", [{}, {"AGENTIHOOKS_TARGET": "codex", "CLAUDE_SESSION_ID": "session-one"}])
def test_source_records_exact_provenance(
    path: str, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert ingest.source(env, path, "canonical é") == {
        "harness": env.get("AGENTIHOOKS_TARGET", "claude"),
        "session": env.get("CLAUDE_SESSION_ID", ""),
        "path": "" if path == "-" else str(tmp_path / path),
        "sha256": hashlib.sha256("canonical é".encode()).hexdigest(),
    }


def test_parser_contract(capsys: pytest.CaptureFixture[str]) -> None:
    parser = ingest.parser()
    assert parser.prog == "agentihooks plan ingest"
    assert parser.description == ingest.__doc__
    assert vars(parser.parse_args(["-", "--ledger", "proof"])) == {
        "path": "-",
        "ledger": "proof",
        "planning": "",
        "dry_run": False,
        "repo": "",
    }
    assert vars(
        parser.parse_args(["plan.json", "--ledger", "proof", "--planning", "manual", "--dry-run", "--repo", "org/repo"])
    ) == {"path": "plan.json", "ledger": "proof", "planning": "manual", "dry_run": True, "repo": "org/repo"}
    help_text = parser.format_help()
    for text in [
        "plan file, or - for standard input",
        "target ledger slug",
        "planning for every phase",
        "print what would be written and write nothing",
        "OWNER/NAME for the plan issue; default the current repo",
    ]:
        assert text in " ".join(help_text.split())
    for args in [
        [],
        ["plan.md"],
        ["plan.md", "--ledger", "proof", "--planning", "wrong"],
        ["plan.md", "--ledger", "proof", "--as", "operator"],
    ]:
        with pytest.raises(SystemExit) as raised:
            parser.parse_args(args)
        assert raised.value.code == 2
    assert "--ledger" in capsys.readouterr().err


def test_read_plan_from_stdin_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"stdin content")))
    assert ingest.read_plan("-") == b"stdin content"
    path = tmp_path / "input.md"
    path.write_bytes(b"file content")
    assert ingest.read_plan(str(path)) == b"file content"


@pytest.mark.parametrize("state", ["running", "paused", "stopped"])
def test_swarm_state_reads_the_target(state: str) -> None:
    def config(slug: str) -> SimpleNamespace:
        assert slug == "proof"
        return SimpleNamespace(state=state)

    assert ingest.swarm_state("proof", lambda: SimpleNamespace(config=config)) == state


def test_batch_accepts_exactly_one_hundred_operations() -> None:
    text = "# Big\n\n## Only\n<!-- phase: id=only -->\n\n" + "".join(
        f"<!-- slice: s{n} -->\n### Slice {n}\nx\n\n" for n in range(97)
    )
    plan = normalize(text, "plan.md")
    assert (
        len(
            ingest.batch(
                plan, ingest.entries(plan, []), ingest.Links("plan-abc", {}, "https://l/a", "https://l/u"), "x"
            )
        )
        == 100
    )


@pytest.mark.parametrize("seat", [True, False])
@pytest.mark.parametrize("path", ["-", "input.txt"])
@pytest.mark.parametrize("task", ["", "task-one"])
def test_publish_upload_and_issue_boundary(
    seat: bool, path: str, task: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = normalize(MARKED, "plan.md")
    phases = ingest.entries(plan, [])
    args = SimpleNamespace(ledger="proof", path=path, repo="org/repo")
    env = {"AGENTIHOOKS_SWARM_TASK": task, "AGENTIHOOKS_TARGET": "codex", "CLAUDE_SESSION_ID": "session-one"}
    by = ingest.Author("planner" if seat else "operator", seat)
    stored = {"id": "file-one.md"}
    expected_name = "stdin.md" if path == "-" else "input.md"
    seen = []

    def upload(slug: str, name: str, filename: str, request: dict[str, Any]) -> dict[str, str]:
        assert (slug, name) == ("proof", by.name)
        assert Path(filename).name == expected_name
        assert Path(filename).read_bytes() == MARKED.encode()
        assert request == {"task": task if seat else "", "title": plan.title, "plan": True}
        seen.append("upload")
        return stored

    def publish(
        filename: str, title: str, repo: str, artifact: Callable[[str, str], str], *, issue_title: str
    ) -> tuple[str, str]:
        assert (title, repo, issue_title) == (plan.title, "org/repo", plan.title)
        assert artifact(filename, title) == "https://ledger.example/artifacts/proof/file-one.md"
        seen.append("issue")
        return "https://github.com/org/repo/issues/1", "issue"

    monkeypatch.setattr(ledger, "base", lambda: "https://ledger.example")
    monkeypatch.setattr(ledger, "upload_artifact", upload)
    monkeypatch.setattr(ledger_publish, "publish", publish)
    ops, url, where = ingest.publish(args, env, plan, phases, by, "plan-one")
    assert (url, where) == ("https://github.com/org/repo/issues/1", "issue")
    assert seen == ["upload", "issue"]
    assert ops[0] == {
        "op": "artifact_add",
        "task": task if seat else "",
        "title": plan.title,
        "file": stored,
        "plan": True,
        "id": "artifact_add-plan-one-0",
        "by": by.name,
    }
    assert ops[1] == {
        "op": "plan_add",
        "plan": "plan-one",
        "title": plan.title,
        "artifact": "https://ledger.example/artifacts/proof/file-one.md",
        "url": url,
        **({} if seat else {"source": ingest.source(env, path, MARKED)}),
        "id": "plan_add-plan-one-1",
        "by": by.name,
    }


def test_publish_refusal_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def publish(*args: Any, **kwargs: Any) -> None:
        raise ledger_publish.PublishError("issue publication refused")

    monkeypatch.setattr(ledger_publish, "publish", publish)
    plan = normalize(MARKED, "plan.md")
    args = SimpleNamespace(ledger="proof", path="-", repo="")
    with pytest.raises(ingest.IngestError) as raised:
        ingest.publish(args, {}, plan, ingest.entries(plan, []), ingest.Author("planner", True), "plan-one")
    assert str(raised.value) == "issue publication refused"


@pytest.mark.parametrize("path", ["-", "input.json"])
def test_ingest_dry_run_preserves_normalization_and_batch_inputs(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import store

    args = SimpleNamespace(ledger="plan-proof", path=path, planning="manual", dry_run=True)
    env = dict(SEAT)
    original = normalize(MARKED, "plan.md")
    phases = [{"phase": "new"}]
    reads = []
    seen = []

    def remote(environ: dict[str, str], *, slug: str) -> bool:
        assert environ is env
        assert slug == "plan-proof"
        return False

    def resource(slug: str, key: str, *, collection: bool) -> list[dict[str, str]]:
        assert slug == "plan-proof"
        assert collection is True
        reads.append(key)
        return [{"id": "p1"}] if key == "phases" else []

    def normalizer(data: bytes, name: str, existing: list[str]) -> Any:
        assert data == b"input"
        assert name == ("stdin.md" if path == "-" else path)
        assert existing == ["p1"]
        seen.append("normalize")
        return original

    def entries(plan: Any, taken: list[str], planning: str) -> Any:
        assert plan is original
        assert taken == ["p1"]
        assert planning == "manual"
        seen.append("entries")
        return phases

    def batch(plan: Any, found: list[dict[str, str]], links: Any, by: str) -> list[dict[str, str]]:
        assert plan is original
        assert found is phases
        assert links == ingest.Links(
            ingest.plan_of(MARKED), {}, "https://ledger.invalid/plan", "https://ledger.invalid/plan"
        )
        assert by == "planner"
        seen.append("batch")
        return [{"op": "plan_add"}]

    def read_plan(found: str) -> bytes:
        assert found == path
        return b"input"

    def refuse_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("dry run attempted a write")

    monkeypatch.setattr(ledger_link, "remote", remote)
    monkeypatch.setattr(ledger, "resource", resource)
    monkeypatch.setattr(ledger, "call", refuse_write)
    monkeypatch.setattr(ledger, "upload_artifact", refuse_write)
    monkeypatch.setattr(ingest, "read_plan", read_plan)
    monkeypatch.setattr(ingest, "normalize", normalizer)
    monkeypatch.setattr(ingest, "entries", entries)
    monkeypatch.setattr(ingest, "batch", batch)
    monkeypatch.setattr(store, "connect", refuse_write)
    assert ingest.ingest(args, env) == {
        "dry_run": True,
        "plan": ingest.plan_of(MARKED),
        "by": "planner",
        "phases": ["new"],
        "ops": 1,
    }
    assert reads == ["phases", "tasks", "plans"]
    assert seen == ["normalize", "entries", "batch"]


@pytest.mark.parametrize("where", ["issue", "artifact"])
def test_rejected_batch_closes_only_its_issue(
    served: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    call = ledger.call
    ops = [{"op": "plan_add", "id": "refused", "by": "planner"}]
    rejected = {"rejected": ["refused"], "_meta": {"warnings": ["batch refused"]}}
    closed = []
    refused = []

    def apply(slug: str, found: list[dict[str, Any]]) -> dict[str, Any]:
        assert slug == served["slug"]
        if found[0]["op"] == "join":
            assert found == [{"op": "join", "id": f"join-{ingest.plan_of(MARKED)}", "by": "planner", "role": "member"}]
            return call(slug, found)
        assert found is ops
        return rejected

    def refuse(state: dict[str, Any], found: Any = ()) -> None:
        refused.append((state, found))
        if state is rejected:
            assert found is ops
            raise SystemExit("batch refused")

    monkeypatch.setattr(ledger, "call", apply)
    monkeypatch.setattr(ledger, "refused", refuse)
    monkeypatch.setattr(ingest, "publish", lambda *args: (ops, "https://github.com/org/repo/issues/1", where))
    monkeypatch.setattr(ledger_publish, "close_issue", closed.append)
    path = tmp_path / "plan.md"
    path.write_text(MARKED)
    args = ingest.parser().parse_args([str(path), "--ledger", served["slug"]])
    with pytest.raises(SystemExit, match="^batch refused$"):
        ingest.ingest(args, SEAT)
    assert closed == (["https://github.com/org/repo/issues/1"] if where == "issue" else [])
    assert len(refused) == 2
    assert len(core.sync(served["slug"])[0]["phases"]) == 1


@pytest.mark.parametrize("command", ["ingest", "read"])
@pytest.mark.parametrize("implicit", [True, False])
def test_cli_forwards_arguments_and_environment(command: str, implicit: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from scripts.swarm_ledger import plan_read

    argv = [command, "input", "--ledger", "proof"]
    env = {"sentinel": "value"}
    expected = argv[1:] if command == "ingest" else argv

    def main(args: list[str], environ: dict[str, str]) -> int:
        assert args == expected
        assert environ is env
        return 7

    monkeypatch.setattr(ingest if command == "ingest" else plan_read, "main", main)
    monkeypatch.setattr(sys, "argv", ["agentihooks plan", *argv])
    assert cli.main(None if implicit else argv, env) == 7


def test_operator_apply_records_source_without_joining(
    served: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"AGENTIHOOKS_TARGET": "codex", "CLAUDE_SESSION_ID": "source-session", "AGENTIHOOKS_SWARM_TASK": "ignored"}
    result = run(tmp_path, capsys, env=env)
    state = core.sync(served["slug"])[0]
    assert result["ingested"] is True
    assert result["phases"] == ["alpha", "beta"]
    assert state["plans"][0]["source"] == ingest.source(env, str(tmp_path / "plan.md"), MARKED)
    assert all("join" not in ops for ops in served["sent"])
    assert served["notes"] == [
        ["send", "master@plan-proof", "--fyi", "operator ingested the plan Ingest proof as 2 phases."]
    ]
