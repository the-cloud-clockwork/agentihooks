"""Gives every test pytest-split has no stored time for the mean stored time of its own file.

pytest-split falls back to the suite mean, so a new test in a slow file counts as a fast one and
its group runs over its estimate. A test whose file has no stored time keeps the suite mean.
"""

import pytest


def file_mean_durations(nodeids: list[str], durations: dict[str, float]) -> dict[str, float]:
    by_file: dict[str, list[float]] = {}
    for nodeid in nodeids:
        if nodeid in durations:
            by_file.setdefault(nodeid.split("::", 1)[0], []).append(durations[nodeid])
    filled = dict(durations)
    for nodeid in nodeids:
        seconds = by_file.get(nodeid.split("::", 1)[0])
        if nodeid not in durations and seconds:
            filled[nodeid] = sum(seconds) / len(seconds)
    return filled


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    split = config.pluginmanager.get_plugin("pytestsplitplugin")
    if split is not None:
        split.cached_durations = file_mean_durations([item.nodeid for item in items], split.cached_durations)
