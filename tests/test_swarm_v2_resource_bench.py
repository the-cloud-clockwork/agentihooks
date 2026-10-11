import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import resource_bench as bench

pytestmark = pytest.mark.unit

WORKLOADS = Path(__file__).parent / "performance" / "swarm_v2" / "workloads"
PROBE = (
    "import os, sys, pathlib; "
    "pathlib.Path(sys.argv[1]).open('a').write(' '.join([os.getcwd(), sys.argv[2], os.environ['HOME'], "
    "os.environ['TMPDIR'], os.environ['CHILDREN'], str(any('FAKE' in k for k in os.environ))]) + chr(10))"
)


@pytest.mark.parametrize(
    ("text", "mib"),
    [
        ("16078424Ki", 15701),
        ("1Gi", 1024),
        ("128Mi", 128),
        ("2Ti", 2097152),
        ("1G", 953),
        ("3M", 2),
        ("5000k", 4),
        ("5000K", 4),
        ("1T", 953674),
        ("97743690881", 93215),
        ("2e9", 1907),
        ("0", 0),
    ],
)
def test_memory_quantities_become_whole_mib(text, mib):
    assert bench.mib(text) == mib


@pytest.mark.parametrize(
    ("text", "millis"), [("4", 4000), ("500m", 500), ("0.25", 250), ("1500000u", 1500), ("100n", 1)]
)
def test_cpu_quantities_become_millis(text, millis):
    assert bench.millis(text) == millis


def container(cpu=None, memory=None):
    requests = {key: value for key, value in (("cpu", cpu), ("memory", memory)) if value}
    return {"name": "c", "resources": {"requests": requests}} if requests else {"name": "bare"}


def pod(node, *containers, phase="Running", init=()):
    spec = {"nodeName": node, "containers": list(containers)}
    if init:
        spec["initContainers"] = list(init)
    return {"spec": spec, "status": {"phase": phase}}


LISTING = {
    "items": [
        {
            "metadata": {
                "name": "n1",
                "labels": {"anton.io/pool": "general", "node.kubernetes.io/instance-type": "m6i"},
            },
            "status": {"allocatable": {"cpu": "4", "memory": "16Gi", "ephemeral-storage": "100Gi"}},
        },
        {"metadata": {"name": "n2"}, "status": {"allocatable": {"cpu": "2", "memory": "8Gi"}}},
        {"metadata": {"name": "n3"}, "status": {"allocatable": {"cpu": "1", "memory": "1Gi"}}},
    ]
}
PODS = {
    "items": [
        pod("n1", container("250m", "512Mi"), container("250m", "512Mi")),
        pod("n1", container("100m", "64Mi"), init=(container("2", "2Gi"),)),
        pod("n1", container("3", "8Gi"), phase="Succeeded"),
        pod("n1", container("3", "8Gi"), phase="Failed"),
        pod("", container("3", "8Gi")),
        {"spec": {"containers": [container("3", "8Gi")]}, "status": {"phase": "Pending"}},
        pod("n2", container(), container(cpu="300m"), phase="Pending"),
    ]
}


def test_nodes_subtract_scheduled_pod_requests_per_node():
    assert bench.nodes(LISTING, PODS) == [
        {
            "name": "n1",
            "pool": "general",
            "instance_type": "m6i",
            "allocatable_memory_mib": 16384,
            "allocatable_cpu_millis": 4000,
            "allocatable_ephemeral_mib": 102400,
            "colocated_memory_mib": 3072,
            "colocated_cpu_millis": 2500,
            "colocated_pods": 2,
        },
        {
            "name": "n2",
            "pool": "",
            "instance_type": "",
            "allocatable_memory_mib": 8192,
            "allocatable_cpu_millis": 2000,
            "allocatable_ephemeral_mib": 0,
            "colocated_memory_mib": 0,
            "colocated_cpu_millis": 300,
            "colocated_pods": 1,
        },
        {
            "name": "n3",
            "pool": "",
            "instance_type": "",
            "allocatable_memory_mib": 1024,
            "allocatable_cpu_millis": 1000,
            "allocatable_ephemeral_mib": 0,
            "colocated_memory_mib": 0,
            "colocated_cpu_millis": 0,
            "colocated_pods": 0,
        },
    ]


def test_a_partial_mib_of_requests_rounds_up():
    listing = {"items": [{"metadata": {"name": "n"}, "status": {"allocatable": {"cpu": "1", "memory": "1Gi"}}}]}
    pods = {"items": [pod("n", container("1m", "1Ki"))]}
    (row,) = bench.nodes(listing, pods)
    assert (row["colocated_memory_mib"], row["colocated_cpu_millis"]) == (1, 1)


def test_versions_take_the_first_line_of_either_stream(monkeypatch):
    outputs = {
        "out": SimpleNamespace(stdout="\n  v1 build  \nmore\n", stderr=""),
        "err": SimpleNamespace(stdout="", stderr="Python 2\n"),
        "quiet": SimpleNamespace(stdout=" \n", stderr=""),
    }
    calls = []

    def fake(command, **kwargs):
        calls.append(kwargs)
        if command[0] == "missing":
            raise FileNotFoundError(command[0])
        if command[0] == "slow":
            raise subprocess.TimeoutExpired(command, 60)
        return outputs[command[0]]

    monkeypatch.setattr(bench.subprocess, "run", fake)
    tools = {"a": ["missing"], "b": ["out"], "c": ["slow"], "d": ["err"], "e": ["quiet"]}
    assert bench.versions(tools) == {"a": "absent", "b": "v1 build", "c": "absent", "d": "Python 2", "e": "absent"}
    assert calls == [{"capture_output": True, "text": True, "timeout": 60}] * 5


def test_meminfo_reads_whole_mib():
    assert bench.meminfo("MemTotal:  2049000 kB\nMemAvailable: 1025 kB\n") == {"MemTotal": 2000, "MemAvailable": 1}


def fixed_host(monkeypatch):
    monkeypatch.setattr(bench.os, "getloadavg", lambda: (1.23456, 9.0, 9.0))
    monkeypatch.setattr(bench.os, "cpu_count", lambda: 3)
    monkeypatch.setattr(bench.platform, "release", lambda: "k1")
    monkeypatch.setattr(bench, "meminfo", lambda text: {"MemTotal": 10, "MemAvailable": 4})


def test_host_records_cpus_memory_load_and_kernel(monkeypatch):
    fixed_host(monkeypatch)
    assert bench.host() == {
        "cpus": 3,
        "memory_total_mib": 10,
        "memory_available_mib": 4,
        "load_average_1m": 1.23,
        "kernel": "k1",
    }


def test_environment_passes_only_named_variables(monkeypatch):
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/pw")
    monkeypatch.setenv("SV2_FAKE_TOKEN", "fake-token-value")
    assert bench.environment(Path("/w"), 3) == {
        "PATH": "/bin",
        "PLAYWRIGHT_BROWSERS_PATH": "/pw",
        "HOME": "/w",
        "TMPDIR": "/w",
        "CHILDREN": "3",
    }


def suite(folder, workloads, repeats=1):
    path = folder / "suite.json"
    path.write_text(json.dumps({"suite": "fixture", "repeats": repeats, "interval": 0.05, "workloads": workloads}))
    return path


def test_run_measures_each_child_count_and_repeat_in_a_fresh_workdir(tmp_path, monkeypatch):
    fixed_host(monkeypatch)
    monkeypatch.setenv("SV2_FAKE_TOKEN", "fake-token-value")
    log = tmp_path / "probe.log"
    path = suite(
        tmp_path,
        [
            {
                "name": "memory",
                "class": "memory",
                "version": "1",
                "children": [1, 2],
                "command": [
                    "{python}",
                    str(WORKLOADS / "memory.py"),
                    "--children",
                    "{children}",
                    "--mib",
                    "16",
                    "--hold",
                    "0.3",
                ],
            },
            {
                "name": "probe",
                "class": "idle",
                "version": "2",
                "children": [0],
                "command": ["{python}", "-c", PROBE, str(log), "{workdir}"],
            },
        ],
        repeats=2,
    )
    root = tmp_path / "work" / "nested"
    result = bench.run(path, root, generation=7, tools={"python": [sys.executable, "--version"]}, source="s1")
    assert result["suite"] == "fixture" and result["generation"] == 7 and result["source"] == "s1"
    assert result["tools"] == {
        "python": f"Python {sys.version.split()[0]}",
        "workload:memory": "1",
        "workload:probe": "2",
    }
    assert [(r["workload"], r["class"], r["children"], r["repeat"]) for r in result["runs"]] == [
        ("memory", "memory", 1, 1),
        ("memory", "memory", 1, 2),
        ("memory", "memory", 2, 1),
        ("memory", "memory", 2, 2),
        ("probe", "idle", 0, 1),
        ("probe", "idle", 0, 2),
    ]
    assert all(r["exit_code"] == 0 and not r["timed_out"] for r in result["runs"])
    assert result["runs"][2]["peak_processes"] >= 3
    assert result["host"] == {
        "cpus": 3,
        "memory_total_mib": 10,
        "memory_available_mib": 4,
        "load_average_1m": 1.23,
        "kernel": "k1",
        "load_average_1m_after": 1.23,
    }
    lines = [line.split() for line in log.read_text().splitlines()]
    assert len(lines) == 2 and lines[0][0] != lines[1][0]
    for cwd, workdir, home, tmp, children, leaked in lines:
        assert cwd == workdir == home == tmp
        assert Path(cwd).parent == root.resolve() and Path(cwd).name.startswith("probe-")
        assert (children, leaked) == ("0", "False")
    assert "fake-token-value" not in json.dumps(result)
    assert list(root.iterdir()) == []
    bench.run(path, root, generation=8, tools={}, source="s2")


def test_run_stops_a_workload_at_its_timeout(tmp_path):
    command = ["{python}", "-c", "import time; time.sleep(30)"]
    path = suite(
        tmp_path,
        [{"name": "slow", "class": "general", "version": "1", "children": [1], "timeout": 0.5, "command": command}],
    )
    (row,) = bench.run(path, tmp_path / "w", generation=1, tools={}, source="")["runs"]
    assert row["timed_out"] and row["wall_seconds"] < 10


def test_run_resolves_workloads_beside_a_relative_suite_path(tmp_path, monkeypatch):
    folder = tmp_path / "suite"
    folder.mkdir()
    (folder / "memory.py").write_bytes((WORKLOADS / "memory.py").read_bytes())
    command = ["{python}", "{workloads}/memory.py", "--children", "{children}", "--mib", "8", "--hold", "0.1"]
    suite(folder, [{"name": "memory", "class": "memory", "version": "1", "children": [1], "command": command}])
    monkeypatch.chdir(folder)
    result = bench.run(Path("suite.json"), Path("work"), generation=1, tools={}, source="")
    assert [r["exit_code"] for r in result["runs"]] == [0]


def memory_suite(folder):
    command = ["{python}", str(WORKLOADS / "memory.py"), "--children", "{children}", "--mib", "8", "--hold", "0.2"]
    workload = {"name": "memory", "class": "memory", "version": "1", "children": [1], "command": command}
    return suite(folder, [workload], repeats=3)


NODE_LISTING = {"items": [{"metadata": {"name": "n"}, "status": {"allocatable": {"cpu": "4", "memory": "16Gi"}}}]}


def test_cli_runs_reads_nodes_derives_and_renders(tmp_path):
    files = tmp_path / "cli"
    files.mkdir()
    measurements = files / "out" / "deep" / "measurements.json"
    run_args = ["run", "--suite", str(memory_suite(files)), "--root", str(files / "w"), "--generation", "3"]
    assert bench.main([*run_args, "--source", "s3", "--output", str(measurements)]) == 0
    measured = json.loads(measurements.read_text())
    assert measurements.read_text() == json.dumps(measured, indent=2) + "\n"
    assert (measured["generation"], measured["source"]) == (3, "s3")
    assert set(measured["tools"]) == {*bench.TOOLS, "workload:memory"}
    (files / "listing.json").write_text(json.dumps(NODE_LISTING))
    (files / "pods.json").write_text(json.dumps({"items": []}))
    nodes = files / "nodes.json"
    assert (
        bench.main(
            [
                "nodes",
                "--nodes",
                str(files / "listing.json"),
                "--pods",
                str(files / "pods.json"),
                "--output",
                str(nodes),
            ]
        )
        == 0
    )
    capacity = json.loads(nodes.read_text())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", capacity["read_at"])
    assert capacity["nodes"] == bench.nodes(NODE_LISTING, {"items": []})
    assert capacity["inputs"] == {
        "nodes": hashlib.sha256((files / "listing.json").read_bytes()).hexdigest(),
        "pods": hashlib.sha256((files / "pods.json").read_bytes()).hexdigest(),
    }
    stamped = files / "stamped.json"
    listing_args = ["--nodes", str(files / "listing.json"), "--pods", str(files / "pods.json")]
    assert bench.main(["nodes", *listing_args, "--read-at", "2026-10-11T12:29:17Z", "--output", str(stamped)]) == 0
    assert json.loads(stamped.read_text())["read_at"] == "2026-10-11T12:29:17Z"
    report, page = files / "report.json", files / "report.md"
    derive = ["derive", "--measurements", str(measurements), "--nodes", str(nodes), "--output", str(report)]
    assert bench.main([*derive, "--markdown", str(page)]) == 0
    derived = json.loads(report.read_text())
    assert sorted(derived["profiles"]) == ["memory"] and derived["generation"] == 3
    assert derived["policy"]["min_repeats"] == 3
    assert [row["node"] for row in derived["coexistence"]] == ["n"]
    assert page.read_text() == bench.profiles.markdown(derived)
    assert bench.main([*derive, "--min-repeats", "4"]) == 2


def test_cli_derive_refuses_an_idle_only_measurement(tmp_path, capsys):
    measurements = tmp_path / "m.json"
    idle = {"workload": "idle", "class": "idle", "children": 0, "repeat": 1, "peak_rss_mib": 40.0, "peak_processes": 1}
    measurements.write_text(json.dumps({"generation": 1, "tools": {}, "runs": [idle]}))
    nodes = tmp_path / "n.json"
    nodes.write_text('{"nodes": []}')
    report = tmp_path / "r.json"
    code = bench.main(["derive", "--measurements", str(measurements), "--nodes", str(nodes), "--output", str(report)])
    assert code == 2 and not report.exists()
    assert capsys.readouterr().err == "refused: no class was admitted: idle: an idle sample cannot size a worker\n"


def test_cli_promote_prints_the_decision_and_keeps_the_accepted_file(tmp_path, capsys):
    accepted = tmp_path / "accepted.json"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"generation": 1, "profiles": {}, "tools": {}}))
    promote = ["promote", "--accepted", str(accepted), "--candidate"]
    assert bench.main([*promote, str(candidate)]) == 0
    assert capsys.readouterr().out == '{"decision": "promoted", "changes": null}\n'
    assert bench.main([*promote, str(candidate)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "replayed"
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"generation": 0, "profiles": {}, "tools": {}}))
    assert bench.main([*promote, str(stale)]) == 1
    assert json.loads(capsys.readouterr().out)["decision"] == "stale"


def report_with(generation, peak):
    measured = {metric: {"max": peak} for metric in bench.profiles.COMPARED}
    return {"generation": generation, "tools": {}, "profiles": {"memory": {"measured": measured}}}


def test_cli_promote_holds_a_material_change_with_the_default_tolerance(tmp_path, capsys):
    accepted, candidate = tmp_path / "accepted.json", tmp_path / "candidate.json"
    accepted.write_text(json.dumps(report_with(1, 100)))
    candidate.write_text(json.dumps(report_with(2, 114)))
    promote = ["promote", "--accepted", str(accepted), "--candidate", str(candidate)]
    assert bench.main(promote) == 0
    capsys.readouterr()
    candidate.write_text(json.dumps(report_with(3, 200)))
    assert bench.main(promote) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["decision"] == "requalify" and len(printed["changes"]["material"]) == 4
    assert json.loads(accepted.read_text())["generation"] == 2


def test_cli_requalifies_a_held_report_for_a_named_reviewer_and_rolls_it_back(tmp_path, capsys):
    accepted, candidate = tmp_path / "accepted.json", tmp_path / "candidate.json"
    accepted.write_text(json.dumps(report_with(1, 100)))
    candidate.write_text(json.dumps(report_with(2, 200)))
    promote = ["promote", "--accepted", str(accepted), "--candidate", str(candidate)]
    assert bench.main([*promote, "--requalified-by", "operator"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["decision"] == "requalified" and printed["changes"]["requalified_by"] == "operator"
    assert json.loads(accepted.read_text())["generation"] == 2
    assert bench.main(["rollback", "--accepted", str(accepted)]) == 0
    assert capsys.readouterr().out == '{"restored": 1}\n'
    assert json.loads(accepted.read_text())["generation"] == 1
    assert bench.main(["rollback", "--accepted", str(accepted)]) == 0
    assert capsys.readouterr().out == '{"restored": 2}\n'
    accepted.with_name("accepted.json.previous").unlink()
    assert bench.main(["rollback", "--accepted", str(accepted)]) == 1
    assert capsys.readouterr().out == '{"restored": null}\n'


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run", "--root", "r", "--generation", "1", "--source", "s", "--output", "o"],
        ["run", "--suite", "s", "--generation", "1", "--source", "s", "--output", "o"],
        ["run", "--suite", "s", "--root", "r", "--source", "s", "--output", "o"],
        ["run", "--suite", "s", "--root", "r", "--generation", "1", "--output", "o"],
        ["run", "--suite", "s", "--root", "r", "--generation", "1", "--source", "s"],
        ["run", "--suite", "s", "--root", "r", "--generation", "one", "--source", "s", "--output", "o"],
        ["nodes", "--pods", "p", "--output", "o"],
        ["derive", "--measurements", "m", "--nodes", "n"],
        ["derive", "--measurements", "m", "--nodes", "n", "--output", "o", "--min-repeats", "x"],
        ["promote", "--accepted", "a"],
        ["rollback"],
    ],
)
def test_cli_refuses_a_missing_or_malformed_argument(argv):
    with pytest.raises(SystemExit) as stopped:
        bench.main(argv)
    assert stopped.value.code == 2
