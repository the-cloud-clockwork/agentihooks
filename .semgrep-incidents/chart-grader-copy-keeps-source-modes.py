import shutil
from pathlib import Path


def keeps_the_modes_of_the_head(chart: Path, copy: Path) -> None:
    # ruleid: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy)
    (copy / "Chart.yaml").write_text("version: 999.999.999\n")


def keeps_the_modes_by_name(chart: Path, copy: Path) -> None:
    # ruleid: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy, copy_function=shutil.copy2)
    meta = (copy / "Chart.yaml").read_text()
    (copy / "Chart.yaml").write_text(meta.replace("1.2.3", "999.999.999"))


def copies_contents_only(chart: Path, copy: Path) -> None:
    # ok: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy, copy_function=shutil.copyfile)
    (copy / "Chart.yaml").write_text("version: 999.999.999\n")


def copies_a_chart_it_only_reads(chart: Path, copy: Path) -> str:
    # ok: chart-grader-copy-keeps-source-modes
    shutil.copytree(chart, copy)
    return (copy / "Chart.yaml").read_text()
