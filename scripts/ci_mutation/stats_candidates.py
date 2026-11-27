from pathlib import Path

import pytest


def candidate_tests(items: list[pytest.Item], root: Path, changes: dict[str, list[int]]) -> list[pytest.Item]:
    return items
