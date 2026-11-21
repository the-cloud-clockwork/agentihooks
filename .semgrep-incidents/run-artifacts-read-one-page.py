import json
from collections.abc import Callable
from typing import Any


def one_page(api: Callable[[str], bytes], actions: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    # ruleid: run-artifacts-read-one-page-python
    return json.loads(api(f"{actions}/runs/{run['id']}/artifacts?per_page=100"))["artifacts"]


def one_page_of_a_row(api: Callable[..., dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    # ruleid: run-artifacts-read-one-page-python
    return api(f"actions/runs/{row['id']}/artifacts?per_page=100", deadline=60)["artifacts"]


def the_default_page(api: Callable[[str], bytes], actions: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    # ruleid: run-artifacts-read-one-page-python
    return json.loads(api(f"{actions}/runs/{run['id']}/artifacts"))["artifacts"]


def the_default_page_through_the_planner_reader(
    _api: Callable[[str], bytes], actions: str, run: dict[str, Any]
) -> list[dict[str, Any]]:
    # ruleid: run-artifacts-read-one-page-python
    return json.loads(_api(f"{actions}/runs/{run['id']}/artifacts"))["artifacts"]


def every_page(api: Callable[[str], bytes], actions: str, run: dict[str, Any], page: int) -> list[dict[str, Any]]:
    # ok: run-artifacts-read-one-page-python
    return json.loads(api(f"{actions}/runs/{run['id']}/artifacts?per_page=100&page={page}"))["artifacts"]


def by_name(api: Callable[[str], bytes], actions: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    # ok: run-artifacts-read-one-page-python
    return json.loads(api(f"{actions}/runs/{run['id']}/artifacts?name=durations-merged"))["artifacts"]


def by_name_after_the_page_size(api: Callable[[str], bytes], actions: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    # ok: run-artifacts-read-one-page-python
    return json.loads(api(f"{actions}/runs/{run['id']}/artifacts?per_page=100&name=coverage-baseline"))["artifacts"]


def endpoint_for_a_pager(actions: str, run: dict[str, Any]) -> str:
    # ok: run-artifacts-read-one-page-python
    return f"{actions}/runs/{run['id']}/artifacts"


def every_page_through_gh_paginate(
    rows: Callable[[str, str], list[dict[str, Any]]], repo: str, run: dict[str, Any]
) -> list[dict[str, Any]]:
    # ok: run-artifacts-read-one-page-python
    return rows(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100", "artifacts")
