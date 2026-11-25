from typing import Any


def required_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # ruleid: plant-check-name-truncated
    return [row for row in rows if row["name"].rsplit("/", 1)[-1] == "required"]


def last_segment(name: str) -> str:
    # ruleid: plant-check-name-truncated
    return name.rsplit("/")[-1]


def verdict_checks(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # ruleid: plant-check-name-truncated
    return {row["name"].split("/")[-1]: row for row in rows}


def second_segment(name: str) -> str:
    # ruleid: plant-check-name-truncated
    return name.split("/")[1]


def complete_suffix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # ok: plant-check-name-truncated
    return [row for row in rows if row["name"].split("/", 1)[1] == "required"]


def partitioned_suffix(name: str) -> str:
    # ok: plant-check-name-truncated
    return name.partition("/")[2]


def run_number(url: str) -> str:
    # ok: plant-check-name-truncated
    return url.partition(":")[0]


def tree_mode(line: str) -> list[str]:
    # ok: plant-check-name-truncated
    return line.split("\t", 1)[0].split()
