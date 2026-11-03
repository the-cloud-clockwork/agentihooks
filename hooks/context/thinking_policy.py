"""Thinking/effort policy — injects model usage guidance at session start.

Reads DEFAULT_EFFORT and THINKING_BUDGET_TOKENS from config to generate
an advisory message that helps control decode token spend.
"""

from typing import Optional


def get_thinking_guidance(default_effort: str, budget: int) -> str:
    """Build session-start injection text for thinking/effort policy.

    Args:
        default_effort: "low", "medium", or "high"
        budget: advisory thinking token ceiling (0 = no limit)

    Returns:
        Guidance string for injection, or empty string if disabled/default-high.
    """
    if default_effort == "high" and budget <= 0:
        return ""

    parts = []

    effort_guidance = {
        "low": (
            "Default effort: low. Use minimal reasoning for straightforward tasks. "
            "Only escalate to medium/high for complex architectural decisions."
        ),
        "medium": (
            "Default effort: medium. Use standard reasoning depth. "
            "Reserve high/ultrathink for complex architectural decisions or debugging."
        ),
        "high": ("Default effort: high. Full reasoning enabled."),
    }
    parts.append(effort_guidance.get(default_effort, effort_guidance["medium"]))

    from hooks.targets import current_target

    if default_effort == "medium" and current_target() == "claude":
        parts.append("Prefer Sonnet for implementation; reserve Opus for planning.")

    if budget > 0:
        parts.append(f"Advisory thinking budget: {budget:,} tokens per response.")

    return " ".join(parts)


def check_subagent_effort(tool_input: dict, default_effort: str) -> Optional[str]:
    """Check if a spawned Agent uses unnecessarily expensive settings.

    Args:
        tool_input: The Agent tool's input dict
        default_effort: The profile's default effort level

    Returns:
        Warning string if misaligned, None otherwise.
    """
    if default_effort == "high":
        return None

    model = (tool_input.get("model") or "").lower()
    if model == "opus" and default_effort in ("low", "medium"):
        subagent_type = tool_input.get("subagent_type", "general-purpose")
        if subagent_type in ("Explore", "Plan", "general-purpose"):
            return (
                f"Subagent spawned with model=opus (profile default: {default_effort}). "
                "Consider using sonnet for this agent type to reduce token spend."
            )

    return None
