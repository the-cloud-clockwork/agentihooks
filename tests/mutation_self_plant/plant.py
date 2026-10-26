import json
import sys
from pathlib import Path

from scripts.ci_mutation.runner import prepare_workspace, run_gate

SAMPLE = "scripts/mutation_self_plant_sample.py"
TEST = "tests/test_mutation_self_plant_sample.py"
SOURCE = "def fee(amount, rate):\n    return amount * rate + 1\n"
TESTS = {
    "kill": "from scripts.mutation_self_plant_sample import fee\n\n\ndef test_fee():\n    assert fee(3, 2) == 7\n",
    "survive": (
        "from scripts.mutation_self_plant_sample import fee\n\n\ndef test_fee():\n    assert fee(3, 2) is not None\n"
    ),
}


def plant(root: Path, work: Path, mode: str) -> dict:
    prepare_workspace(root, work, [SAMPLE], [TEST])
    (work / "tests").rename(work / "removed-tests")
    (work / "tests").mkdir()
    (work / SAMPLE).write_text(SOURCE)
    (work / TEST).write_text(TESTS[mode])
    report = run_gate(work, {SAMPLE: {1, 2}}, work / "evidence", 300)
    print(json.dumps(report), flush=True)
    assert report["not_mutated"] == [], f"The gate did not mutate the sample: {report['not_mutated']}"
    [result] = report["files"]
    counts = result["counts"]
    if mode == "kill":
        assert counts.get("killed"), f"The real test graded no killed mutant: {counts}"
        assert set(counts) == {"killed"} and not report["failed"], f"The real test left a survivor: {counts}"
    else:
        assert counts.get("survived"), f"The weakened test let no mutant survive: {counts}"
        assert report["failed"], f"The gate passed a weakened test that kills nothing: {counts}"
    return report


if __name__ == "__main__":
    root = Path.cwd()
    plant(root, root / ".mutation-self-plant", sys.argv[1])
