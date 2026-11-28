import subprocess
from pathlib import Path


def bundles_prerequisite_on_every_parent(store: Path, ref: str, parents: list[str]) -> bytes:
    # ruleid: lab-bundle-excludes-every-parent
    excluded = [f"^{parent}" for parent in parents]
    command = ["git", "-C", str(store), "bundle", "create", "-", ref, *excluded]
    return subprocess.run(command, check=True, capture_output=True).stdout


def bundles_prerequisite_on_every_parent_inline(store: Path, ref: str, parents: list[str]) -> bytes:
    return subprocess.run(
        # ruleid: lab-bundle-excludes-every-parent
        ["git", "-C", str(store), "bundle", "create", "-", ref] + [f"^{parent}" for parent in parents],
        check=True,
        capture_output=True,
    ).stdout


def bundles_prerequisite_on_the_parents_a_generator_names(store: Path, ref: str, parents: list[str]) -> bytes:
    # ruleid: lab-bundle-excludes-every-parent
    command = ["git", "-C", str(store), "bundle", "create", "-", ref, *(f"^{parent}" for parent in parents)]
    return subprocess.run(command, check=True, capture_output=True).stdout


def bundles_prerequisite_on_the_first_two_parents(store: Path, ref: str, parents: list[str]) -> bytes:
    # ruleid: lab-bundle-excludes-every-parent
    excluded = [f"^{parent}" for parent in parents[:2]]
    command = ["git", "-C", str(store), "bundle", "create", "-", ref, *excluded]
    return subprocess.run(command, check=True, capture_output=True).stdout


def bundles_prerequisite_on_the_first_parent_only(store: Path, ref: str, parents: list[str]) -> bytes:
    # ok: lab-bundle-excludes-every-parent
    excluded = [f"^{parent}" for parent in parents[:1]]
    command = ["git", "-C", str(store), "bundle", "create", "-", ref, *excluded]
    return subprocess.run(command, check=True, capture_output=True).stdout


def bundles_prerequisite_on_the_first_parent_only_inline(store: Path, ref: str, parents: list[str]) -> bytes:
    return subprocess.run(
        # ok: lab-bundle-excludes-every-parent
        ["git", "-C", str(store), "bundle", "create", "-", ref] + [f"^{parent}" for parent in parents[:1]],
        check=True,
        capture_output=True,
    ).stdout
