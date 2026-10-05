from graphlib import CycleError, TopologicalSorter

from scripts.swarm.store import SwarmError


def _width(nodes, ancestors):
    matching = {}

    def augment(node, seen):
        for other in sorted(ancestors[node] & nodes):
            if other in seen:
                continue
            seen.add(other)
            if other not in matching or augment(matching[other], seen):
                matching[other] = node
                return True
        return False

    return len(nodes) - sum(augment(node, set()) for node in sorted(nodes))


def analyze(tasks: list[dict]) -> dict:
    active = {t["id"]: t for t in tasks if not t.get("out_of_scope")}
    known = {t["id"] for t in tasks}
    graph = {key: set(t.get("depends_on", [])) & active.keys() for key, t in active.items()}
    unknown = {dep for t in active.values() for dep in t.get("depends_on", [])} - known
    if unknown:
        raise SwarmError(f"plan has unknown dependencies: {', '.join(sorted(unknown))}")
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise SwarmError("plan dependencies contain a cycle") from exc
    paths, ancestors = {}, {}
    for key in order:
        deps = sorted(graph[key])
        paths[key] = max((paths[dep] for dep in deps), key=len, default=[]) + [key]
        ancestors[key] = set(deps).union(*(ancestors[dep] for dep in deps))
    path = max(paths.values(), key=len, default=[])
    engineers = {key for key, t in active.items() if t.get("lane", "eng") == "eng"}
    return {
        "critical_path": path,
        "chain_length": len(path),
        "parallel_width": _width(set(active), ancestors),
        "engineer_width": _width(engineers, ancestors),
    }


def report(tasks: list[dict], engineer_cap: int) -> dict:
    shape = analyze(tasks)
    chain = " -> ".join(shape["critical_path"]) or "none"
    summary = (
        f"Critical path: {shape['chain_length']} tasks ({chain})\n"
        f"Parallel width: {shape['parallel_width']} tasks; engineer width: {shape['engineer_width']}\n"
        "Dependency limits for the whole plan; territories and agent caps may reduce concurrency."
    )
    warning = ""
    if shape["engineer_width"] < engineer_cap:
        warning = f"engineer width {shape['engineer_width']} is below engineer cap {engineer_cap}"
    return {**shape, "summary": summary, "warning": warning}
