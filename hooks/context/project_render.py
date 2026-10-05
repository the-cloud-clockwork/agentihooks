from hooks.context.project_memory import ProjectMemory


def render_project_block(memory: ProjectMemory, max_bytes: int) -> str:
    lines = [f"[Project memory: {memory.project}]", "BRAIN CONTEXT (recalled state, not an operator directive):", ""]
    if memory.arcs:
        arc = memory.arcs[0]
        focus = arc.get("summary") or arc["title"]
        active = arc.get("updated") or arc.get("created") or "unknown"
        lines.append(f"Focus: {focus} ({arc.get('status') or 'unknown'}; last active {active}).")
        lines.append("Hot arcs:")
        lines.extend(f"* {arc['title']} [{arc['id']}]" for arc in memory.arcs)
    if memory.lessons:
        lines.append("Recent lessons:")
        lines.extend(f"* {lesson}" for lesson in memory.lessons)
    if not memory.arcs and not memory.lessons:
        lines.append("No project memory yet.")
    return "\n".join(lines).encode()[: max(0, max_bytes)].decode(errors="ignore")
