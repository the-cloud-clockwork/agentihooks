import json
from pathlib import Path

import pytest

from scripts import ci_access_audit

pytestmark = pytest.mark.unit

APPROVED = ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"]


def test_unapproved_collaborator_and_installed_app_are_reported():
    report = ci_access_audit.audit(
        APPROVED,
        [{"login": "nestorcolt"}, {"login": "stranger"}],
        [{"app_slug": "agenticore-app"}, {"app_slug": "claude"}, {"app_slug": "tcc-main-ci"}],
    )
    assert report["unapproved"] == ["claude[bot]", "stranger"]
    assert report["collaborators"] == ["nestorcolt", "stranger"]
    assert report["apps"] == ["agenticore-app[bot]", "claude[bot]", "tcc-main-ci[bot]"]
    assert report["approved"] == APPROVED


def test_approved_inventory_has_no_alert():
    report = ci_access_audit.audit(
        APPROVED, [{"login": "nestorcolt"}], [{"app_slug": "agenticore-app"}, {"app_slug": "tcc-main-ci"}]
    )
    assert report["unapproved"] == []


def test_inventory_paginates_every_collaborator_and_app():
    calls = []
    responses = {
        "repos/owner/repo/collaborators?page=1&per_page=100": [{"login": f"user{i}"} for i in range(100)],
        "repos/owner/repo/collaborators?page=2&per_page=100": [{"login": "last"}],
        "orgs/owner/installations?page=1&per_page=100": {
            "total_count": 101,
            "installations": [{"app_slug": f"app{i}"} for i in range(100)],
        },
        "orgs/owner/installations?page=2&per_page=100": {
            "total_count": 101,
            "installations": [{"app_slug": "last-app"}],
        },
    }

    def api(endpoint):
        assert endpoint not in calls
        calls.append(endpoint)
        return responses[endpoint]

    collaborators, apps = ci_access_audit.inventory("owner/repo", api)
    assert len(collaborators) == len(apps) == 101
    assert collaborators[-1]["login"] == "last"
    assert apps[-1]["app_slug"] == "last-app"
    assert calls == list(responses)


def test_incomplete_installation_inventory_is_red():
    def api(endpoint):
        return [] if "collaborators" in endpoint else {"total_count": 10, "installations": [{"app_slug": "one"}]}

    with pytest.raises(ValueError, match="incomplete"):
        ci_access_audit.inventory("owner/repo", api)


def test_failed_inventory_writes_alert_without_credentials(tmp_path, monkeypatch):
    policy = tmp_path / "actors.json"
    policy.write_text(json.dumps(APPROVED))
    report = tmp_path / "report.json"
    alert = tmp_path / "alert.txt"

    def unavailable(*args):
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(ci_access_audit, "inventory", unavailable)
    assert (
        ci_access_audit.main(
            ["--repo", "owner/repo", "--actors", str(policy), "--report", str(report), "--alert", str(alert)]
        )
        == 1
    )
    assert json.loads(report.read_text())["error"] == "Access inventory could not be completed"
    assert "inventory could not be completed" in alert.read_text()


def test_audit_cli_reports_complete_inventory_and_unapproved_access(tmp_path, monkeypatch, capsys):
    policy = tmp_path / "actors.json"
    policy.write_text(json.dumps(APPROVED))
    report = tmp_path / "report.json"
    alert = tmp_path / "alert.txt"
    monkeypatch.setattr(
        ci_access_audit,
        "inventory",
        lambda repo: (
            [{"login": "nestorcolt"}, {"login": "stranger"}],
            [{"app_slug": "agenticore-app"}, {"app_slug": "tcc-main-ci"}, {"app_slug": "claude"}],
        ),
    )
    assert (
        ci_access_audit.main(
            ["--repo", "owner/repo", "--actors", str(policy), "--report", str(report), "--alert", str(alert)]
        )
        == 1
    )
    expected = {
        "approved": APPROVED,
        "collaborators": ["nestorcolt", "stranger"],
        "apps": ["agenticore-app[bot]", "claude[bot]", "tcc-main-ci[bot]"],
        "unapproved": ["claude[bot]", "stranger"],
    }
    assert json.loads(report.read_text()) == expected
    assert json.loads(capsys.readouterr().out) == expected
    assert alert.read_text() == "Unapproved GitHub access: claude[bot], stranger\n"


def test_weekly_job_reads_dev_and_alerts_through_notifications():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/access-audit.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert "schedule" in events and "workflow_dispatch" in events
    job = workflow["jobs"]["audit"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "dev"
    notification = next(step for step in job["steps"] if "ntfy.homeofanton.com/anton-alerts" in step.get("run", ""))
    assert "always()" in notification["if"]
    assert notification["env"]["NTFY_USER"] == "${{ secrets.NTFY_USER }}"
    assert notification["env"]["NTFY_PASS"] == "${{ secrets.NTFY_PASS }}"
    assert "--fail" in notification["run"]


@pytest.mark.parametrize("unapproved, denied", [(False, False), (True, False), (False, True)])
def test_audit_cli_uses_real_transport_and_default_outputs(tmp_path, monkeypatch, capsys, unapproved, denied):
    import sys

    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "trusted-actors.json").write_text(json.dumps(APPROVED))
    collaborators = [{"login": "nestorcolt"}] + ([{"login": "stranger"}] if unapproved else [])
    responses = {
        "repos/owner/repo/collaborators?page=1&per_page=100": {"status": int(denied), "body": collaborators},
        "orgs/owner/installations?page=1&per_page=100": {
            "status": 0,
            "body": {"total_count": 2, "installations": [{"app_slug": "agenticore-app"}, {"app_slug": "tcc-main-ci"}]},
        },
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "gh"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json\nimport sys\n"
        "assert sys.argv[1] == 'api'\n"
        f"responses = json.loads({json.dumps(responses)!r})\n"
        "entry = responses[sys.argv[2]]\n"
        "print(json.dumps(entry['body']))\n"
        "sys.exit(entry['status'])\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.chdir(tmp_path)
    assert ci_access_audit.main(["--repo", "owner/repo"]) == int(unapproved or denied)
    report = json.loads((tmp_path / "access-report.json").read_text())
    assert json.loads(capsys.readouterr().out) == report
    alert = (tmp_path / "access-alert.txt").read_text()
    if denied:
        assert report == {"error": "Access inventory could not be completed"}
        assert "owner/repo" in alert and "could not be completed" in alert
    else:
        assert report == {
            "approved": APPROVED,
            "collaborators": [entry["login"] for entry in collaborators],
            "apps": ["agenticore-app[bot]", "tcc-main-ci[bot]"],
            "unapproved": ["stranger"] if unapproved else [],
        }
        assert alert == "Unapproved GitHub access: stranger\n" if unapproved else alert == ""


def test_audit_cli_requires_a_repository():
    with pytest.raises(SystemExit) as error:
        ci_access_audit.main([])
    assert error.value.code == 2


def test_corrupt_collaborator_inventory_fails_closed():
    with pytest.raises(ValueError):
        ci_access_audit.inventory("owner/repo", lambda endpoint: {})
