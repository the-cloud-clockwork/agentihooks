import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def access_responses():
    responses = {
        "orgs/the-cloud-clockwork": {"default_repository_permission": "read"},
        "orgs/the-cloud-clockwork/members?per_page=100": [{"login": "nestorcolt"}],
        "orgs/the-cloud-clockwork/installations?per_page=100": {
            "installations": [
                {"app_id": 2988497, "app_slug": "agenticore-app"},
                {"app_id": 5243008, "app_slug": "tcc-main-ci"},
            ]
        },
        "orgs/the-cloud-clockwork/actions/runner-groups/1": {
            "id": 1,
            "name": "Default",
            "allows_public_repositories": False,
        },
        "orgs/the-cloud-clockwork/actions/runner-groups/3": {
            "id": 3,
            "name": "anton-oss",
            "visibility": "selected",
            "allows_public_repositories": True,
            "restricted_to_workflows": False,
            "selected_workflows": [],
        },
        "orgs/the-cloud-clockwork/actions/runner-groups/3/repositories?per_page=100": {
            "repositories": [{"name": "agentihooks"}, {"name": "agentibrain-kernel"}]
        },
        "orgs/the-cloud-clockwork/actions/permissions/fork-pr-contributor-approval": {
            "approval_policy": "all_external_contributors"
        },
        "orgs/the-cloud-clockwork/repos?type=public&per_page=100": [
            {"name": "agentihooks"},
            {"name": "agentibrain-kernel"},
            {"name": "later-public"},
        ],
        "repos/the-cloud-clockwork/later-public/actions/permissions/fork-pr-contributor-approval": {
            "approval_policy": "all_external_contributors"
        },
    }
    for repository in ("agentihooks", "agentibrain-kernel"):
        prefix = f"repos/the-cloud-clockwork/{repository}"
        responses[f"{prefix}/actions/permissions/fork-pr-contributor-approval"] = {
            "approval_policy": "all_external_contributors"
        }
        responses[f"{prefix}/actions/permissions/workflow"] = {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        responses[f"{prefix}/collaborators?per_page=100"] = [
            {"login": "nestorcolt", "permissions": {"push": True, "admin": True}}
        ]
        responses[f"{prefix}/teams?per_page=100"] = []
        responses[f"{prefix}/keys?per_page=100"] = []
    return responses


def run_access(tmp_path, responses, mode="check", gh_script=None, input_data=None, **environment):
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    gh = tmp_path / "gh"
    gh.write_text(
        gh_script
        or (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "assert sys.argv[1] == 'api'\n"
            "assert '--method' not in sys.argv and '-X' not in sys.argv\n"
            "data = json.loads(Path(os.environ['GH_ACCESS_FIXTURE']).read_text())\n"
            "value = data[sys.argv[2]]\n"
            "if isinstance(value, dict) and 'pages' in value:\n"
            "    assert '--paginate' in sys.argv\n"
            "    for page in value['pages']: print(json.dumps(page))\n"
            "else: print(json.dumps(value))\n"
        )
    )
    gh.chmod(0o755)
    return subprocess.run(
        ["bash", str(ROOT / "scripts/github_access.sh"), mode],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GH_ACCESS_FIXTURE": str(fixture),
            **environment,
        },
        input=input_data,
        text=True,
        capture_output=True,
    )


def test_read_back_accepts_the_operator_access_boundary(tmp_path, access_responses):
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GitHub access controls verified" in result.stdout


@pytest.mark.parametrize("repository", ["agentihooks", "agentibrain-kernel"])
@pytest.mark.parametrize(
    ("setting", "changed"),
    [
        ("fork-pr-contributor-approval", {"approval_policy": "first_time_contributors"}),
        ("workflow", {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}),
        ("workflow", {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": False}),
        ("workflow", {"default_workflow_permissions": "read"}),
    ],
)
def test_read_back_rejects_repository_actions_drift(tmp_path, access_responses, repository, setting, changed):
    access_responses[f"repos/the-cloud-clockwork/{repository}/actions/permissions/{setting}"] = changed
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"DRIFT {repository}" in result.stderr


def test_read_back_rejects_public_access_to_default_runners(tmp_path, access_responses):
    access_responses["orgs/the-cloud-clockwork/actions/runner-groups/1"]["allows_public_repositories"] = True
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1
    assert "DRIFT Default runner public access" in result.stderr


def test_read_back_rejects_an_additional_writer_on_a_later_page(tmp_path, access_responses):
    endpoint = "repos/the-cloud-clockwork/agentihooks/collaborators?per_page=100"
    access_responses[endpoint] = {
        "pages": [
            access_responses[endpoint],
            [{"login": "outsider", "permissions": {"push": True, "admin": False}}],
        ]
    }
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1
    assert "DRIFT agentihooks writers" in result.stderr
    assert '"outsider"' in result.stdout


def test_read_back_fails_when_the_api_cannot_read_a_setting(tmp_path, access_responses):
    del access_responses["repos/the-cloud-clockwork/agentibrain-kernel/actions/permissions/workflow"]
    result = run_access(tmp_path, access_responses)
    assert result.returncode != 0
    assert "GitHub access controls verified" not in result.stdout


@pytest.mark.parametrize("variable", ["AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK", "GITHUB_ACTIONS"])
def test_apply_is_refused_in_an_agent_or_ci_session(tmp_path, access_responses, variable):
    result = run_access(tmp_path, access_responses, mode="apply", **{variable: "active"})
    assert result.returncode == 1
    assert "Only the operator may apply" in result.stderr


@pytest.mark.parametrize(
    ("app_id", "app_slug"),
    [
        (1236702, "claude"),
        (81054, "aws-connector-for-github"),
        (1144995, "chatgpt-codex-connector"),
        (999, "unknown-app"),
    ],
)
def test_read_back_rejects_an_unapproved_installed_app(tmp_path, access_responses, app_id, app_slug):
    access_responses["orgs/the-cloud-clockwork/installations?per_page=100"]["installations"].append(
        {"app_id": app_id, "app_slug": app_slug}
    )
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1
    assert "DRIFT installed GitHub apps" in result.stderr


def test_apply_opens_every_unapproved_app_when_the_browser_consumes_stdin(tmp_path, access_responses):
    apps = [
        (1236702, "claude", "https://github.com/settings/installations/109007191"),
        (81054, "aws-connector-for-github", "https://github.com/settings/installations/123736980"),
        (1144995, "chatgpt-codex-connector", "https://github.com/settings/installations/141979034"),
    ]
    installations = access_responses["orgs/the-cloud-clockwork/installations?per_page=100"]["installations"]
    installations.extend({"app_id": app_id, "app_slug": slug, "html_url": url} for app_id, slug, url in apps)
    gh_script = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
assert sys.argv[1] == "api"
endpoint = next(arg for arg in sys.argv[2:] if arg.startswith(("repos/", "orgs/")) or arg == "user")
fixture = Path(os.environ["GH_ACCESS_FIXTURE"])
data = json.loads(fixture.read_text())
if endpoint == "user":
    print("nestorcolt")
elif "--method" in sys.argv:
    request = json.load(sys.stdin)
    expected = {"allows_public_repositories": False} if "/runner-groups/" in endpoint else data[endpoint]
    assert request == expected
    print("{}")
else:
    value = data[endpoint]
    print(json.dumps(value))
    if "/installations?" in endpoint:
        value["installations"] = [app for app in value["installations"] if app["app_id"] in (2988497, 5243008)]
        fixture.write_text(json.dumps(data))
"""
    browser = tmp_path / "xdg-open"
    browser.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncat > /dev/null\nprintf "%s\\n" "$1" >> "$GH_ACCESS_BROWSER_LOG"\n'
    )
    browser.chmod(0o755)
    browser_log = tmp_path / "browser-pages.txt"
    result = run_access(
        tmp_path,
        access_responses,
        mode="apply",
        gh_script=gh_script,
        input_data="\n",
        AGENTIHOOKS_AGENT_NAME="",
        AGENTIHOOKS_SWARM_TASK="",
        GITHUB_ACTIONS="",
        GH_ACCESS_BROWSER_LOG=str(browser_log),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert browser_log.read_text().splitlines() == [url for _, _, url in apps]


@pytest.mark.parametrize("target", ["organization", "later-public"])
def test_read_back_rejects_weak_approval_outside_the_declared_repositories(tmp_path, access_responses, target):
    access_responses["orgs/the-cloud-clockwork/repos?type=public&per_page=100"] = {
        "pages": [[{"name": "agentihooks"}, {"name": "agentibrain-kernel"}], [{"name": "later-public"}]]
    }
    org_endpoint = "orgs/the-cloud-clockwork/actions/permissions/fork-pr-contributor-approval"
    repo_endpoint = "repos/the-cloud-clockwork/later-public/actions/permissions/fork-pr-contributor-approval"
    access_responses[org_endpoint] = {"approval_policy": "all_external_contributors"}
    access_responses[repo_endpoint] = {"approval_policy": "all_external_contributors"}
    access_responses[org_endpoint if target == "organization" else repo_endpoint] = {
        "approval_policy": "first_time_contributors"
    }
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"DRIFT {target} contributor approval" in result.stderr


@pytest.mark.parametrize("inventory", [[], None])
def test_read_back_refuses_empty_or_failed_public_repository_discovery(tmp_path, access_responses, inventory):
    access_responses["orgs/the-cloud-clockwork/actions/permissions/fork-pr-contributor-approval"] = {
        "approval_policy": "all_external_contributors"
    }
    endpoint = "orgs/the-cloud-clockwork/repos?type=public&per_page=100"
    if inventory is None:
        del access_responses[endpoint]
    else:
        access_responses[endpoint] = inventory
    result = run_access(tmp_path, access_responses)
    assert result.returncode != 0
    assert "GitHub access controls verified" not in result.stdout


@pytest.mark.parametrize("drift", ["visibility", "repository"])
def test_read_back_rejects_open_source_runner_group_access_drift(tmp_path, access_responses, drift):
    endpoint = "orgs/the-cloud-clockwork/actions/runner-groups/3"
    access_responses[endpoint] = {
        "id": 3,
        "name": "anton-oss",
        "visibility": "selected",
        "allows_public_repositories": True,
        "restricted_to_workflows": False,
        "selected_workflows": [],
    }
    access_responses[f"{endpoint}/repositories?per_page=100"] = {
        "repositories": [{"name": "agentihooks"}, {"name": "agentibrain-kernel"}]
    }
    if drift == "visibility":
        access_responses[endpoint]["visibility"] = "all"
    else:
        access_responses[f"{endpoint}/repositories?per_page=100"]["repositories"].append({"name": "outsider"})
    result = run_access(tmp_path, access_responses)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT Anton open source runner" in result.stderr
