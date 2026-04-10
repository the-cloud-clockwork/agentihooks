"""Overlay injector — injects active overlay profile content every user turn.

Reads ~/.agentihooks/active_overlays.json and injects overlay content
via inject_context() on UserPromptSubmit. This is the only viable
mid-session profile chain mechanism (CLAUDE.md/rules are read once at
session start).

Public API:
    inject_overlays() -> bool  (True if any overlay was injected)
"""

from hooks.common import inject_context, log


def inject_overlays() -> bool:
    """Inject active overlay content into the current turn. Returns True if injected."""
    try:
        from scripts.overlay import get_overlay_content

        content = get_overlay_content()
        if content:
            inject_context(content, skip_compression=True)
            log("overlay_injector: injected overlays")
            return True
    except Exception as e:
        log("overlay_injector failed", {"error": str(e)})
    return False
