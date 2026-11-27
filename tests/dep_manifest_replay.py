import hashlib
import json
import os
import pstats
import resource
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from coverage.numbits import numbits_to_nums

from scripts import dep_manifest
from tests.test_dep_manifest_parallel import _producer_state
from tests.test_sonar_arrivals import Clock, _token, arrivals

ROOT = Path(__file__).resolve().parents[1]
BASE = "13775026851010893656186c523347c8e03555a8"
RUN = "45229761034"
REPOSITORY = "the-cloud-clockwork/agentihooks"


def _phase(name: str, event: str, details: dict[str, Any]) -> None:
    folder = Path(os.environ["PROOF_REPORT_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "manifest-phases.jsonl").open("a") as stream:
        stream.write(json.dumps({"phase": name, "event": event, "utc_seconds": time.time(), **details}) + "\n")


def _request(path: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={"Accept": "application/vnd.github+json"},
    )
    request.add_unredirected_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    return urllib.request.urlopen(request, timeout=60)


def _download(folder: Path) -> dict[str, dict[str, Any]]:
    artifacts = []
    page = 1
    while True:
        with _request(f"actions/runs/{RUN}/artifacts?per_page=100&page={page}") as response:
            payload = json.load(response)
        artifacts.extend(payload["artifacts"])
        if len(artifacts) >= payload["total_count"]:
            break
        page += 1
    selected = {}
    for shard in range(1, 21):
        name = f"coverage-3.12-{shard}"
        artifact = max((item for item in artifacts if item["name"] == name), key=lambda item: item["id"])
        assert not artifact["expired"]
        path = folder / f"{name}.zip"
        with _request(f"actions/artifacts/{artifact['id']}/zip") as response, path.open("wb") as output:
            shutil.copyfileobj(response, output)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assert artifact["digest"] == f"sha256:{digest}"
        selected[name] = {"id": artifact["id"], "digest": digest, "bytes": path.stat().st_size}
        _phase(name, "downloaded", selected[name])
    return selected


def _members(folder: Path) -> dict[str, str]:
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(folder).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _coverage(path: Path) -> dict[str, Any]:
    result = {}
    with sqlite3.connect(path) as connection:
        for table, fields in (("arc", "fromno, tono"), ("line_bits", "numbits")):
            query = (
                f"SELECT file.path, context.context, {fields} FROM {table} "
                f"JOIN file ON file.id = {table}.file_id JOIN context ON context.id = {table}.context_id "
                f"ORDER BY file.path, context.context, {fields}"
            )
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(query):
                values = [numbits_to_nums(value) if isinstance(value, bytes) else value for value in row]
                digest.update(json.dumps(values, separators=(",", ":")).encode() + b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
        result["contexts"] = connection.execute("SELECT count(*) FROM context").fetchone()[0]
        result["files"] = connection.execute("SELECT count(*) FROM file").fetchone()[0]
    return result


def _command(script: str, environment: dict[str, str]) -> dict[str, Any]:
    name = f"{Path(environment['RUNNER_TEMP']).name}: {script.splitlines()[0]}"
    _phase(name, "started", {})
    start = time.monotonic()
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    result = {
        "exit": completed.returncode,
        "seconds": time.monotonic() - start,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    _phase(name, "completed", result)
    return result


def _workflow(text: str) -> dict[str, str]:
    steps = yaml.safe_load(text)["jobs"]["dep-manifest"]["steps"]
    return {step["name"]: step["run"] for step in steps if "run" in step}


def _run_commands(workflow: dict[str, str], folder: Path, head: str) -> dict[str, Any]:
    folder.mkdir()
    environment = {**os.environ, "RUNNER_TEMP": str(folder), "GITHUB_SHA": head, "ENVIRONMENT": "py3.12-shared"}
    validation = _command(workflow["Check the shard coverage downloads"], environment)
    assert validation["exit"] == 0, validation
    build = workflow["Build the test dependency manifest"]
    combine, marker, cli = build.partition("python -m scripts.dep_manifest build")
    assert marker
    combined = _command(combine, environment)
    assert combined["exit"] == 0, combined
    profile = folder / "manifest.profile"
    command = f'python -m cProfile -o "{profile}" -m scripts.dep_manifest build' if folder.name == "after" else marker
    built = _command(command + cli, environment)
    assert built["exit"] == 0, built
    phases = {}
    if folder.name == "after":
        stats = pstats.Stats(str(profile)).get_stats_profile().func_profiles
        phases = {name: stats[name].cumtime for name in ("build", "pack", "dumps")}
    return {"validation": validation, "combine": combined, "cli": built, "profile_seconds": phases}


def _reject_commands(workflow: dict[str, str], folder: Path, head: str) -> list[int]:
    folder.mkdir(parents=True)
    environment = {**os.environ, "RUNNER_TEMP": str(folder), "GITHUB_SHA": head, "ENVIRONMENT": "py3.12-shared"}
    validation = _command(workflow["Check the shard coverage downloads"], environment)["exit"]
    if validation:
        return [validation]
    return [0, _command(workflow["Build the test dependency manifest"], environment)["exit"]]


def _malformed(before: dict[str, str], after: dict[str, str], corpus: Path, head: str) -> dict[str, Any]:
    link = ROOT / ".coverage-shards"
    reports = {}
    cases = [
        ("malformed-jsonl", next(corpus.rglob("*.jsonl")), b"{\n"),
        ("torn-sqlite", next(corpus.glob("coverage-3.12-*/.coverage")), b"broken"),
    ]
    for name, path, broken in cases:
        original = path.read_bytes()
        link.symlink_to(corpus, target_is_directory=True)
        try:
            path.write_bytes(broken)
            reports[name] = {
                label: _reject_commands(workflow, corpus.parent / "invalid" / name / label, head)
                for label, workflow in (("before", before), ("after", after))
            }
            assert reports[name]["before"] == reports[name]["after"] == [1]
        finally:
            path.write_bytes(original)
            link.unlink()
    empty = corpus.parent / "empty"
    empty.mkdir()
    link.symlink_to(empty, target_is_directory=True)
    try:
        reports["missing-inputs"] = {
            label: _reject_commands(workflow, corpus.parent / "invalid" / "missing" / label, head)
            for label, workflow in (("before", before), ("after", after))
        }
        assert reports["missing-inputs"]["before"] == reports["missing-inputs"]["after"] == [0, 1]
    finally:
        link.unlink()
    return reports


@pytest.mark.feature("dep-manifest-raw-arrivals")
@pytest.mark.timeout(2100)
def test_real_promotion_archives_produce_identical_workflow_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert os.environ.get("GH_TOKEN"), "The focused proof requires its Actions read token"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    before = _workflow(
        subprocess.check_output(["git", "show", f"{BASE}:.github/workflows/test.yml"], cwd=ROOT, text=True)
    )
    after = _workflow((ROOT / ".github/workflows/test.yml").read_text())
    for name in ("Check the shard coverage downloads", "Build the test dependency manifest"):
        assert before[name] == after[name]
    archives = tmp_path / "archives"
    archives.mkdir()
    _phase("corpus", "started", {"head": head, "source_run": RUN})
    fingerprints = _download(archives)
    bulk = tmp_path / "bulk"
    for name in fingerprints:
        with zipfile.ZipFile(archives / f"{name}.zip") as archive:
            archive.extractall(bulk / name)
    clock = Clock()
    raw = tmp_path / "raw"
    _producer_state(raw, False)
    service = arrivals.Results("https://example.invalid/", _token("run-1", "job-2"))
    service.owners = dict.fromkeys(fingerprints, {})
    names = list(fingerprints)
    listings = []

    def listed() -> dict[str, str]:
        count = min(len(names), len(clock.pauses) + 1)
        if count == len(names):
            _producer_state(raw, True)
        listings.append(count)
        return {name: str(fingerprints[name]["id"]) for name in names[:count]}

    monkeypatch.setattr(service, "listed", listed)
    monkeypatch.setattr(
        service, "call", lambda method, body: {"signed_url": (archives / f"{body['name']}.zip").as_uri()}
    )
    monkeypatch.setenv("SHARDS", "20")
    monkeypatch.setattr(arrivals, "WAIT", 600)
    monkeypatch.setattr(arrivals, "JOB_POLL", arrivals.INTERVAL)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://example.invalid/")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("COVERAGE_RAW", "true")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setattr(arrivals, "Results", lambda *args: service)
    monkeypatch.setattr(arrivals, "Wall", lambda: clock)
    monkeypatch.setattr(arrivals, "OUT", raw / "shards")
    monkeypatch.setattr(arrivals, "QUEUE_STATE", raw / "queue")
    assert arrivals.main() == 0
    assert listings[0] == 1 and listings[-1] == 20
    assert _members(bulk) == _members(raw / "shards")
    link = ROOT / ".coverage-shards"
    assert not link.exists()
    outputs = {}
    try:
        for label, inputs, workflow in (("before", bulk, before), ("after", raw / "shards", after)):
            link.symlink_to(inputs, target_is_directory=True)
            folder = tmp_path / label
            measured = _run_commands(workflow, folder, head)
            manifest = folder / "dep-manifest"
            measured["coverage"] = _coverage(manifest / ".coverage")
            measured["manifest"] = hashlib.sha256((manifest / "manifest.json").read_bytes()).hexdigest()
            measured["packed"] = hashlib.sha256((manifest / "packed.json").read_bytes()).hexdigest()
            assert dep_manifest.unpack(json.loads((manifest / "packed.json").read_text())) == json.loads(
                (manifest / "manifest.json").read_text()
            )
            outputs[label] = measured
            link.unlink()
        for field in ("coverage", "manifest", "packed"):
            assert outputs["before"][field] == outputs["after"][field]
        assert outputs["before"]["coverage"]["contexts"] > 1000
    finally:
        link.unlink(missing_ok=True)
    report = {
        "head": head,
        "source_run": RUN,
        "commands": {
            name: before[name]
            for name in (
                "Check the shard coverage downloads",
                "Build the test dependency manifest",
            )
        },
        "grader": BASE,
        "artifacts": fingerprints,
        "member_fingerprints": _members(bulk),
        "invalid_inputs": _malformed(before, after, bulk, head),
        "outputs": outputs,
        "peak_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "free_disk_bytes": shutil.disk_usage(tmp_path).free,
    }
    destination = Path(os.environ["PROOF_REPORT_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest-equivalence.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({"head": head, "source_run": RUN, "outputs": outputs}, sort_keys=True))
