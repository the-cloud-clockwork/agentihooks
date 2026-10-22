import shutil

from scripts.swarm_ledger import ledger_artifacts, plan_ranges
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_ranges
from tests.swarm_ledger.test_plan_kind import add
from tests.swarm_ledger.test_plan_publish import task

plan_ledger = test_plan_ranges.plan_ledger
published = test_plan_ranges.published


def test_a_task_add_reads_a_published_plan_another_server_stored(published):
    shutil.rmtree(ledger_artifacts.media.folder(published))
    state, rejected = add(published, "first", plan_slice="first")
    assert (rejected, state["_meta"]["warnings"]) == ([], [])
    assert task(state, "first")["plan_lines"] == "4-6"


def test_a_stored_artifact_is_served_without_its_disk_copy(published):
    state = core.sync(published)[0]
    [artifact] = state["artifacts"]
    file_id = artifact["file"]["id"]
    shutil.rmtree(ledger_artifacts.media.folder(published))
    assert plan_ranges.stored_text(state["phases"][0]["plan_ref"], state) == test_plan_ranges.PLAN
    assert ledger_artifacts.read(published, file_id) == test_plan_ranges.PLAN.encode()
    assert ledger_artifacts.entry(published, file_id) == artifact["file"]
