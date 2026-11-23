import shutil
from pathlib import Path


def keeps_the_modes_of_the_head(chart: Path, copy: Path) -> None:
    # ruleid: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy)


def keeps_the_modes_by_name(chart: Path, copy: Path) -> None:
    # ruleid: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy, copy_function=shutil.copy2)


def copies_contents_only(chart: Path, copy: Path) -> None:
    # ok: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy, copy_function=shutil.copyfile)
