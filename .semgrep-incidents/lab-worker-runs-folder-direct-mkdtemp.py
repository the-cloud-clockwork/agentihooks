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
