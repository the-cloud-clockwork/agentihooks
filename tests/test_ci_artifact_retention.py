from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _uploads():
    files = [*(_ROOT / ".github/workflows").glob("*.yml"), *(_ROOT / ".github/actions").glob("*/action.yml")]
    for path in sorted(files):
        doc = yaml.safe_load(path.read_text())
        jobs = [job.get("steps") or [] for job in (doc.get("jobs") or {}).values()]
        for steps in [*jobs, (doc.get("runs") or {}).get("steps") or []]:
            for step in steps:
                uses = str(step.get("uses", ""))
                if uses.startswith("actions/upload-artifact@") or uses == "./.github/actions/retry-upload":
                    yield path.relative_to(_ROOT), step


def test_the_walk_finds_upload_steps():
    assert len(list(_uploads())) > 50


def test_every_upload_step_sets_retention_days():
    missing = [
        f"{path}: {step.get('name') or step['with'].get('name')}"
        for path, step in _uploads()
        if not str((step.get("with") or {}).get("retention-days", "")).strip()
    ]
    assert missing == []
