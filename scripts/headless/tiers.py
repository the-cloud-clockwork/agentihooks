from collections.abc import Mapping

DEFAULTS = {
    ("claude", "high"): "opus",
    ("claude", "low"): "sonnet",
    ("codex", "high"): "gpt-6.1-sol",
    ("codex", "low"): "gpt-6-luna",
}


def model(harness: str, tier: str, environ: Mapping[str, str]) -> str:
    if (harness, tier) not in DEFAULTS:
        raise ValueError(f"no {tier} tier for {harness}")
    return environ.get(f"AGENTIHOOKS_TIER_{tier.upper()}_{harness.upper()}") or DEFAULTS[(harness, tier)]
