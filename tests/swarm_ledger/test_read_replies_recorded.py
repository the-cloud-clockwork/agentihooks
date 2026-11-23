"""The replies the orders and focus reads serve, as text: a recorded run of each read over one ledger, of each read
one byte past the reply limit and of a target that does not exist."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm.test_freeze import record, task
from tests.swarm.test_order_check import SLUG, served

pytestmark = pytest.mark.unit

RECORDED = Path(__file__).with_name("read_replies_recorded.json")
READS = ("orders", "focus", "focus/phases/p1", "focus/tasks/t2")
NOW_S = 600.0


def rows() -> list[dict[str, Any]]:
    return [
        task("t1"),
        task("t2", depends_on=["t9"], ordered={"at": 9, "by": "operator"}, title="Fix the writer"),
        task("t3", state="claimed", claimed_by="engineer@one"),
        task("t4", state="pr"),
        task("t5", state="done"),
        task("t9", phase="p2"),
    ]


def no_fix(services: Any, slug: str) -> str:
    return ""


def now() -> float:
    return NOW_S


def refusal(services: Any, path: str) -> list[Any]:
    with pytest.raises(APIError) as refused:
        routes.ledger_read(services, SLUG, path, {})
    return [refused.value.status, refused.value.code, str(refused.value)]


def read_run(services: Any, patch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The text of each read, what each answers one byte past the reply limit, and the answer for a target that does
    not exist."""
    found: dict[str, Any] = {}
    for path in READS:
        whole = routes.ledger_read(services, SLUG, path, {})
        found[path] = json.dumps(whole)
        with patch.context() as limited:
            limited.setattr(routes.resources, "MAX_REPLY", routes.resources.reply_size(whole) - 1)
            found[f"{path} past the limit"] = refusal(services, path)
    found["focus/tasks/none"] = refusal(services, "focus/tasks/none")
    return found


@pytest.mark.feature("ledger-verdict-reads-one-reply")
def test_a_recorded_run_of_the_orders_and_focus_reads_keeps_the_text_of_every_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = served(tmp_path, rows(), record("phases/p1", "focus"), record("tasks/t9", "focus"))
    monkeypatch.setattr(routes, "time", SimpleNamespace(time=now))
    monkeypatch.setattr(routes, "fix_phase", no_fix)
    found = read_run(services, monkeypatch)
    assert found == json.loads(RECORDED.read_text()), f"the run now leaves: {json.dumps(found)}"
