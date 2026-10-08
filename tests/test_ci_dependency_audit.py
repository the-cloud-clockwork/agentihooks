import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_dependency_audit as audit

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

BASE = (
    "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
    " || github.event.before || inputs.base }}"
)


def _report(*deps):
    return json.dumps({"dependencies": list(deps), "fixes": []})


def _dep(name, version, *ids):
    return {"name": name, "version": version, "vulns": [{"id": i, "fix_versions": []} for i in ids]}


def test_findings_collect_every_advisory_once():
    report = _report(_dep("urllib3", "1.26.4", "PYSEC-1", "PYSEC-1", "PYSEC-2"), _dep("idna", "3.7"))
    assert audit.findings(report) == {("urllib3", "1.26.4", "PYSEC-1"), ("urllib3", "1.26.4", "PYSEC-2")}


def test_findings_ignore_skipped_dependencies():
    assert audit.findings(_report({"name": "local", "skip_reason": "not on PyPI"})) == set()


@pytest.mark.parametrize("report", ["", "not json", "{}", '{"dependencies": [1]}'])
def test_a_report_that_is_not_a_pip_audit_report_is_an_error(report):
    with pytest.raises(audit.AuditError):
        audit.findings(report)


def test_new_findings_ignore_advisories_the_base_already_carries_at_any_version():
    head = {("urllib3", "1.26.5", "PYSEC-1"), ("urllib3", "1.26.5", "PYSEC-2"), ("idna", "2.0", "PYSEC-3")}
    base = {("urllib3", "1.26.4", "PYSEC-1")}
    assert audit.new_findings(head, base) == [("idna", "2.0", "PYSEC-3"), ("urllib3", "1.26.5", "PYSEC-2")]


def test_resolve_compiles_every_extra_without_the_test_excludes(tmp_path, monkeypatch):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/test-excludes.txt").write_text("grpc\n")
    calls = []
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda cmd, **_: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    out = tmp_path / "head.txt"
    assert audit.resolve(tmp_path, out) == out
    cmd = calls[0]
    assert cmd[:4] == ["uv", "pip", "compile", str(tmp_path / "pyproject.toml")]
    assert {"--all-extras", "--no-header", "--no-annotate"} <= set(cmd)
    assert cmd[cmd.index("--python-version") + 1] == "3.12"
    assert cmd[cmd.index("--excludes") + 1] == str(tmp_path / ".github/test-excludes.txt")
    assert cmd[cmd.index("-o") + 1] == str(out)


def test_a_failed_resolve_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audit.subprocess, "run", lambda cmd, **_: subprocess.CompletedProcess(cmd, 2, "", "no solution")
    )
    with pytest.raises(audit.AuditError, match="no solution"):
        audit.resolve(tmp_path, tmp_path / "head.txt")


def test_audit_runs_the_pinned_pip_audit_on_the_resolved_pins_alone(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda cmd, **_: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, _report(), ""),
    )
    assert audit.audit(tmp_path / "head.txt") == _report()
    cmd = calls[0]
    assert cmd[:2] == ["uvx", "pip-audit==2.10.1"]
    assert cmd[cmd.index("-r") + 1] == str(tmp_path / "head.txt")
    assert {"--no-deps", "--disable-pip"} <= set(cmd)
    assert cmd[cmd.index("--format") + 1] == "json"


def _stub(monkeypatch, reports):
    monkeypatch.setattr(audit, "resolve", lambda root, out: out.with_name(Path(root).name))
    monkeypatch.setattr(audit, "audit", lambda requirements: reports[requirements.name])


def test_main_fails_on_an_advisory_new_against_the_base_and_reports_every_count(monkeypatch, capsys):
    _stub(
        monkeypatch,
        {
            "head": _report(_dep("urllib3", "1.26.4", "PYSEC-1", "PYSEC-2"), _dep("idna", "3.7", "PYSEC-3")),
            "base": _report(_dep("idna", "3.6", "PYSEC-3")),
        },
    )
    assert audit.main(["--base", "base", "--head", "head"]) == 1
    out = capsys.readouterr().out
    assert "3 known vulnerabilities on head, 1 on base, 2 new" in out
    assert "::error::urllib3 1.26.4 carries PYSEC-1" in out
    assert "::error::urllib3 1.26.4 carries PYSEC-2" in out
    assert "idna" not in out


def test_main_passes_when_head_adds_no_advisory(monkeypatch, capsys):
    _stub(monkeypatch, {"head": _report(_dep("idna", "3.7")), "base": _report(_dep("idna", "3.6", "PYSEC-3"))})
    assert audit.main(["--base", "base", "--head", "head"]) == 0
    assert "0 known vulnerabilities on head, 1 on base, 0 new" in capsys.readouterr().out


def test_main_is_red_when_an_audit_produces_no_report(monkeypatch):
    _stub(monkeypatch, {"head": "", "base": _report()})
    with pytest.raises(audit.AuditError):
        audit.main(["--base", "base", "--head", "head"])


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def test_dependency_audit_is_a_parallel_need_of_gate_required_graded_by_the_base():
    jobs = _workflow()["jobs"]
    job = jobs["dependency-audit"]
    assert "dependency-audit" in jobs["gate-required"]["needs"]
    assert "needs" not in job
    assert not {"if", "continue-on-error"} & set(job)
    assert all("if" not in step and "continue-on-error" not in step for step in job["steps"])
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    base, grade = job["steps"][-2:]
    assert base["env"]["BASE"] == BASE
    assert base["run"] == 'git worktree add --detach "$RUNNER_TEMP/base" "$BASE"'
    assert grade["run"] == (
        'grader="$RUNNER_TEMP/base"\n'
        '[[ -f "$grader/scripts/ci_dependency_audit.py" ]] || grader="$GITHUB_WORKSPACE"\n'
        'cd "$grader"\n'
        'python -m scripts.ci_dependency_audit --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
    )


def test_dependabot_updates_pip_and_actions_weekly_into_dev_in_one_group_each():
    config = yaml.safe_load((_ROOT / ".github/dependabot.yml").read_text())
    assert config["version"] == 2
    updates = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(updates) == {"pip", "github-actions"}
    for ecosystem, update in updates.items():
        assert update["directory"] == "/"
        assert update["target-branch"] == "dev"
        assert update["schedule"]["interval"] == "weekly"
        assert update["cooldown"] == {"default-days": 7}
        assert update["groups"] == {ecosystem: {"patterns": ["*"]}}
