import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SELECTORS = [
    ("coverage-ratchet", "Find the run that published the base baseline"),
    ("select", "Find the dev push run of the dispatched base"),
]


@pytest.fixture
def lookup(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['REQUESTS'], 'a') as stream: stream.write(json.dumps(args) + '\\n')\n"
        "data = json.loads(os.environ['SOURCE'])\n"
        "endpoint = args[1]\n"
        "path = urlsplit(endpoint).path\n"
        "query = parse_qs(urlsplit(endpoint).query)\n"
        "if '/compare/' in path:\n"
        "    body = {'status': data['queue_ancestry'] if path.endswith('...head') else data['ancestry']}\n"
        "elif '/commits/' in path: body = {'sha': 'base', 'commit': {'tree': {'sha': 'tree'}}, 'parents': [{'sha': 'base'}]}\n"
        "elif path.endswith('/jobs'): body = {'jobs': data['jobs']}\n"
        "elif path.endswith('/artifacts'): body = {'artifacts': [{'name': name, 'expired': False} for name in data['artifacts']]}\n"
        "elif path.endswith('/runs'):\n"
        "    run = data['run']\n"
        "    keep = all(run.get(key) == value[0] for key, value in query.items() if key in ('head_sha', 'status', 'event'))\n"
        "    if 'status' in query and query['status'][0] == 'success': keep = run['conclusion'] == 'success'\n"
        "    if 'branch' in query: keep = keep and run['head_branch'] == query['branch'][0]\n"
        "    body = {'workflow_runs': [run] if keep else []}\n"
        "else: raise SystemExit('unexpected API endpoint: ' + endpoint)\n"
        "if '--jq' in args:\n"
        "    result = subprocess.run(['jq', '-r', args[args.index('--jq') + 1]], input=json.dumps(body), text=True, capture_output=True)\n"
        "    sys.stdout.write(result.stdout)\n"
        "    sys.stderr.write(result.stderr)\n"
        "    raise SystemExit(result.returncode)\n"
        "print(json.dumps(body))\n"
    )
    gh.chmod(0o755)

    def run(selector, event="push", fault=None, queue_ancestry="diverged"):
        publisher, measure, publish = (
            ("coverage-baseline", "Compute the coverage baseline", "Publish the passed dev coverage baseline")
            if event == "push"
            else ("queue-baseline", "Record this run's coverage baseline", "Publish this run's coverage baseline")
        )
        jobs = [
            {"name": f"unit ({version}, {shard})", "conclusion": "success"}
            for version, count in ((("3.12", 20),) if fault == "twenty-shards" else (("3.11", 6), ("3.12", 12)))
            for shard in range(1, count + 1)
            if not (fault == "shard-gap" and (version, shard) == ("3.12", 7))
        ]
        jobs.extend(
            [
                {
                    "name": publisher,
                    "conclusion": "success",
                    "steps": [{"name": name, "conclusion": "success"} for name in (measure, publish)],
                },
                {"name": "coverage-ratchet", "conclusion": "failure"},
            ]
        )
        source = {
            "run": {
                "id": 222,
                "event": event,
                "head_sha": "base",
                "head_branch": "dev" if event == "push" else "gh-readonly-queue/dev/pr-1-base",
                "status": "completed",
                "conclusion": "failure",
            },
            "ancestry": "ahead",
            "queue_ancestry": queue_ancestry,
            "jobs": jobs,
            "artifacts": ["coverage-baseline", "durations-merged"],
        }
        changes = {
            "unit": (jobs[0], "conclusion", "failure"),
            "measure": (jobs[-2]["steps"][0], "conclusion", "failure"),
            "publish": (jobs[-2]["steps"][1], "conclusion", "failure"),
            "publisher-job": (jobs[-2], "conclusion", "failure"),
            "outside-dev": (source, "ancestry", "diverged"),
            "event": (source["run"], "event", "pull_request"),
            "branch": (source["run"], "head_branch", "feature"),
            "head": (source["run"], "head_sha", "other"),
            "artifact": (source, "artifacts", []),
        }
        if fault in changes:
            target, key, value = changes[fault]
            target[key] = value
        elif fault == "missing-unit":
            jobs.pop(0)
        elif fault == "missing-publisher":
            jobs.pop(-2)
        elif fault in {"reuse", "reuse-failed", "reuse-decision-failed"}:
            jobs[:] = [
                jobs[-2],
                jobs[-1],
                {"name": "unit", "conclusion": "skipped"},
                {
                    "name": "reuse",
                    "conclusion": "failure" if fault == "reuse-failed" else "success",
                    "steps": [
                        {
                            "name": "Decide from protected code and record the tested tree",
                            "conclusion": "failure" if fault == "reuse-decision-failed" else "success",
                        }
                    ],
                },
            ]
        output = tmp_path / "output"
        output.write_text("")
        requests = tmp_path / "requests"
        requests.write_text("")
        env = dict(
            os.environ,
            PATH=f"{tools}:{os.environ['PATH']}",
            SOURCE=json.dumps(source),
            REQUESTS=str(requests),
            GITHUB_OUTPUT=str(output),
            GITHUB_REPOSITORY="o/r",
            GITHUB_REF_NAME="feature",
            GITHUB_SHA="head",
            BASE="base",
            BASE_SHA="base",
            WAIT_SECONDS="0",
        )
        job, name = selector
        definitions = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
        steps = definitions["select" if job == "durations" and job not in definitions else job]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == name)
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        return result, output.read_text(), requests.read_text()

    return run


@pytest.mark.parametrize(
    ("selector", "event"),
    [
        (selector, event)
        for selector in _SELECTORS
        for event in ("push", "merge_group")
        if selector[0] != "select" or event == "push"
    ],
)
def test_lookup_accepts_measured_publication_when_its_ratchet_failed(lookup, selector, event):
    result, output, requests = lookup(selector, event=event)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "id=222\n" in output
    assert "/runs/222/jobs" in requests


@pytest.mark.parametrize("selector", _SELECTORS)
def test_lookup_reads_the_shard_count_from_the_base_run(lookup, selector):
    result, output, _ = lookup(selector, fault="twenty-shards")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "id=222\n" in output


@pytest.mark.parametrize("selector", _SELECTORS[:1])
@pytest.mark.parametrize("fault", ["reuse", "publisher-job"])
def test_lookup_accepts_queue_publication_before_unrelated_jobs_pass(lookup, selector, fault):
    result, output, _ = lookup(selector, event="merge_group", fault=fault)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "id=222\n" in output


@pytest.mark.parametrize("selector", _SELECTORS[:1])
@pytest.mark.parametrize("fault", ["reuse-failed", "reuse-decision-failed"])
def test_lookup_rejects_unverified_queue_reuse(lookup, selector, fault):
    result, output, _ = lookup(selector, event="merge_group", fault=fault)
    assert result.returncode != 0, result.stdout + result.stderr
    assert output == ""


@pytest.mark.parametrize("selector", _SELECTORS)
@pytest.mark.parametrize(
    "fault",
    [
        "unit",
        "missing-unit",
        "shard-gap",
        "measure",
        "publish",
        "missing-publisher",
        "publisher-job",
        "outside-dev",
        "event",
        "branch",
        "head",
        "artifact",
    ],
)
def test_lookup_rejects_sources_without_valid_dev_measurements(lookup, selector, fault):
    result, output, _ = lookup(selector, fault=fault)
    assert result.returncode != 0, result.stdout + result.stderr
    assert output == ""
