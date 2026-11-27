import tempfile
from pathlib import Path
from typing import Any


def cuts_a_units_report_folder_directly_in_the_shared_runs_folder(pod: Any, record: dict[str, Any]) -> Path:
    # ruleid: lab-worker-runs-folder-direct-mkdtemp
    path = Path(tempfile.mkdtemp(dir=pod.runs)) / "report.json"
    path.write_text(str(record))
    return path


def cuts_a_mutation_runners_request_folder_directly_in_the_shared_runs_folder(pod: Any) -> Path:
    # ruleid: lab-worker-runs-folder-direct-mkdtemp
    return Path(tempfile.mkdtemp(dir=pod.runs))


def cuts_a_units_report_folder_inside_the_root_the_worker_handed_in(root: Path, record: dict[str, Any]) -> Path:
    # ok: lab-worker-runs-folder-direct-mkdtemp
    path = Path(tempfile.mkdtemp(dir=root)) / "report.json"
    path.write_text(str(record))
    return path


def cuts_a_mutation_runners_request_folder_inside_the_root_the_worker_handed_in(root: Path) -> Path:
    # ok: lab-worker-runs-folder-direct-mkdtemp
    return Path(tempfile.mkdtemp(dir=root))


def hands_the_shared_runs_folder_to_the_static_runner_as_its_root(pod: Any, run_static: Any, venv: Path) -> Any:
    # ruleid: lab-worker-runs-folder-direct-mkdtemp
    return run_static.Settings(pod.runs, venv, pod.ports)


def hands_the_shared_runs_folder_to_the_test_runner_as_its_root(pod: Any, run_tests: Any) -> Any:
    # ruleid: lab-worker-runs-folder-direct-mkdtemp
    return run_tests.Lab(pod.runs, pod.ports, pod.whitelist)


def hands_the_shared_runs_folder_to_a_mutation_sandbox_as_its_root(pod: Any, sandbox: Any, grader: Path) -> Any:
    # ruleid: lab-worker-runs-folder-direct-mkdtemp
    return sandbox(pod.runs, grader, pod.ports)


def hands_the_lease_root_to_the_static_runner(pod: Any, run_static: Any, root: Path, venv: Path) -> Any:
    # ok: lab-worker-runs-folder-direct-mkdtemp
    return run_static.Settings(root, venv, pod.ports)


def settings(pod: Any) -> Any:
    # ok: lab-worker-runs-folder-direct-mkdtemp
    for folder in (pod.runs, pod.venvs):
        folder.mkdir(parents=True, exist_ok=True)
    return pod


def main(pod: Any, worker: Any) -> Any:
    # ok: lab-worker-runs-folder-direct-mkdtemp
    return worker.build(pod, pod.runs)
