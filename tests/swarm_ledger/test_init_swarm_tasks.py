import re
import shlex
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "swarm_ledger"))
import ledger  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_kinds  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.unit

GUIDE = ROOT / "profiles/package/skills/init-swarm/work-beyond-code.md"
SLUG = "init-swarm-sample-2026-01-01"
PREFIX = "agentihooks ledger --slug <slug> task add "
BEYOND_CODE = set(ledger_kinds.KINDS) - {"code", "ci", "plan"}
FIELDS_BEFORE_KINDS = {"op", "id", "by", "task", "title", "lane", "phase", "description", "depends_on", "territory"}


def sample_commands():
    blocks = re.findall(r"```bash\n(.*?)```", GUIDE.read_text(), re.S)
    lines = "\n".join(blocks).replace("\\\n", " ").splitlines()
    return [shlex.split(line) for line in lines if line.startswith(PREFIX)]


def task_op(argv):
    sent = []
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "init-swarm", *argv[4:]])
    with unittest.mock.patch.object(ledger, "send", lambda a, kind, /, **f: sent.append(ledger.op(kind, a, **f))):
        ledger.cmd_task(args)
    return sent[0]


def sample_ledger():
    phases = [{"title": "find the cause"}, {"title": "fix and tune"}, {"title": "decide"}]
    content = {"title": "Sample", "overview": "o", "sources": [str(GUIDE)], "phases": phases}
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    ops = [task_op(argv) for argv in sample_commands()]
    state, rejected = core.sync(SLUG, ops=ops)
    return ops, {t["id"]: t for t in state["tasks"]}, rejected


def mark_done(task):
    return {
        "op": "task_update",
        "id": f"done-{task}",
        "by": "swarm",
        "item": f"tasks/{task}",
        "fields": {"state": "done"},
    }


def test_the_sample_plan_has_a_task_of_every_kind_beyond_code_and_a_code_task():
    kinds = {op.get("kind", "code") for op in map(task_op, sample_commands())}
    assert BEYOND_CODE | {"code"} <= kinds


def test_the_ledger_accepts_every_sample_task_with_its_kind_and_a_full_contract():
    ops, tasks, rejected = sample_ledger()
    assert rejected == []
    for op in ops:
        if op.get("kind") in BEYOND_CODE:
            task = tasks[op["task"]]
            assert task["kind"] == op["kind"]
            assert set(task["contract"]) == set(ledger_kinds.CONTRACT_KEYS)
            assert all(value.strip() for value in task["contract"].values())


def test_a_sample_task_beyond_code_cannot_be_done_without_its_proof():
    ops, _, _ = sample_ledger()
    done = [mark_done(op["task"]) for op in ops if op.get("kind") in BEYOND_CODE]
    _, rejected = core.sync(SLUG, ops=done)
    assert rejected == [op["id"] for op in done]


def test_a_code_task_is_written_as_before_task_kinds():
    ops, tasks, _ = sample_ledger()
    code = [op for op in ops if op.get("kind", "code") == "code"]
    assert code
    for op in code:
        assert set(op) <= FIELDS_BEFORE_KINDS
        assert ledger_kinds.kind(tasks[op["task"]]) == "code"
        assert "contract" not in tasks[op["task"]]
    _, rejected = core.sync(SLUG, ops=[mark_done(op["task"]) for op in code])
    assert rejected == []
