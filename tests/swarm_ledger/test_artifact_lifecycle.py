import json
import sys
from pathlib import Path
from unittest.mock import patch

import ledger_core as core
import ledger_media as media
import pytest

from scripts.swarm import prompt
from scripts.swarm_ledger import ledger, ledger_gate, ledger_tasks
from scripts.swarm_ledger import ledger_artifacts as artifacts
from scripts.swarm_ledger.repository import repository as storage
from tests.swarm_ledger.test_artifacts import MARKDOWN
from tests.swarm_ledger.test_bin import DAY_MS, make_ledger
from tests.swarm_ledger.test_media import png

AGENT = "life-engineer"
RULE = "proofs go on the task proof and the pull request"


@pytest.fixture(autouse=True)
def package_modules(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)
    monkeypatch.setitem(sys.modules, "ledger_artifacts", artifacts)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)


@pytest.fixture
def slug(request):
    name = f"life-{request.node.name.replace('_', '-')[:40].lower()}"
    make_ledger(name)
    storage.apply_ops(name, ops=[{"op": "join", "id": "j", "by": AGENT}])
    return name


def add_task(slug, task_id, **extra):
    op = {"op": "task_add", "id": f"t-{task_id}", "by": AGENT, "task": task_id, "title": "Work", "lane": "eng"}
    return storage.apply_ops(slug, ops=[{**op, **extra}])


def publish(slug, op_id, task="", data=MARKDOWN, **extra):
    file = artifacts.store(slug, f"{op_id}.md", data)
    op = {"op": "artifact_add", "id": op_id, "by": AGENT, "task": task, "title": "Plan", "file": file, **extra}
    return storage.apply_ops(slug, ops=[op])


class TestRequestGate:
    def test_a_publish_on_an_unmarked_task_is_refused_and_names_where_proofs_go(self, slug):
        add_task(slug, "w1")
        state, rejected = publish(slug, "a-plain", task="w1")
        assert rejected == ["a-plain"] and state["artifacts"] == []
        assert any(RULE in w for w in state["_meta"]["warnings"])

    def test_a_task_marked_artifact_accepts_a_publish(self, slug):
        add_task(slug, "w2", artifact=True)
        state, rejected = publish(slug, "a-asked", task="w2")
        assert rejected == [] and [a["id"] for a in state["artifacts"]] == ["a-asked"]

    def test_a_task_set_to_artifact_later_accepts_a_publish(self, slug):
        add_task(slug, "w3")
        update = {"op": "task_update", "id": "u", "by": AGENT, "item": "tasks/w3", "fields": {"artifact": True}}
        state, _ = storage.apply_ops(slug, ops=[update])
        assert next(t for t in state["tasks"] if t["id"] == "w3")["artifact"] is True
        assert publish(slug, "a-later", task="w3")[1] == []

    def test_an_operator_message_naming_the_request_accepts_a_publish(self, slug):
        storage.apply_ops(slug, ops=[{"op": "add", "thread": "chat", "id": "m-asks", "text": "Draw me the logo"}])
        state, rejected = publish(slug, "a-logo", request="m-asks")
        assert rejected == [] and state["artifacts"][0]["request"] == "m-asks"

    def test_an_operator_comment_on_an_item_counts_as_a_request(self, slug):
        add_task(slug, "w4")
        comment = {"op": "add", "thread": "tasks/w4/comments", "id": "c-asks", "text": "Send me the plan"}
        storage.apply_ops(slug, ops=[comment])
        assert publish(slug, "a-plan", task="w4", request="c-asks")[1] == []

    def test_an_agent_message_or_an_unknown_id_is_no_request(self, slug):
        agent_line = {"op": "add", "thread": "chat", "id": "m-self", "text": "I made a plan", "by": AGENT}
        storage.apply_ops(slug, ops=[agent_line])
        assert publish(slug, "a-self", request="m-self")[1] == ["a-self"]
        assert publish(slug, "a-ghost", request="m-none")[1] == ["a-ghost"]

    def test_request_and_artifact_shapes_are_checked(self):
        good = {
            "op": "artifact_add",
            "id": "a",
            "by": "eng",
            "task": "",
            "title": "Plan",
            "file": {"id": "a" * 64 + ".md"},
        }
        core.check_op({**good, "request": "m-1"})
        for bad in (7, ""):
            with pytest.raises(ValueError, match="^request must be the id of the operator message that asked"):
                core.check_op({**good, "request": bad})
        with pytest.raises(
            ValueError, match="^artifact_add takes id, by, task, title, file and an optional request or plan$"
        ):
            core.check_op({**good, "extra": 1})
        task = {"op": "task_add", "id": "t", "by": "eng", "task": "w", "title": "Work", "lane": "eng"}
        core.check_op({**task, "artifact": True})
        update = {"op": "task_update", "id": "u", "by": "eng", "item": "tasks/w", "fields": {"artifact": False}}
        core.check_op(update)
        for bad in ({**task, "artifact": "yes"}, {**update, "fields": {"artifact": "yes"}}):
            with pytest.raises(ValueError, match="^artifact must be true or false$"):
                core.check_op(bad)


class TestCommands:
    def run(self, argv, state=None):
        args = ledger.build_parser().parse_args(["--slug", "cli", "--as", AGENT, *argv])
        file = {"id": "a" * 64 + ".md", "type": "text/markdown", "size": 1}
        with (
            patch.object(ledger, "upload_artifact", return_value=file),
            patch.object(ledger, "call", return_value=state or {}) as call,
            patch.object(
                ledger,
                "resource",
                side_effect=lambda slug, path, collection=False: (
                    state["_meta"]["events"] if path == "events" else {"artifacts": len(state["artifacts"])}
                ),
            ),
        ):
            getattr(ledger, f"cmd_{args.command.replace('-', '_')}")(args)
        return call.call_args.args[1][0]

    def test_task_add_artifact_marks_the_task(self):
        assert self.run(["task", "add", "w9", "Logo", "--artifact"])["artifact"] is True
        assert "artifact" not in self.run(["task", "add", "w9", "Logo"])

    def test_task_set_artifact_yes_and_no(self):
        assert self.run(["task", "set", "w9", "artifact=yes"])["fields"] == {"artifact": True}
        assert self.run(["task", "set", "w9", "artifact=no"])["fields"] == {"artifact": False}
        with pytest.raises(SystemExit) as exc:
            self.run(["task", "set", "w9", "artifact=maybe"])
        assert exc.value.code == "task set takes artifact=yes or artifact=no"

    def test_artifact_request_names_the_operator_message(self, tmp_path):
        doc = tmp_path / "logo.md"
        doc.write_bytes(MARKDOWN)
        assert self.run(["artifact", str(doc), "Logo", "--task", "", "--request", "m-asks"])["request"] == "m-asks"
        assert "request" not in self.run(["artifact", str(doc), "Logo", "--task", ""])

    def test_a_refused_publish_exits_with_the_rule(self, tmp_path):
        doc = tmp_path / "proof.md"
        doc.write_bytes(MARKDOWN)
        refused = {"rejected": ["x"], "_meta": {"warnings": ["overview has 300 words, limit 200", artifacts.REFUSED]}}
        with pytest.raises(SystemExit) as exc:
            self.run(["artifact", str(doc), "Proof", "--task", ""], refused)
        assert exc.value.code == artifacts.REFUSED
        with pytest.raises(SystemExit) as exc:
            self.run(["artifact", str(doc), "Proof", "--task", ""], {"rejected": ["x"]})
        assert exc.value.code == "rejected: join the ledger first and name a task it holds"

    def test_artifact_purge_sends_the_purge(self):
        purged = {"artifacts": [], "_meta": {"events": [{"kind": "artifacts purged", "count": 0}]}}
        op = self.run(["artifact-purge"], purged)
        assert op["op"] == "artifact_purge" and op["by"] == AGENT

    def test_the_new_options_explain_themselves(self):
        sub = next(a for a in ledger.build_parser()._actions if a.dest == "command").choices
        helps = {a.dest: a.help for name in ("artifact", "task") for a in sub[name]._actions}
        assert helps["request"] == "id of the operator chat line or comment that asked for the file"
        assert helps["artifact"] == "the operator asked this task for a file to review"
        assert "artifact-purge" in sub


class TestInstructions:
    OLD = "Publish plans, screenshots, reports and proof files as they are produced"
    LINE = (
        "Publish an artifact only when the operator asked for that file: a plan, an image, a logo, an SVG, markdown "
        'or JSON he wants to review. Publish it with agentihooks ledger --slug demo --as agent artifact <file> "<title '
        'in plain words>" from a task marked artifact requested, or add --request <id of his message that asked>. '
        "Never publish test runs, logs, review notes or proofs: proofs go on the task proof and the pull request, raw "
        "output stays in the task work folder."
    )

    @pytest.mark.parametrize("lane", ["eng", "ci", "master"])
    def test_prompts_publish_only_requested_files(self, lane):
        text = prompt.build("demo", "/repo", lane, "agent", {"id": "one", "title": "One"})
        assert self.OLD not in text
        assert self.LINE in text

    def test_master_marks_tasks_that_carry_a_requested_file(self):
        text = prompt.build("demo", "/repo", "master", "agent", {"id": "one", "title": "One"})
        assert (
            f"- {self.LINE} When the operator asks for a file to review, add or set its task with --artifact "
            "or artifact=yes so its agent may publish it."
        ) in text

    def test_packaged_toolbelt_carries_the_rule(self):
        rule = Path(__file__).resolve().parents[2] / "profiles/package/rules/agentihooks-toolbelt.md"
        text = rule.read_text()
        assert self.OLD not in text
        assert "Publish an artifact only when the operator asked for that file" in text
        assert RULE in text


def delete(slug, row_id, op="artifact_delete"):
    return storage.apply_ops(slug, ops=[{"op": op, "id": f"{op}-{row_id}", "target": row_id}])


class TestTrash:
    def test_delete_moves_the_artifact_to_the_trash_and_keeps_its_file(self, slug):
        add_task(slug, "w5", artifact=True)
        state, _ = publish(slug, "a-del", task="w5")
        file_id = state["artifacts"][0]["file"]["id"]
        state, rejected = delete(slug, "a-del")
        assert rejected == [] and state["artifacts"] == []
        trashed = state["artifact_trash"][0]
        assert trashed["id"] == "a-del" and isinstance(trashed["deleted_at"], int)
        assert artifacts.path_of(slug, file_id).is_file()

    def test_restore_brings_it_back(self, slug):
        add_task(slug, "w6", artifact=True)
        publish(slug, "a-back", task="w6")
        delete(slug, "a-back")
        state, rejected = delete(slug, "a-back", op="artifact_restore")
        assert rejected == [] and state["artifact_trash"] == []
        assert [a["id"] for a in state["artifacts"]] == ["a-back"] and "deleted_at" not in state["artifacts"][0]

    def test_only_the_operator_deletes(self):
        with pytest.raises(ValueError):
            core.check_op({"op": "artifact_delete", "id": "d", "target": "a", "by": "eng"})
        with pytest.raises(ValueError):
            core.check_op({"op": "artifact_restore", "id": "d"})

    def test_a_deletion_owes_no_agent_a_reaction(self):
        assert {"artifact deleted", "artifact restored"} <= set(ledger_gate.IGNORED_KINDS)

    def test_a_trash_row_older_than_thirty_days_goes_with_its_file(self, slug):
        add_task(slug, "w7", artifact=True)
        state, _ = publish(slug, "a-old", task="w7", data=b"# Old plan\n")
        file_id = state["artifacts"][0]["file"]["id"]
        with patch.object(core, "now_ms", return_value=1_000):
            delete(slug, "a-old")
        with patch.object(core, "now_ms", return_value=1_000 + 29 * DAY_MS):
            state, _ = storage.apply_ops(slug)
        assert [r["id"] for r in state["artifact_trash"]] == ["a-old"]
        with patch.object(core, "now_ms", return_value=1_001 + 30 * DAY_MS):
            state, _ = storage.apply_ops(slug)
        assert state["artifact_trash"] == []
        with pytest.raises(ValueError):
            artifacts.path_of(slug, file_id)

    def test_an_expired_file_another_entry_still_uses_is_kept(self, slug):
        add_task(slug, "w8", artifact=True)
        shot = png(5, 5)
        publish(slug, "a-one", task="w8", data=shot)
        state, _ = publish(slug, "a-two", task="w8", data=shot)
        file_id = state["artifacts"][0]["file"]["id"]
        with patch.object(core, "now_ms", return_value=1_000):
            delete(slug, "a-one")
        with patch.object(core, "now_ms", return_value=2_000 + 30 * DAY_MS):
            storage.apply_ops(slug)
        assert artifacts.path_of(slug, file_id).is_file()


class TestPurge:
    def test_purge_deletes_every_artifact_and_trash_row_with_their_files(self, slug):
        add_task(slug, "w10", artifact=True)
        files = [
            publish(slug, f"a-{n}", task="w10", data=f"# Plan {n}\n".encode())[0]["artifacts"][-1] for n in range(3)
        ]
        delete(slug, "a-0")
        attached = media.store(slug, png(7, 7))
        image = {"op": "add", "thread": "chat", "id": "m-pic", "text": "look", "attachments": [attached]}
        storage.apply_ops(slug, ops=[image])
        shared = {"op": "artifact_add", "id": "a-pic", "by": AGENT, "task": "w10", "title": "Pic", "file": attached}
        storage.apply_ops(slug, ops=[shared])
        state, rejected = storage.apply_ops(slug, ops=[{"op": "artifact_purge", "id": "p", "by": AGENT}])
        assert rejected == [] and state["artifacts"] == [] and state["artifact_trash"] == []
        for row in files:
            with pytest.raises(ValueError):
                artifacts.path_of(slug, row["file"]["id"])
        assert media.path_of(slug, attached["id"]).is_file()
        event = state["_meta"]["events"][-1]
        assert (event["kind"], event["by"], event["count"]) == ("artifacts purged", AGENT, 4)

    def test_purge_needs_a_member(self, slug):
        assert storage.apply_ops(slug, ops=[{"op": "artifact_purge", "id": "p2", "by": "stranger"}])[1] == ["p2"]
        with pytest.raises(ValueError):
            core.check_op({"op": "artifact_purge", "id": "p3"})


def test_a_purge_summary_prints_the_count(capsys):
    args = ledger.build_parser().parse_args(["--slug", "cli", "--as", AGENT, "artifact-purge"])
    with (
        patch.object(
            ledger,
            "call",
            return_value={
                "artifacts": [],
                "artifact_trash": [],
                "_meta": {"events": [{"kind": "artifacts purged", "count": 4}]},
            },
        ),
        patch.object(ledger, "resource", side_effect=[[{"kind": "artifacts purged", "count": 4}], {"artifacts": 0}]),
    ):
        ledger.cmd_artifact_purge(args)
    assert json.loads(capsys.readouterr().out) == {"purged": 4, "artifacts": 0}


class TestDetails:
    def test_a_published_row_and_its_event_carry_every_field(self, slug):
        add_task(slug, "d1", artifact=True)
        state, _ = publish(slug, "a-full", task="d1")
        row = state["artifacts"][0]
        assert set(row) == {"id", "title", "by", "task", "at", "file"}
        assert (row["id"], row["title"], row["by"], row["task"]) == ("a-full", "Plan", AGENT, "d1")
        assert row["at"] == state["_meta"]["updated_at"]
        event = state["_meta"]["events"][-1]
        assert {k: event[k] for k in ("by", "kind", "target", "id", "text")} == {
            "by": AGENT,
            "kind": "artifact added",
            "target": "tasks/d1",
            "id": "a-full",
            "text": "Plan",
        }
        assert publish(slug, "a-full", task="d1")[1] == []
        assert [a["id"] for a in storage.apply_ops(slug)[0]["artifacts"]] == ["a-full"]

    def test_a_stranger_or_an_unknown_task_is_refused_even_with_a_request(self, slug):
        storage.apply_ops(slug, ops=[{"op": "add", "thread": "chat", "id": "m-want", "text": "Draw me a logo"}])
        file = artifacts.store(slug, "x.md", MARKDOWN)
        base = {"op": "artifact_add", "title": "Logo", "file": file, "request": "m-want"}
        stranger = {**base, "id": "a-who", "by": "stranger", "task": ""}
        ghost = {**base, "id": "a-ghost", "by": AGENT, "task": "nope"}
        state, rejected = storage.apply_ops(slug, ops=[stranger, ghost])
        assert rejected == ["a-who", "a-ghost"] and state["artifacts"] == []
        assert artifacts.REFUSED not in state["_meta"]["warnings"]

    def test_a_deleted_operator_message_is_no_request(self, slug):
        storage.apply_ops(slug, ops=[{"op": "add", "thread": "chat", "id": "m-gone", "text": "Draw me a logo"}])
        storage.apply_ops(slug, ops=[{"op": "delete", "thread": "chat", "id": "m-gone"}])
        assert publish(slug, "a-late", request="m-gone")[1] == ["a-late"]

    def test_delete_and_restore_are_recorded_and_repeat_safely(self, slug):
        add_task(slug, "d2", artifact=True)
        publish(slug, "a-one", task="d2", data=b"# One\n")
        publish(slug, "a-two", task="d2", data=b"# Two\n")
        state, _ = delete(slug, "a-one")
        event = state["_meta"]["events"][-1]
        assert (event["by"], event["kind"], event["target"], event["id"], event["text"]) == (
            "operator",
            "artifact deleted",
            "artifacts",
            "a-one",
            "Plan",
        )
        assert delete(slug, "a-one")[1] == []
        assert delete(slug, "a-none")[1] == ["artifact_delete-a-none"]
        state, _ = delete(slug, "a-one", op="artifact_restore")
        assert state["_meta"]["events"][-1]["kind"] == "artifact restored"
        assert delete(slug, "a-one", op="artifact_restore")[1] == []
        assert delete(slug, "a-none", op="artifact_restore")[1] == ["artifact_restore-a-none"]

    def test_the_trash_keeps_a_row_on_its_thirtieth_day_and_bumps_the_revision_when_it_expires(self, slug):
        add_task(slug, "d3", artifact=True)
        publish(slug, "a-day", task="d3", data=b"# Day\n")
        with patch.object(core, "now_ms", return_value=1_000):
            delete(slug, "a-day")
        with patch.object(core, "now_ms", return_value=1_000 + 30 * DAY_MS):
            state, _ = storage.apply_ops(slug)
        assert [r["id"] for r in state["artifact_trash"]] == ["a-day"]
        rev = state["_meta"]["rev"]
        with patch.object(core, "now_ms", return_value=1_001 + 30 * DAY_MS):
            state, _ = storage.apply_ops(slug)
        assert state["artifact_trash"] == [] and state["_meta"]["rev"] == rev + 1

    def test_purge_records_itself_and_survives_a_file_already_gone(self, slug):
        add_task(slug, "d4", artifact=True)
        state, _ = publish(slug, "a-lost", task="d4", data=b"# Lost\n")
        artifacts.path_of(slug, state["artifacts"][0]["file"]["id"]).unlink()
        storage.apply_ops(slug, ops=[{"op": "add", "thread": "chat", "id": "m-plain", "text": "No picture here"}])
        state, rejected = storage.apply_ops(slug, ops=[{"op": "artifact_purge", "id": "p-lost", "by": AGENT}])
        event = state["_meta"]["events"][-1]
        assert rejected == [] and (event["target"], event["id"], event["count"]) == ("artifacts", "p-lost", 1)

    def test_a_ledger_without_a_trash_gets_an_empty_one(self, slug):
        state = storage.export_document(slug)
        del state["artifact_trash"]
        core.paths(slug)[1].write_text(json.dumps(state))
        assert storage.apply_ops(slug)[0]["artifact_trash"] == []

    def test_operation_shapes_name_what_they_take(self):
        good = {
            "op": "artifact_add",
            "id": "a",
            "by": "eng",
            "task": "",
            "title": "Plan",
            "file": {"id": "a" * 64 + ".md"},
        }
        assert core.check_op(good) is None
        purge = {"op": "artifact_purge", "id": "p", "by": "eng"}
        assert core.check_op(purge) is None
        for bad in (
            {**purge, "extra": 1},
            {**purge, "by": "operator"},
            {**purge, "by": "1 bad"},
            {"op": "artifact_purge", "id": "p"},
        ):
            with pytest.raises(ValueError, match="^artifact_purge takes id and by, an agent name other than operator$"):
                core.check_op(bad)
        for kind in ("artifact_delete", "artifact_restore"):
            move = {"op": kind, "id": "d", "target": "a-1"}
            assert core.check_op(move) is None
            for bad in ({**move, "by": "eng"}, {**move, "target": 7}, {**move, "target": ""}, {"op": kind, "id": "d"}):
                with pytest.raises(
                    ValueError, match=f"^{kind} is the operator's and takes only id and target, an artifact id$"
                ):
                    core.check_op(bad)

    def test_task_add_keeps_gain_contract_workspace_and_artifact(self, slug):
        contract = {"must": "logo drawn", "check": "look", "judge": "operator"}
        add_task(slug, "d5", gain=2, contract=contract, workspace="/work", artifact=True)
        task = next(t for t in storage.apply_ops(slug)[0]["tasks"] if t["id"] == "d5")
        assert (task["gain"], task["contract"], task["workspace"], task["artifact"]) == (2, contract, "/work", True)

    def test_the_publish_goes_to_the_named_ledger(self, tmp_path):
        doc = tmp_path / "logo.md"
        doc.write_bytes(MARKDOWN)
        args = ledger.build_parser().parse_args(["--slug", "cli", "--as", AGENT, "artifact", str(doc), "Logo"])
        with (
            patch.object(ledger, "upload_artifact", return_value={"id": "a" * 64 + ".md"}),
            patch.object(ledger, "call", return_value={}) as call,
        ):
            ledger.cmd_artifact(args)
        assert call.call_args.args[0] == "cli"
