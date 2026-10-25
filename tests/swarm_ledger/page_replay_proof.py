import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def test_page_replay_driver_rejects_the_chat_fault(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for name in ("base", "head"):
        (tmp_path / name).symlink_to(root, target_is_directory=True)
    fault = tmp_path / "fault-chat"
    shutil.copytree(root / "scripts", fault / "scripts", ignore=shutil.ignore_patterns("__pycache__", ".coverage*"))
    chat = fault / "scripts/swarm_ledger/static/js/chat.js"
    source = chat.read_text()
    needle = 'return to && !text.startsWith("@") ? `@${to} ${text}` : text;'
    assert source.count(needle) == 1
    chat.write_text(source)
    logs = tmp_path / "logs"
    logs.mkdir()
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(root / "tests/swarm_ledger/equivalence.sh"), "page"],
        cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(logs)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    print(f"page replay seconds: {time.monotonic() - started:.3f}")
    print(result.stdout)
    print(result.stderr)
    for log in sorted(logs.glob("*.log")):
        print(f"{log.name}: {log.read_text()}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "planted chat fault rejected" in result.stdout
    clean = json.loads((tmp_path / "head-page.json").read_text())
    planted = json.loads((tmp_path / "fault-page.json").read_text())
    clean_chat = next(record for record in clean if record["step"] == "chat send")
    planted_chat = next(record for record in planted if record["step"] == "chat send")
    assert clean_chat["requests"] != planted_chat["requests"]
