import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "scripts/swarm_ledger/expected_page_change.json"
ORDER = "Operator: add the proof panel"
SECTION = '<section id="sec-proof"><details class="fold" id="proof-box" open><summary>Proof</summary><p>Added</p></details></section>'
SCRIPT = """
if (document.getElementById("sec-proof")) {
  const folds = JSON.parse(localStorage.getItem("plan-ledger:replay:fold") || "{}");
  localStorage.setItem("plan-ledger:replay:fold", JSON.stringify({ ...folds, "proof-box": true }));
  fetch("/api/v1/ledgers/replay/orders").catch(() => {});
}
"""


def run(tmp_path: Path, requests: int, extra: str = "") -> subprocess.CompletedProcess[str]:
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--system",
            "--python",
            sys.executable,
            "--constraint",
            str(ROOT / ".github/test-constraints.txt"),
            "playwright",
        ],
        check=True,
        timeout=120,
    )
    (tmp_path / "base").symlink_to(ROOT, target_is_directory=True)
    head = tmp_path / "head"
    shutil.copytree(ROOT / "scripts", head / "scripts", ignore=shutil.ignore_patterns("__pycache__", ".coverage*"))
    shell = head / "scripts/swarm_ledger/shell.html"
    anchor = '<section id="sec-notes"'
    assert shell.read_text().count(anchor) == 1
    shell.write_text(shell.read_text().replace(anchor, SECTION + extra + anchor))
    main = head / "scripts/swarm_ledger/static/js/main.js"
    main.write_text(main.read_text() + SCRIPT)
    contract = {
        "reviewed_by": ["page-addition-proof"],
        "added": [
            {"selector": "#sec-proof", "order": ORDER, "matches": 1},
            {"request": "GET /api/v1/ledgers/replay/orders", "order": ORDER, "matches": requests},
        ],
    }
    (head / CONTRACT).write_text(json.dumps(contract, indent=2) + "\n")
    fault = tmp_path / "fault-chat"
    shutil.copytree(head / "scripts", fault / "scripts")
    chat = fault / "scripts/swarm_ledger/static/js/chat.js"
    needle = 'return to && !text.startsWith("@") ? `@${to} ${text}` : text;'
    assert chat.read_text().count(needle) == 1
    chat.write_text(chat.read_text().replace(needle, "return text;"))
    logs = tmp_path / "logs"
    logs.mkdir()
    result = subprocess.run(
        ["bash", str(ROOT / "tests/swarm_ledger/equivalence.sh"), "page"],
        cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(logs)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    print(result.stdout)
    print(result.stderr)
    for log in sorted(logs.glob("*.log")):
        print(f"{log.name}: {log.read_text()[-4000:]}")
    return result


def first_difference(folder: Path) -> str:
    base = json.loads((folder / "base-page.json").read_text())
    head = json.loads((folder / "head-page.json").read_text())
    counts: dict[str, int] = {}
    first = ""
    for left, right in zip(base, head, strict=True):
        parts = {key: (left[key], right[key]) for key in ("requests", "dialogs", "errors")}
        parts.update({f"page.{key}": (value, right["page"][key]) for key, value in left["page"].items()})
        for part, (value, other) in parts.items():
            if value == other:
                continue
            counts[part] = counts.get(part, 0) + 1
            if first:
                continue
            if isinstance(value, str):
                at = next((i for i, (a, b) in enumerate(zip(value, other)) if a != b), min(len(value), len(other)))
                low = max(0, at - 400)
                first = f"{left['step']} {part} at {at}: base {value[low : at + 400]!r} head {other[low : at + 400]!r}"
            elif isinstance(value, list):
                only_base = [item for item in value if item not in other]
                only_head = [item for item in other if item not in value]
                first = f"{left['step']} {part}: only base {only_base} only head {only_head}"
            elif isinstance(value, dict):
                keys = sorted(set(value) | set(other))
                first = f"{left['step']} {part}: " + "; ".join(
                    f"{key}: base {value.get(key)!r} head {other.get(key)!r}"
                    for key in keys
                    if value.get(key) != other.get(key)
                )
            else:
                first = f"{left['step']} {part}: base {value!r} head {other!r}"
    return f"steps differing per part: {counts}\nfirst difference: {first}"


def steps_with(path: Path, needle: str) -> list[str]:
    return [record["step"] for record in json.loads(path.read_text()) if needle in json.dumps(record)]


def test_a_named_addition_passes_and_the_planted_chat_fault_is_still_rejected(tmp_path: Path) -> None:
    result = run(tmp_path, 2)
    assert result.returncode == 0, first_difference(tmp_path) + "\n" + result.stdout + result.stderr
    assert "PASS: identical base and head pages; planted chat fault rejected" in result.stdout
    for name in ("head-page.json", "fault-page.json"):
        assert steps_with(tmp_path / name, "proof-box") == []
        assert steps_with(tmp_path / name, "/orders") == []
    clean = json.loads((tmp_path / "head-page.json").read_text())
    planted = json.loads((tmp_path / "fault-page.json").read_text())
    chat = [(a["requests"], b["requests"]) for a, b in zip(clean, planted, strict=True) if a["step"] == "chat send"]
    assert chat and chat[0][0] != chat[0][1]


@pytest.mark.parametrize(
    ("requests", "extra", "message"),
    [
        (2, '<p id="proof-extra">Unnamed</p>', "base-page.json head-page.json differ"),
        (3, "", "GET /api/v1/ledgers/replay/orders names 3 requests but the head page makes 2"),
    ],
)
def test_an_unnamed_addition_or_a_wrong_count_still_fails(
    tmp_path: Path, requests: int, extra: str, message: str
) -> None:
    result = run(tmp_path, requests, extra)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
