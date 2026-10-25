import pytest

from tests import test_ci_bot_updates as checks


@pytest.mark.parametrize("filename", ["extra.yml", "extra.yaml"])
def test_commit_check_still_checks_inline_jobs_beside_reusable_jobs(tmp_path, monkeypatch, filename):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / filename).write_text(
        "jobs:\n"
        "  reusable:\n"
        "    uses: ./.github/workflows/swarm-smoke.yml\n"
        "  inline:\n"
        "    steps:\n"
        "      - run: git commit -m forbidden\n"
    )
    monkeypatch.setattr(checks, "_ROOT", tmp_path)
    with pytest.raises(AssertionError):
        checks.test_ci_creates_no_commits_or_bot_pull_requests()
