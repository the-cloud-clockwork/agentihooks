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
        timeout=30,
    )
    records = json.loads(output.read_text())
    replies = [r["response"] for r in records if "response" in r]
    assert [r["status"] for r in replies] == [200] * 9
    documents = [json.loads(r["body"]) for r in replies]
    assert documents[2]["phases"][0]["comments"] == documents[3]["phases"][0]["comments"]
    assert len(documents[3]["phases"][0]["comments"]) == 1
    assert documents[4]["phases"][0]["done"] is True
    assert documents[5]["rejected"] == ["phases/p1/done"]
    assert documents[6]["tasks"][0]["state"] == "claimed"
    assert documents[7]["tasks"][0]["state"] == documents[8]["tasks"][0]["state"] == "done"
    [seed] = [json.loads(r["stale_seed"]["body"]) for r in records if "stale_seed" in r]
    assert seed["overview"] == "Agent edit" and seed["phases"][0]["done"] is True
    assert [r["bin"]["status"] for r in records if "bin" in r] == [200, 200, 404]
    assert len(records[-1]["inbox"]) == 2
    assert {r["address"] for r in records[-1]["inbox"]} == {"master@replay"}
