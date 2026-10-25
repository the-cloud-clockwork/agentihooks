import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_cpu_quota
from scripts.ci_cpu_quota import quota_cpus, worker_count

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _tree(tmp_path: Path, proc: str, files: dict[str, str]) -> tuple[Path, Path]:
    root = tmp_path / "cgroup"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    root.mkdir(exist_ok=True)
    (tmp_path / "proc").write_text(proc)
    return root, tmp_path / "proc"


def test_v2_quota_of_a_four_cpu_runner(tmp_path):
    root, proc = _tree(tmp_path, "0::/\n", {"cpu.max": "400000 100000\n"})
    assert quota_cpus(root, proc) == 4


def test_v2_unlimited_has_no_quota(tmp_path):
    root, proc = _tree(tmp_path, "0::/\n", {"cpu.max": "max 100000\n"})
    assert quota_cpus(root, proc) is None


def test_v2_tightest_ancestor_wins(tmp_path):
    root, proc = _tree(
        tmp_path,
        "0::/pod/runner\n",
        {"cpu.max": "max 100000\n", "pod/cpu.max": "200000 100000\n", "pod/runner/cpu.max": "800000 100000\n"},
    )
    assert quota_cpus(root, proc) == 2


def test_v1_quota(tmp_path):
    root, proc = _tree(
        tmp_path,
        "4:cpu,cpuacct:/kube/pod\n2:memory:/kube/pod\n",
        {"cpu/kube/pod/cpu.cfs_quota_us": "300000\n", "cpu/kube/pod/cpu.cfs_period_us": "100000\n"},
    )
    assert quota_cpus(root, proc) == 3


def test_v1_unlimited_has_no_quota(tmp_path):
    root, proc = _tree(
        tmp_path,
        "4:cpu,cpuacct:/\n",
        {"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"},
    )
    assert quota_cpus(root, proc) is None


def test_malformed_cpu_max_has_no_quota(tmp_path):
    root, proc = _tree(tmp_path, "0::/\n", {"cpu.max": "garbage\n"})
    assert quota_cpus(root, proc) is None


def test_v2_path_may_hold_a_colon(tmp_path):
    root, proc = _tree(tmp_path, "0::/pod:a\n", {"pod:a/cpu.max": "200000 100000\n"})
    assert quota_cpus(root, proc) == 2


@pytest.mark.parametrize(("quota", "cpus"), [("0", None), ("1", 0.00001)])
def test_v1_only_a_positive_quota_limits(tmp_path, quota, cpus):
    root, proc = _tree(
        tmp_path,
        "4:cpu:/\n",
        {"cpu/cpu.cfs_quota_us": f"{quota}\n", "cpu/cpu.cfs_period_us": "100000\n"},
    )
    assert quota_cpus(root, proc) == cpus


def test_v1_other_controllers_are_not_read(tmp_path):
    root, proc = _tree(
        tmp_path,
        "4:cpu:/pod\n2:memory:/other\n",
        {
            "cpu/pod/cpu.cfs_quota_us": "300000\n",
            "cpu/pod/cpu.cfs_period_us": "100000\n",
            "cpu/other/cpu.cfs_quota_us": "100000\n",
            "cpu/other/cpu.cfs_period_us": "100000\n",
        },
    )
    assert quota_cpus(root, proc) == 3


def test_v1_reads_a_joined_controller_mount(tmp_path):
    root, proc = _tree(
        tmp_path,
        "4:cpu,cpuacct:/pod\n",
        {"cpu,cpuacct/pod/cpu.cfs_quota_us": "200000\n", "cpu,cpuacct/pod/cpu.cfs_period_us": "100000\n"},
    )
    assert quota_cpus(root, proc) == 2


@pytest.mark.parametrize(
    "proc",
    ["0::/v2\n4:cpu:/v1\n", "4:cpu:/v1\n0::/v2\n"],
)
@pytest.mark.parametrize(("v1", "v2", "cpus"), [("200000", "300000", 2), ("300000", "200000", 2)])
def test_every_hierarchy_counts_toward_the_tightest_limit(tmp_path, proc, v1, v2, cpus):
    root, proc_file = _tree(
        tmp_path,
        proc,
        {
            "v2/cpu.max": f"{v2} 100000\n",
            "cpu/v1/cpu.cfs_quota_us": f"{v1}\n",
            "cpu/v1/cpu.cfs_period_us": "100000\n",
        },
    )
    assert quota_cpus(root, proc_file) == cpus


def test_main_prints_the_count_and_explains_it(monkeypatch, capsys):
    monkeypatch.setattr(ci_cpu_quota, "quota_cpus", lambda: 2.5)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)))
    ci_cpu_quota.main()
    out, err = capsys.readouterr()
    assert out == "2\n"
    assert err == "pytest workers 2: cpu quota 2.5, affinity 16\n"


def test_unreadable_cgroup_has_no_quota(tmp_path):
    assert quota_cpus(tmp_path / "missing", tmp_path / "missing-proc") is None


@pytest.mark.parametrize(
    ("cpus", "quota", "workers"),
    [(16, 4.0, 4), (16, 3.5, 3), (16, 0.5, 1), (2, 4.0, 2), (16, None, 16), (0, None, 1)],
)
def test_workers_never_exceed_the_quota_or_the_cores(cpus, quota, workers):
    assert worker_count(cpus, quota) == workers


def test_module_prints_a_worker_count_within_this_container():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci_cpu_quota"], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    workers = int(result.stdout)
    assert 1 <= workers <= len(os.sched_getaffinity(0))
    quota = quota_cpus()
    if quota is not None:
        assert workers <= max(1, int(quota))
    assert f"pytest workers {workers}" in result.stderr
