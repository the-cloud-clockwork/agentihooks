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


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, env={**os.environ, **_GIT_ENV})


def _commit(repo, files, message):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _notes(repo, next_version):
    out = repo.parent / f"notes-{next_version}.md"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ci_release.sh"), "notes", "HEAD", next_version, str(out)],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return out.read_text()


def test_release_notes_assemble_every_fragment_added_since_the_previous_tag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, {"changelog.d/old.md": "### Fixed\n\n- released before\n"}, "old")
    assert _notes(repo, "1.0.0") == "## Changelog\n\n### Fixed\n\n- released before\n\n"
    _git(repo, "tag", "v1.0.0")
    _commit(
        repo,
        {
            "changelog.d/old.md": "### Fixed\n\n- released before, typo fixed\n",
            "changelog.d/README.md": "# Changelog fragments\n\n- not an entry\n",
            "changelog.d/branch-a.md": "### Added\n\n- alpha feature\n#123 wrapped line\n\n### Fixed\n\n- alpha fix\n",
            "changelog.d/branch-b.md": "### Added\n\n- beta feature\n",
        },
        "parallel entries",
    )
    _git(repo, "tag", "v1.0.1")

    assert _notes(repo, "1.0.1") == (
        "## Changelog\n\n"
        "### Added\n\n- alpha feature\n#123 wrapped line\n- beta feature\n\n"
        "### Fixed\n\n- alpha fix\n\n"
    )
    assert _notes(repo, "1.0.2") == ""
