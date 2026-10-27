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


@pytest.mark.parametrize("git_failures,api_status", [(0, 0), (1, 0), (3, 0), (3, 1)])
def test_tag_push_retries_then_uses_the_same_app_for_the_exact_release_ref(tmp_path, git_failures, api_status):
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    for name, body in {
        "git": (
            'if [[ "$1" == rev-parse ]]; then echo release-commit; exit 0; fi\n'
            'echo "git $*" >> "$CALLS"\n'
            'count=$(cat "$COUNT" 2>/dev/null || echo 0)\n'
            'echo "$((count + 1))" > "$COUNT"\n'
            '[[ "$count" -ge "$GIT_FAILURES" ]]\n'
        ),
        "sleep": 'echo "sleep $*" >> "$CALLS"\n',
        "gh": ('[[ "$GH_TOKEN" == fixture-app ]]\necho "gh $*" >> "$CALLS"\nexit "$API_STATUS"\n'),
    }.items():
        tool = tools / name
        tool.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        tool.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ci_release.sh"), "push", "release-commit", "2.17.1"],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "COUNT": str(tmp_path / "count"),
            "GIT_FAILURES": str(git_failures),
            "API_STATUS": str(api_status),
            "GH_TOKEN": "fixture-app",
            "GITHUB_REPOSITORY": "the-cloud-clockwork/agentihooks",
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (api_status == 0), result.stderr
    events = calls.read_text().splitlines()
    attempts = min(git_failures + 1, 3)
    assert events[: attempts * 2 - 1] == [
        item
        for attempt in range(attempts)
        for item in (
            ["git push origin refs/tags/v2.17.1", "sleep 5"]
            if attempt < attempts - 1
            else ["git push origin refs/tags/v2.17.1"]
        )
    ]
    rest = events[attempts * 2 - 1 :]
    assert rest == (
        [
            "gh api --method POST repos/the-cloud-clockwork/agentihooks/git/refs -f ref=refs/tags/v2.17.1 -f sha=release-commit"
        ]
        if git_failures == 3
        else []
    )
