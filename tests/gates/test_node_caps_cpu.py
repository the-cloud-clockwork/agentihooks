import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

PROBE = r"""
import cProfile
import io
import json
import pstats
import sys
from pathlib import Path
from time import process_time

root = Path(sys.argv[1])
profile = cProfile.Profile()
profile.enable()
started = process_time()
if sys.argv[2] == "entry":
    from scripts.gates import entry
else:
    from scripts.gates import node_caps
import_cpu = process_time() - started
profile.disable()
report = {"import": sys.argv[2], "import_cpu": import_cpu, "files": []}
from scripts.gates import Call, Who, entry, node_caps

who = Who()
for size in (65536, 1048576, 8388608):
    text = "padding: unchanged\n" * (size // 19) + "maxRunners: 8\n"
    target = root / "runners.yaml"
    target.write_text(text)
    data = {"file_path": str(target), "old_string": "8", "new_string": "7"}
    for tool in ("Edit", "MultiEdit", "Write"):
        payload = data if tool == "Edit" else (
            {"file_path": str(target), "edits": [data]} if tool == "MultiEdit" else
            {"file_path": str(target), "content": text.replace("8", "7", 1)}
        )
        call = Call(tool, payload)
        started = process_time()
        before, after = node_caps._edited(call)
        edited_cpu = process_time() - started
        started = process_time()
        found = node_caps.raises(before, after)
        regex_cpu = process_time() - started
        started = process_time()
        decision = node_caps.NodeCaps().decide(call, who, None)
        decide_cpu = process_time() - started
        started = process_time()
        code = entry.main(["node-caps"], io.StringIO(json.dumps({"tool_name": tool, "tool_input": payload})), {}, root)
        entry_cpu = process_time() - started
        assert not found and decision.allowed and code == 0
        report["files"].append({"bytes": len(text), "tool": tool, "edited_cpu": edited_cpu,
                                "regex_cpu": regex_cpu, "decide_cpu": decide_cpu, "entry_cpu": entry_cpu})
stream = io.StringIO()
pstats.Stats(profile, stream=stream).sort_stats("cumulative").print_stats(12)
report["import_profile"] = stream.getvalue()
print(json.dumps(report))
"""


@pytest.mark.parametrize("module", ["entry", "node"], ids=["cold-entry", "cold-node"])
def test_node_caps_cpu_split(tmp_path: Path, module: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path), module],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    report = json.loads(completed.stdout)
    warnings.warn(json.dumps(report), stacklevel=1)
    assert report["import"] == module
    assert len(report["files"]) == 9


@pytest.mark.feature("node-caps-named-import")
def test_node_caps_dispatch_imports_only_its_gate(tmp_path: Path) -> None:
    program = """
import importlib.abc
import io
import sys

attempted: list[str] = []

class NamedGateOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname.startswith("scripts.gates.") and fullname.rsplit(".", 1)[-1] not in {
            "entry", "base", "lift", "log", "modes", "node_caps", "verdicts"
        }:
            attempted.append(fullname)
            raise AssertionError("unrelated gate imported: " + fullname)

sys.meta_path.insert(0, NamedGateOnly())
from pathlib import Path
from scripts.gates import entry
from scripts.gates.node_caps import NodeCaps
assert isinstance(entry.HOST_GATES["node-caps"], NodeCaps)
assert entry.main(["node-caps"], io.StringIO('{"tool_name":"Bash","tool_input":{"command":"printf ok"}}'), {}, Path(sys.argv[1])) == 0
assert not attempted, attempted
"""
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
