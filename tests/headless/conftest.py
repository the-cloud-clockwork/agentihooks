import json
import os
import sys

import pytest

SCRIPT = """#!{python}
import json
import sys
from pathlib import Path

log = Path({log!r})
calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
args = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[-2:] == ["debug", "models"]:
    print({listing!r})
    raise SystemExit(0)
answers = [call for call in calls if call[-2:] != ["debug", "models"]]
replies = {replies!r}
reply = replies[min(len(answers), len(replies) - 1)]
if "-o" in args:
    Path(args[args.index("-o") + 1]).write_text(reply)
else:
    print(reply)
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, replies: list[str], listing: str = "") -> list:
        log = tmp_path / f"{name}.log"
        path = bin_dir / name
        path.write_text(SCRIPT.format(python=sys.executable, log=str(log), replies=replies, listing=listing))
        path.chmod(0o755)
        return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return install
