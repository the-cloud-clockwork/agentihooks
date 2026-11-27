import os
import shutil
from pathlib import Path


def artifact(kind: str, leg: int) -> str:
    return f"{kind}-3.12-{leg}"


def copied(queue: Path, out: Path, source: int, leg: int) -> Path:
    # ruleid: unit-copy-coverage-copytree-without-link
    return shutil.copytree(queue / artifact("coverage", source), out / artifact("coverage", leg))


def copied_with_copy2(queue: Path, out: Path, source: int, leg: int) -> Path:
    # ruleid: unit-copy-coverage-copytree-without-link
    return shutil.copytree(
        queue / artifact("coverage", source), out / artifact("coverage", leg), copy_function=shutil.copy2
    )


def linked(queue: Path, out: Path, source: int, leg: int) -> Path:
    # ok: unit-copy-coverage-copytree-without-link
    return shutil.copytree(queue / artifact("coverage", source), out / artifact("coverage", leg), copy_function=os.link)


def durations(queue: Path, out: Path, source: int, leg: int) -> Path:
    # ok: unit-copy-coverage-copytree-without-link
    return shutil.copytree(queue / artifact("durations", source), out / artifact("durations", leg))
