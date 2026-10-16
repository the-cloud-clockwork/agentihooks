import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "same_tree,expected",
    [(True, "next=2.17.0\nexisting=true\n"), (False, "next=2.17.1\nexisting=false\n")],
)
def test_a_rerun_reuses_the_existing_tree_tag_and_a_new_tree_gets_one_patch(tmp_path, same_tree, expected):
    tools = tmp_path / "bin"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "$1" in\n'
        '  tag) printf "v2.17.0\\nv2.16.0\\n" ;;\n'
        "  rev-parse)\n"
        '    if [[ "$2" == HEAD* ]]; then echo current; else echo "$TAG_TREE"; fi ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ci_release.sh"), "version"],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "TAG_TREE": "current" if same_tree else "previous",
            "REUSE_TREE": "true",
            "BUMP": "patch",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().endswith(expected)
