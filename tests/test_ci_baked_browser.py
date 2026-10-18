import os
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[1] / ".github/actions/browser-cache"


@pytest.mark.parametrize(
    "present,usable,existing",
    [(True, True, False), (True, False, False), (False, False, False), (True, True, True)],
)
def test_image_browser_is_used_only_after_a_successful_launch(tmp_path, present, usable, existing):
    baked = tmp_path / "baked"
    if present:
        (baked / "chromium_headless_shell-1234").mkdir(parents=True)
        (baked / "chromium-1234").mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / ".playwright"
    if existing:
        (target / "chromium-1234").mkdir(parents=True)
        (target / "chromium-1234" / "existing").write_text("preserved")
    image_output = tmp_path / "image-output"
    launch_output = tmp_path / "launch-output"
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'echo "$PLAYWRIGHT_BROWSERS_PATH" >> "$CALLS"\n'
        '[[ "$(readlink -f "$PLAYWRIGHT_BROWSERS_PATH")" == "$(readlink -f "$ARC_PLAYWRIGHT_BROWSERS_PATH")" ]]\n'
        '[[ "$USABLE" == true ]]\n'
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARC_PLAYWRIGHT_BROWSERS_PATH": str(baked),
        "PLAYWRIGHT_BROWSERS_PATH": str(target),
        "GITHUB_ACTION_PATH": str(ACTION),
        "CALLS": str(calls),
        "USABLE": str(usable).lower(),
        "CACHE_HIT": "",
    }
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]
    image = next(step for step in steps if step.get("id") == "image")
    launch = next(step for step in steps if step.get("id") == "launch")
    for step, output in ((image, image_output), (launch, launch_output)):
        if step is launch:
            env["IMAGE_LINKED"] = image_output.read_text().strip().removeprefix("linked=")
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step["run"]],
            env={**env, "GITHUB_OUTPUT": str(output)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    assert image_output.read_text().strip() == f"linked={str(present and usable and not existing).lower()}"
    assert launch_output.read_text().strip() == f"ready={str(present and usable and not existing).lower()}"
    if present and not existing:
        assert calls.read_text().splitlines() == [str(target)]
    else:
        assert not calls.exists()
    if existing:
        assert (target / "chromium-1234" / "existing").read_text() == "preserved"
        assert sorted(path.name for path in (target / "chromium-1234").iterdir()) == ["existing"]
    elif present and usable:
        assert (target / "chromium-1234").resolve() == baked / "chromium-1234"
        assert (target / "chromium_headless_shell-1234").resolve() == baked / "chromium_headless_shell-1234"
    else:
        assert not target.exists() and not target.is_symlink()


def test_usable_image_browser_skips_cache_restoration_and_duplicate_launch():
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]
    image = next(step for step in steps if step.get("id") == "image")
    cache = next(step for step in steps if step.get("id") == "cache")
    version = next(step for step in steps if step.get("id") == "version")
    launch = next(step for step in steps if step.get("id") == "launch")
    assert steps.index(image) < steps.index(cache)
    assert cache["if"] == "steps.image.outputs.linked != 'true'"
    assert version["if"] == "steps.image.outputs.linked != 'true'"
    assert launch["env"]["IMAGE_LINKED"] == "${{ steps.image.outputs.linked }}"
    assert "verify.sh" in image["run"]
    assert launch["run"].count("verify.sh") == 1


@pytest.mark.parametrize("present,usable", [(True, True), (True, False), (False, False)])
def test_queue_browser_is_verified_from_the_image_without_installing(tmp_path, present, usable):
    baked = tmp_path / "baked"
    if present:
        baked.mkdir()
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'echo launch >> "$CALLS"\n'
        '[[ "$*" != *install* ]]\n'
        '[[ "$(readlink -f "$PLAYWRIGHT_BROWSERS_PATH")" == "$ARC_PLAYWRIGHT_BROWSERS_PATH" ]]\n'
        '[[ "$USABLE" == true ]]\n'
    )
    python.chmod(0o755)
    target = tmp_path / "workspace" / ".playwright"
    target.parent.mkdir()
    result = subprocess.run(
        ["bash", str(ACTION / "use-image.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "ARC_PLAYWRIGHT_BROWSERS_PATH": str(baked),
            "PLAYWRIGHT_BROWSERS_PATH": str(target),
            "CALLS": str(calls),
            "USABLE": str(usable).lower(),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (present and usable), result.stderr
    attempts = calls.read_text().splitlines() if calls.exists() else []
    assert not any("install" in call for call in attempts)
    assert len(attempts) == int(present)
    if present:
        assert target.resolve() == baked
