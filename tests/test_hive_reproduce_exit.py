"""The hive reproduce proof removes the hive member it invited when it exits, unless told to keep it."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "packaging" / "hive-reproduce.sh"
MEMBER = "abcdef0123456789"
STUBS = {
    "agentihooks": (
        '#!/usr/bin/env bash\necho "$*" >> "$CALLS"\n'
        f'[[ "$1 $2" == "hive invite" ]] && echo "invited hive member {MEMBER} as $3"\nexit 0\n'
    ),
    "kubectl": (
        '#!/usr/bin/env bash\ncase "$*" in\n  *"get externalsecret -o name"*) printf "a\\nb\\nc\\nd\\n" ;;\n'
        "  *annotate*) exit 1 ;;\nesac\nexit 0\n"
    ),
    "probe-python": "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n",
}


def _run(tmp_path: Path, keep: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "HIVE_PYTHON": str(bin_dir / "probe-python"),
        "HIVE_KEEP": keep,
    }
    done = subprocess.run(["bash", str(SCRIPT), "demo"], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    return calls.read_text().splitlines()


@pytest.mark.feature("hive-quiet-sweep")
def test_a_reproduce_run_that_stops_after_its_invite_removes_the_member(tmp_path: Path) -> None:
    """A run failing after its invite still removes the invited member through hive remove."""
    calls = _run(tmp_path, "no")
    assert calls == [
        "hive invite demo --path k8s/agentihooks-hive-demo --like k8s/agentihooks-swarm-workstation-hive",
        f"hive remove {MEMBER} --label demo --path k8s/agentihooks-hive-demo --wait 300",
    ]


@pytest.mark.feature("hive-quiet-sweep")
def test_a_reproduce_run_told_to_keep_its_hive_leaves_it(tmp_path: Path) -> None:
    """HIVE_KEEP=yes keeps the invited member for a reproduction the operator wants to inspect."""
    assert _run(tmp_path, "yes") == [
        "hive invite demo --path k8s/agentihooks-hive-demo --like k8s/agentihooks-swarm-workstation-hive"
    ]
