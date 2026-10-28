"""Workflow layers: package, bundle, user bundle, each chained profile, then the project; a later layer wins by name."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hooks.context import profile_chain
from hooks.context.conditions import repo_root
from scripts.workflows import schema

WORKFLOWS = "workflows"
PACKAGE_WORKFLOWS = profile_chain.BUILT_IN_PROFILES / "package" / WORKFLOWS
PROJECT_WORKFLOWS = Path(".agentihooks") / WORKFLOWS
SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class Found:
    layer: str
    path: Path
    workflow: schema.Workflow


def layer_dirs(state: dict, cwd: str | Path | None) -> list[tuple[str, Path]]:
    bundle = profile_chain.bundle_path(state)
    user = profile_chain.user_bundle_path(state)
    layers = [("package", PACKAGE_WORKFLOWS)]
    if bundle is not None:
        layers.append(("bundle", bundle / WORKFLOWS))
    if user is not None:
        layers.append(("user-bundle", user / WORKFLOWS))
    chain = profile_chain.profile_dirs(
        bundle, profile_chain.active_profile(state), profile_chain.linked_profiles(state), user
    )
    layers.extend((f"profile:{name}", directory / WORKFLOWS) for name, directory in chain)
    root = repo_root(cwd)
    if root is not None:
        layers.append(("project", root / PROJECT_WORKFLOWS))
    return layers


def workflow_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.suffix in SUFFIXES and path.is_file())


def read(path: Path) -> schema.Workflow:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise schema.WorkflowError([f"unreadable: {exc}"]) from exc
    return schema.parse(text)


def load(layers: list[tuple[str, Path]]) -> tuple[dict[str, Found], list[str]]:
    found: dict[str, Found] = {}
    errors: list[str] = []
    for layer, directory in layers:
        for path in workflow_files(directory):
            try:
                workflow = read(path)
            except schema.WorkflowError as exc:
                errors.extend(f"{path}: {error}" for error in exc.errors)
                continue
            found[workflow.name] = Found(layer, path, workflow)
    return found, errors


def resolve(cwd: str | Path | None = None) -> tuple[dict[str, Found], list[str]]:
    return load(layer_dirs(profile_chain.read_state(), cwd))
