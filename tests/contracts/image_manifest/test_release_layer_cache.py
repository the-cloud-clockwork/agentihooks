from pathlib import Path

import yaml


def test_release_build_reuses_the_qualified_development_image_cache() -> None:
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/swarm-node-image.yml").read_text())
    steps = workflow["jobs"]["image"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build the candidate")

    assert "--build-arg BUILDKIT_INLINE_CACHE=1" in build["run"]
    assert "--cache-from type=registry,ref=ghcr.io/the-cloud-clockwork/agentihooks-worker:dev" in build["run"]
