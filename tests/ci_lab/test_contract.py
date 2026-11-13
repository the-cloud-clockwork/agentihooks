import json
from pathlib import Path

import pytest

from scripts import cli_delegates
from scripts.ci_lab import __main__ as lab_cli
from scripts.ci_lab import contract

DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "ci-lab.md"

EXPECTED_METRICS = {
    "ci_lab_units_queued": ("class",),
    "ci_lab_units_running": ("class",),
    "ci_lab_slots_total": (),
    "ci_lab_slots_busy": (),
    "ci_lab_slots_wanted": (),
    "ci_lab_queue_wait_seconds": ("class",),
    "ci_lab_unit_duration_seconds": ("kind",),
    "ci_lab_cache_hits_total": ("cache",),
    "ci_lab_cache_misses_total": ("cache",),
    "ci_lab_mirror_age_seconds": (),
    "ci_lab_core_seconds_total": ("class",),
    "ci_lab_heads_graded_total": ("class", "conclusion"),
    "ci_lab_budget_fraction": (),
    "ci_lab_class_shed": ("class",),
    "ci_lab_units_redelivered_total": (),
    "ci_lab_units_lost_total": (),
    "ci_lab_units_overdue": (),
    "ci_lab_workers_ready": (),
    "ci_lab_github_rate_remaining": (),
    "ci_lab_verdict_mismatch_total": ("gate",),
}

NAMED_CONSTANTS = {
    "UNITS_QUEUED": "ci_lab_units_queued",
    "UNITS_RUNNING": "ci_lab_units_running",
    "SLOTS_TOTAL": "ci_lab_slots_total",
    "SLOTS_BUSY": "ci_lab_slots_busy",
    "SLOTS_WANTED": "ci_lab_slots_wanted",
    "QUEUE_WAIT_SECONDS": "ci_lab_queue_wait_seconds",
    "UNIT_DURATION_SECONDS": "ci_lab_unit_duration_seconds",
    "CACHE_HITS_TOTAL": "ci_lab_cache_hits_total",
    "CACHE_MISSES_TOTAL": "ci_lab_cache_misses_total",
    "MIRROR_AGE_SECONDS": "ci_lab_mirror_age_seconds",
    "CORE_SECONDS_TOTAL": "ci_lab_core_seconds_total",
    "HEADS_GRADED_TOTAL": "ci_lab_heads_graded_total",
    "BUDGET_FRACTION": "ci_lab_budget_fraction",
    "CLASS_SHED": "ci_lab_class_shed",
    "UNITS_REDELIVERED_TOTAL": "ci_lab_units_redelivered_total",
    "UNITS_LOST_TOTAL": "ci_lab_units_lost_total",
    "UNITS_OVERDUE": "ci_lab_units_overdue",
    "WORKERS_READY": "ci_lab_workers_ready",
    "GITHUB_RATE_REMAINING": "ci_lab_github_rate_remaining",
    "VERDICT_MISMATCH_TOTAL": "ci_lab_verdict_mismatch_total",
}

SCHEDULER_DESCRIPTION = {
    "service": "scheduler",
    "ports": [8800, 9108],
    "environment": {
        "prefixes": ["AGENTIHOOKS_BUS", "CI_LAB_MINIO_", "GITHUB_APP_"],
        "names": ["CI_LAB_REDIS_URL", "CI_LAB_RUNTIME_CONFIG"],
    },
    "metrics": sorted(EXPECTED_METRICS),
}

WORKER_DESCRIPTION = {
    "service": "worker",
    "ports": [9108],
    "environment": {
        "prefixes": ["AGENTIHOOKS_BUS", "CI_LAB_MINIO_"],
        "names": ["CI_LAB_SCHEDULER_URL", "CI_LAB_SLOTS", "CI_LAB_GIT_MIRROR", "UV_CACHE_DIR"],
    },
    "metrics": sorted(EXPECTED_METRICS),
}


ENVIRONMENT_NAMES = (
    "AGENTIHOOKS_BUS",
    "CI_LAB_MINIO_",
    "GITHUB_APP_",
    "CI_LAB_REDIS_URL",
    "CI_LAB_RUNTIME_CONFIG",
    "CI_LAB_SCHEDULER_URL",
    "CI_LAB_SLOTS",
    "CI_LAB_GIT_MIRROR",
    "UV_CACHE_DIR",
)


def test_delegates_route_to_the_two_services() -> None:
    assert cli_delegates.delegated_cli(["ci-lab-scheduler", "--self-test"]) is contract.scheduler
    assert cli_delegates.delegated_cli(["ci-lab-worker", "--self-test"]) is contract.worker


def test_ports_are_fixed() -> None:
    assert contract.SCHEDULER_PORT == 8800
    assert contract.METRICS_PORT == 9108


def test_metric_names_and_labels_match_the_architecture() -> None:
    assert dict(contract.METRICS) == EXPECTED_METRICS


@pytest.mark.parametrize(("constant", "name"), sorted(NAMED_CONSTANTS.items()))
def test_metric_constants_are_importable_by_name(constant: str, name: str) -> None:
    assert getattr(contract, constant) == name


def test_environment_names() -> None:
    assert contract.BUS_PREFIX == "AGENTIHOOKS_BUS"
    assert contract.MINIO_PREFIX == "CI_LAB_MINIO_"
    assert contract.GITHUB_APP_PREFIX == "GITHUB_APP_"
    assert contract.REDIS_URL == "CI_LAB_REDIS_URL"
    assert contract.SCHEDULER_URL == "CI_LAB_SCHEDULER_URL"
    assert contract.SLOTS == "CI_LAB_SLOTS"
    assert contract.GIT_MIRROR == "CI_LAB_GIT_MIRROR"
    assert contract.UV_CACHE_DIR == "UV_CACHE_DIR"
    assert contract.RUNTIME_CONFIG == "CI_LAB_RUNTIME_CONFIG"


def test_scheduler_self_test_passes_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.run(contract.SCHEDULER, ["--self-test"], {}) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == SCHEDULER_DESCRIPTION
    assert captured.err == ""


def test_worker_self_test_passes_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.run(contract.WORKER, ["--self-test"], {}) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == WORKER_DESCRIPTION
    assert captured.err == ""


def test_delegates_read_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    assert contract.scheduler(["--self-test"]) == 0
    assert json.loads(capsys.readouterr().out) == SCHEDULER_DESCRIPTION
    assert contract.worker(["--self-test"]) == 1
    assert capsys.readouterr().err == "ci-lab worker refuses to start: GITHUB_APP_ID is set\n"


@pytest.mark.parametrize("variable", ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_INSTALLATION_ID"])
def test_worker_refuses_any_github_app_variable(variable: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.run(contract.WORKER, ["--self-test"], {variable: "x"}) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ci-lab worker refuses to start: {variable} is set\n"


def test_worker_refuses_before_reading_its_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert contract.run(contract.WORKER, [], {"GITHUB_APP_ID": "1"}) == 1
    assert capsys.readouterr().err == "ci-lab worker refuses to start: GITHUB_APP_ID is set\n"


def test_worker_refusal_names_every_github_app_variable(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"GITHUB_APP_PRIVATE_KEY": "k", "GITHUB_APP_ID": "1", "CI_LAB_SLOTS": "4"}
    assert contract.run(contract.WORKER, ["--self-test"], environ) == 1
    assert capsys.readouterr().err == ("ci-lab worker refuses to start: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY is set\n")


def test_worker_accepts_names_that_only_share_the_word(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"GITHUB_APPS": "x", "MY_GITHUB_APP_ID": "y", "GITHUB_TOKEN": "z"}
    assert contract.run(contract.WORKER, ["--self-test"], environ) == 0
    assert capsys.readouterr().err == ""


def test_scheduler_accepts_github_app_variables(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "k"}
    assert contract.run(contract.SCHEDULER, ["--self-test"], environ) == 0
    assert capsys.readouterr().err == ""


def test_self_test_flag_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        contract.run(contract.SCHEDULER, [], {})
    assert raised.value.code == 2
    assert capsys.readouterr().err == (
        "usage: agentihooks ci-lab-scheduler [-h] [--self-test]\n"
        "agentihooks ci-lab-scheduler: error: only --self-test is available until the service loops land\n"
    )


def test_self_test_reports_a_planted_bad_metric_name() -> None:
    service = contract.SCHEDULER._replace(metrics={"ci-lab-bad": (), "lab_units": (), "ci_lab_units bad": ()})
    assert contract.self_test(service) == [
        "metric ci-lab-bad is not a valid ci_lab_ Prometheus name",
        "metric lab_units is not a valid ci_lab_ Prometheus name",
        "metric ci_lab_units bad is not a valid ci_lab_ Prometheus name",
    ]


def test_self_test_reports_planted_port_faults() -> None:
    assert contract.self_test(contract.SCHEDULER._replace(ports=(9108, 9108))) == ["ports repeat: 9108, 9108"]
    assert contract.self_test(contract.WORKER._replace(ports=(0, 65536))) == [
        "port 0 is outside 1 to 65535",
        "port 65536 is outside 1 to 65535",
    ]
    assert contract.self_test(contract.WORKER._replace(ports=(1, 65535))) == []


def test_self_test_reports_a_worker_granted_github_app() -> None:
    service = contract.WORKER._replace(prefixes=(*contract.WORKER.prefixes, "GITHUB_APP_"))
    assert contract.self_test(service) == ["worker environment grants GITHUB_APP_"]
    assert contract.self_test(contract.SCHEDULER) == []


def test_self_test_failure_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    service = contract.WORKER._replace(ports=(9108, 9108))
    assert contract.run(service, ["--self-test"], {}) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ci-lab worker self test failed: ports repeat: 9108, 9108\n"


def test_module_cli_runs_the_services(capsys: pytest.CaptureFixture[str]) -> None:
    assert lab_cli.main(["worker", "--self-test"], {}) == 0
    assert json.loads(capsys.readouterr().out) == WORKER_DESCRIPTION
    assert lab_cli.main(["scheduler", "--self-test"], {}) == 0
    assert json.loads(capsys.readouterr().out) == SCHEDULER_DESCRIPTION
    assert lab_cli.main(["worker", "--self-test"], {"GITHUB_APP_ID": "1"}) == 1


def test_doc_lists_the_contract() -> None:
    text = DOC.read_text()
    for name in [*EXPECTED_METRICS, *NAMED_CONSTANTS.values()]:
        assert f"`{name}`" in text, name
    for name in ENVIRONMENT_NAMES:
        assert f"`{name}" in text, name
    for port in ("8800", "9108"):
        assert f"`{port}`" in text, port
    for command in ("agentihooks ci-lab-scheduler --self-test", "agentihooks ci-lab-worker --self-test"):
        assert command in text, command
