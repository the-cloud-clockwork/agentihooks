import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
_ACTION = "./.github/actions/setup-uv"


@pytest.mark.parametrize("job", ["test-count", "unit", "unit-rerun", "unit-selected"])
def test_unit_legs_resolve_image_uv_before_downloading(job: str) -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == "Set up uv")
    assert step["uses"] == _ACTION
    assert step["if"] == "steps.venv.outputs.cache-hit != 'true'"
    assert step["with"]["version"] == "0.12.23"
    assert step["with"]["enable-cache"] is False


@pytest.mark.parametrize(
    ("version", "status", "installed"),
    [
        ("uv 0.12.23", 0, True),
        ("uv 0.12.23 (abc123 2026-11-19)", 0, True),
        ("uv 0.12.230", 0, False),
        ("uv 0.12.22", 0, False),
        ("uv 0.12.23", 1, False),
        (None, 0, False),
    ],
    ids=["exact", "build-suffix", "version-prefix", "different", "broken", "missing"],
)
def test_only_working_image_uv_at_the_pinned_version_skips_the_download(
    tmp_path: Path, version: str | None, status: int, installed: bool
) -> None:
    action = yaml.safe_load((_ROOT / ".github/actions/setup-uv/action.yml").read_text())
    resolve, fallback = action["runs"]["steps"]
    binary_dir = tmp_path / ".local/bin"
    binary_dir.mkdir(parents=True)
    if version is not None:
        binary = binary_dir / "uv"
        binary.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\necho '{version}'\nexit {status}\n")
        binary.chmod(0o755)
    output = tmp_path / "output"
    output.touch()
    github_path = tmp_path / "path"
    github_path.touch()
    subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", resolve["run"]],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "UV_VERSION": "0.12.23",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_PATH": str(github_path),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert output.read_text() == ("installed=true\n" if installed else "")
    assert github_path.read_text() == (f"{binary_dir}\n" if installed else "")
    assert resolve["env"] == {"UV_VERSION": "${{ inputs.version }}"}
    assert fallback["if"] == f"steps.{resolve['id']}.outputs.installed != 'true'"
    assert fallback["uses"] == "astral-sh/setup-uv@v10.2.0"
    assert fallback["with"] == {
        "version": "${{ inputs.version }}",
        "checksum": "${{ inputs.checksum }}",
        "enable-cache": "${{ inputs.enable-cache }}",
    }
