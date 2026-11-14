def swarm(slug: str, *rest: str) -> list[str]:
    return ["agentihooks", "swarm", slug, *rest]


def ledger(slug: str, who: str, *rest: str) -> list[str]:
    return ["agentihooks", "ledger", "--slug", slug, "--as", who, *rest]


def stop_work(slug: str, who: str, task: str) -> list[list[str]]:
    return [
        # ruleid: feature-launch-troubleshooting-crew
        swarm(slug, "stop"),
        # ruleid: feature-launch-troubleshooting-crew
        ledger(slug, who, "delete", f"tasks/{task}"),
        # ruleid: feature-launch-troubleshooting-crew
        ["agentihooks", "swarm", slug, "merge", "priority", task, "--lift"],
    ]


def move_focus(slug: str, task: str, quote: str) -> list[list[str]]:
    return [
        # ok: feature-launch-troubleshooting-crew
        swarm(slug, "focus", f"tasks/{task}", "--only", "--quote", quote),
        # ok: feature-launch-troubleshooting-crew
        swarm(slug, "unfreeze", f"tasks/{task}", "--quote", quote),
    ]
