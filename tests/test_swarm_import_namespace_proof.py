from pathlib import Path

import pytest

from scripts import ci_swarm_imports
from tests.test_ci_swarm_imports import source_tree

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "module", ["opentelemetry.sdk.metrics", "opentelemetry.exporter.otlp.proto.http.metric_exporter"]
)
def test_namespace_requires_its_owner(tmp_path: Path, module: str) -> None:
    root = source_tree(tmp_path, f"import {module}\n", "opentelemetry-api==1.45.1\n")
    assert ci_swarm_imports.check(root)[1] == {"opentelemetry"}
