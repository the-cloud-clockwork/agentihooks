import subprocess
import tempfile
from pathlib import Path


def bundles_through_a_temp_file(store: Path, ref: str) -> bytes:
    # ruleid: lab-scheduler-temp-file-outside-its-store
    with tempfile.NamedTemporaryFile(suffix=".bundle") as handle:
        subprocess.run(["git", "-C", str(store), "bundle", "create", handle.name, ref], check=True)
        return Path(handle.name).read_bytes()


def checks_out_into_the_default_temp_folder() -> Path:
    # ruleid: lab-scheduler-temp-file-outside-its-store
    return Path(tempfile.mkdtemp())


def bundles_to_standard_output(store: Path, ref: str) -> bytes:
    command = ["git", "-C", str(store), "bundle", "create", "-", ref]
    # ok: lab-scheduler-temp-file-outside-its-store
    return subprocess.run(command, check=True, capture_output=True).stdout


def checks_out_under_the_store(root: Path) -> Path:
    # ok: lab-scheduler-temp-file-outside-its-store
    return Path(tempfile.mkdtemp(dir=root))
