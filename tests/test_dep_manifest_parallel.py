import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from coverage import CoverageData

from scripts import dep_manifest
from tests.test_sonar_arrivals import Clock, Service, _token, arrivals

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _corpus(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    root = tmp_path / "source"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks/sample.py").write_text(
        "def double(value: int) -> int:\n    if value:\n        return value * 2\n    return 0\n"
    )
    archives = {}
    for shard, branch in ((1, 3), (2, 4)):
        folder = tmp_path / f"input-{shard}"
        folder.mkdir()
        data = CoverageData(basename=str(folder / ".coverage"))
        data.set_context(f"tests/test_sample.py::test_{shard}|run")
        data.add_arcs({str(root / "hooks/sample.py"): [(-1, 2), (2, branch), (branch, -1)]})
        data.write()
        reused = CoverageData(basename=str(folder / "reused"))
        reused.set_context("tests/test_sample.py::test_reused|run")
        reused.add_arcs({str(root / "hooks/sample.py"): [(-1, 2), (2, 3), (3, -1)]})
        reused.write()
        inputs = (
            json.dumps(
                {"test": f"tests/test_sample.py::test_{shard}", "files": ["docs/data.json"], "escapes": False}
            ).encode()
            + b"\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(".coverage", (folder / ".coverage").read_bytes())
            archive.writestr("reused-coverage/.coverage", (folder / "reused").read_bytes())
            archive.writestr("dep-inputs/worker.jsonl", inputs)
        archives[f"coverage-3.12-{shard}"] = buffer.getvalue()
    return root, archives


def _transport(archives: dict[str, bytes], monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Any:
    service = arrivals.Results("https://example.invalid/", _token("run-1", "job-2"))
    names = sorted(archives)
    service.owners = dict.fromkeys(names, {})

    def listed() -> dict[str, str]:
        offered = names[:1] if clock.now() == 0 else names
        return {name: str(index + 1) for index, name in enumerate(offered)}

    def call(method: str, body: dict[str, object]) -> dict[str, str]:
        assert method == "GetSignedArtifactURL"
        return {"signed_url": f"https://example.invalid/{body['name']}"}

    def opened(url: str, timeout: float) -> io.BytesIO:
        assert timeout > 0
        return io.BytesIO(archives[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(service, "listed", listed)
    monkeypatch.setattr(service, "call", call)
    monkeypatch.setattr(arrivals.urllib.request, "urlopen", opened)
    return service


def _outputs(folder: Path, root: Path) -> dict[str, Any]:
    combined = CoverageData(basename=str(folder / "combined"))
    databases = sorted(folder.glob("coverage-3.12-*/.coverage"))
    databases += sorted(folder.glob("coverage-3.12-*/reused-coverage/.coverage"))
    for path in databases:
        data = CoverageData(basename=str(path))
        data.read()
        combined.update(data)
    combined.write()
    contexts = {}
    for context in sorted(combined.measured_contexts()):
        combined.set_query_context(context)
        contexts[context] = {
            path: {"lines": sorted(combined.lines(path) or []), "arcs": sorted(combined.arcs(path) or [])}
            for path in sorted(combined.measured_files())
        }
    inputs = [json.loads(line) for path in sorted(folder.rglob("*.jsonl")) for line in path.read_text().splitlines()]
    manifest = dep_manifest.build(folder / "combined", sorted(folder.rglob("*.jsonl")), root, "same-head", "py3.12")
    packed = dep_manifest.pack(manifest)
    assert dep_manifest.unpack(json.loads(json.dumps(packed))) == manifest
    return {"contexts": contexts, "inputs": inputs, "manifest": manifest, "packed": packed}


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_raw_archives_keep_contexts_reused_coverage_and_nested_dependency_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archives = _corpus(tmp_path)
    service = _transport(archives, monkeypatch, Clock())
    out = tmp_path / "downloaded"
    out.mkdir()
    name = "coverage-3.12-1"
    assert service.fetch(name, out / name)
    with zipfile.ZipFile(io.BytesIO(archives[name])) as archive:
        for member in archive.namelist():
            assert (out / name / member).read_bytes() == archive.read(member)
    result = _outputs(out, root)
    contexts = result["contexts"]
    assert set(contexts) == {"tests/test_sample.py::test_1|run", "tests/test_sample.py::test_reused|run"}
    assert contexts["tests/test_sample.py::test_1|run"][str(root / "hooks/sample.py")]["lines"] == [2, 3]
    assert contexts["tests/test_sample.py::test_reused|run"][str(root / "hooks/sample.py")]["arcs"] == [
        (-1, 2),
        (2, 3),
        (3, -1),
    ]
    assert result["inputs"] == [{"test": "tests/test_sample.py::test_1", "files": ["docs/data.json"], "escapes": False}]
    assert sorted(path.name for path in out.iterdir()) == ["combined", name]


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_raw_arrival_collection_matches_bulk_inputs_without_flattening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archives = _corpus(tmp_path)
    baseline = tmp_path / "bulk"
    for name, body in archives.items():
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            archive.extractall(baseline / name)
    out = tmp_path / "arriving"
    clock = Clock()
    service = _transport(archives, monkeypatch, clock)
    pauses = []

    def pause(seconds: float) -> None:
        assert (out / "coverage-3.12-1/.coverage").is_file()
        assert not (out / "coverage-3.12-2").exists()
        pauses.append(seconds)
        clock.moment += seconds

    def flatten(*args: object) -> None:
        pytest.fail("Raw manifest collection started coverage flattening")

    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://example.invalid/")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("COVERAGE_RAW", "true")
    monkeypatch.setattr(arrivals, "Results", lambda *args: service)
    monkeypatch.setattr(arrivals, "Wall", lambda: clock)
    monkeypatch.setattr(arrivals, "OUT", out)
    monkeypatch.setattr(arrivals, "advance", flatten)
    monkeypatch.setattr(clock, "pause", pause)
    assert arrivals.main() == 0
    assert pauses == [arrivals.INTERVAL]
    assert _outputs(out, root) == _outputs(baseline, root)
    assert not list(out.rglob("flat-*"))


@pytest.mark.feature("dep-manifest-raw-arrivals")
@pytest.mark.parametrize(
    "listed",
    [
        {"coverage-3.12-1": "1", "flakes-3.12-2": "2"},
        {"coverage-3.12-1": "1"},
    ],
    ids=["ended-without-coverage", "still-missing"],
)
def test_raw_collection_refuses_missing_shards(
    listed: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service([listed])
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://example.invalid/")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("COVERAGE_RAW", "true")
    monkeypatch.setattr(arrivals, "Results", lambda *args: service)
    monkeypatch.setattr(arrivals, "OUT", tmp_path / "incomplete")
    monkeypatch.setattr(arrivals, "WAIT", 0)
    assert arrivals.main() == 1


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_manifest_preparation_starts_beside_units_and_collects_before_validation() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    manifest = jobs["dep-manifest"]
    assert set(manifest["needs"]) == {"select", "reuse", "admission"}
    assert "dep-manifest" not in jobs["unit"]["needs"]
    steps = manifest["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Install coverage") < names.index("Take raw shard coverage as it lands")
    assert names.index("Take raw shard coverage as it lands") < names.index("Check the shard coverage downloads")
    assert names.index("Check the shard coverage downloads") < names.index("Require every raw shard before the build")
    assert names.index("Require every raw shard before the build") < names.index("Build the test dependency manifest")
    complete = next(step for step in steps if step.get("name") == "Require every raw shard before the build")
    assert str(complete["env"]["SHARDS"]) == "20"
    assert '[[ -s ".coverage-shards/coverage-3.12-$shard/.coverage" ]]' in complete["run"]
    collect = next(step for step in steps if step.get("name") == "Take raw shard coverage as it lands")
    assert collect["uses"] == "actions/github-script@v7"
    assert collect["env"]["COVERAGE_RAW"] == "true"
    assert collect["env"]["SCOPE"] == "full"
    assert str(collect["env"]["SHARDS"]) == "20"
    assert collect["with"]["github-token"] == "none"
    assert manifest["timeout-minutes"] > collect["timeout-minutes"]


def _producer_state(folder: Path, complete: bool, queued: bool = False) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "queue").write_text("queued" if queued else "started")
    (folder / "queue.complete").write_text(json.dumps({"run": "9", "attempt": 2, "complete": complete}))


def _raw_cli(service: Any, clock: Clock, folder: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://example.invalid/")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("COVERAGE_RAW", "true")
    monkeypatch.setattr(arrivals, "Results", lambda *args: service)
    monkeypatch.setattr(arrivals, "Wall", lambda: clock)
    monkeypatch.setattr(arrivals, "OUT", folder / "shards")
    monkeypatch.setattr(arrivals, "QUEUE_STATE", folder / "queue")
    return arrivals.main()


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_raw_rerun_reselects_replaced_archives_after_producers_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archives = _corpus(tmp_path)
    final = dict(archives)
    final["coverage-3.12-1"] = archives["coverage-3.12-2"]
    baseline = tmp_path / "bulk"
    for name, body in final.items():
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            archive.extractall(baseline / name)
    clock = Clock()
    service = _transport(archives, monkeypatch, clock)
    folder = tmp_path / "raw"
    _producer_state(folder, False)
    monkeypatch.setattr(
        service, "listed", lambda: {"coverage-3.12-1": "3" if clock.now() else "1", "coverage-3.12-2": "2"}
    )
    replacement_calls = []

    def opened(url: str, timeout: float) -> io.BytesIO:
        name = url.rsplit("/", 1)[1]
        if clock.now() and name == "coverage-3.12-1":
            replacement_calls.append(name)
            if len(replacement_calls) == 1:
                raise OSError("replacement temporarily unavailable")
        return io.BytesIO((final if clock.now() else archives)[name])

    def pause(seconds: float) -> None:
        clock.pauses.append(seconds)
        clock.moment += seconds
        _producer_state(folder, True)

    monkeypatch.setattr(arrivals.urllib.request, "urlopen", opened)
    monkeypatch.setattr(clock, "pause", pause)
    assert _raw_cli(service, clock, folder, monkeypatch) == 0
    assert clock.pauses == [arrivals.INTERVAL] * 2
    assert len(replacement_calls) == 2
    assert _outputs(folder / "shards", root) == _outputs(baseline, root)


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_raw_cli_does_not_spend_its_budget_while_shards_are_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archives = _corpus(tmp_path)
    clock = Clock()
    service = _transport(archives, monkeypatch, clock)
    folder = tmp_path / "raw"
    _producer_state(folder, False, queued=True)
    monkeypatch.setattr(arrivals, "WAIT", arrivals.INTERVAL)
    monkeypatch.setattr(arrivals, "JOB_POLL", arrivals.INTERVAL)
    monkeypatch.setattr(
        service,
        "listed",
        lambda: {} if clock.now() < arrivals.INTERVAL * 4 else {"coverage-3.12-1": "1", "coverage-3.12-2": "2"},
    )

    def pause(seconds: float) -> None:
        clock.pauses.append(seconds)
        clock.moment += seconds
        ready = clock.now() >= arrivals.INTERVAL * 4
        _producer_state(folder, ready, queued=not ready)

    monkeypatch.setattr(clock, "pause", pause)
    assert _raw_cli(service, clock, folder, monkeypatch) == 0
    assert clock.pauses == [arrivals.INTERVAL] * 4
    assert (folder / "shards/coverage-3.12-1/.coverage").is_file()
    assert _outputs(folder / "shards", root)["contexts"]


@pytest.mark.feature("dep-manifest-raw-arrivals")
def test_manifest_tracks_queued_and_completed_producers_before_fallback() -> None:
    manifest = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["dep-manifest"]
    steps = manifest["steps"]
    named = {step.get("name"): step for step in steps}
    mint = named["Mint the token that reads this run's job states"]
    assert mint["with"]["permission-actions"] == "read"
    assert manifest["permissions"] == {"contents": "read"}
    watch = named["Watch whether this run's shard jobs have started"]
    assert watch["env"]["COVERAGE_RAW"] == "true"
    assert watch["env"]["PREFIX"] == "unit ("
    assert watch["env"]["GH_APP_TOKEN"] == "${{ steps.jobs-token.outputs.token }}"
    assert "queue_watch.py" in watch["run"]
    stop = named["Stop the queue watch"]
    assert "always()" in stop["if"]
    names = [step.get("name") for step in steps]
    boundary = names.index("Require completed unit producers")
    assert names.index("Take raw shard coverage as it lands") < boundary
    assert boundary < names.index("Download shard coverage")
    assert boundary < names.index("Check the shard coverage downloads")
