import json
import subprocess
import sys
from pathlib import Path


def test_recorded_requests_exercise_the_required_behavior(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "responses.json"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("repository_replay.py")),
            str(root),
            str(tmp_path / "ledger"),
            str(output),
        ],
        check=True,
        timeout=60,
    )
    records = json.loads(output.read_text())
    assert records[0]["created"] is True
    exchanges = [r for r in records if "response" in r]
    assert [r["response"]["status"] for r in exchanges] == [200] * 9
    replies = [json.loads(r["response"]["body"]) for r in exchanges]
    stored = [r["stored"] for r in exchanges]
    assert stored[2]["phases"][0]["comments"] == stored[3]["phases"][0]["comments"]
    assert len(stored[3]["phases"][0]["comments"]) == 1
    assert stored[4]["phases"][0]["done"] is True
    assert replies[5]["rejected"] == ["phases/p1/done"]
    assert stored[5]["phases"][0]["done"] is True
    assert stored[6]["tasks"][0]["state"] == "claimed"
    assert stored[7]["tasks"][0]["state"] == stored[8]["tasks"][0]["state"] == "done"
    assert [r["bin"]["status"] for r in records if "bin" in r] == [200, 200, 404]
    assert len(records[-1]["inbox"]) == 2
    assert {r["address"] for r in records[-1]["inbox"]} == {"master@replay"}
