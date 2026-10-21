import re

REARM = r"\b[Rr]e[- ]?arm (?:your |a |an |the )?`?Monitors?`?(?: on (?:the )?ledger)?"
MONITOR = r"`?\bMonitors?\b`?"


def waiting(slug: str, push: bool = False) -> str:
    if push:
        return (
            "Read agentihooks msg inbox and handle every open item. When no work remains, end your turn: new inbox "
            f"items arrive in this thread on their own, so never run agentihooks swarm {slug} wait --inbox, even where "
            "your profile instructions describe that loop. "
            "Use swarm wait --on checks, reply or task for a specific dependency."
        )
    return (
        "Read agentihooks msg inbox and handle every open item. "
        f"When no work remains, run agentihooks swarm {slug} wait --inbox in a foreground tool call. "
        "It returns pending inbox work or times out after one minute; read the inbox and wait again. "
        "Keep the tool call active while waiting so new work resumes this turn without pane input. "
        "Use swarm wait --on checks, reply or task for a specific dependency."
    )


def next_action(text: str, slug: str, push: bool = False) -> str:
    if push:
        return re.sub(REARM, "Read agentihooks msg inbox", text)
    text = re.sub(REARM, f"Read agentihooks msg inbox and run agentihooks swarm {slug} wait --inbox", text)
    return re.sub(MONITOR, f"`agentihooks swarm {slug} wait --inbox`", text)


def persona(text: str) -> str:
    text = text.replace("Monitor its checks", "Watch its checks")
    return next_action(text, "<slug>") + "\n" + waiting("<slug>") + "\n"


def handoff(task: dict, slug: str, push: bool = False) -> dict:
    result = dict(task)
    if "handoff" in task:
        lines, active, fenced = [], False, False
        for line in task["handoff"].splitlines(keepends=True):
            if line.lstrip().startswith("```"):
                fenced = not fenced
            if not fenced and line.startswith("## "):
                active = line[3:].strip() == "Next"
            elif active:
                line = next_action(line, slug, push)
            lines.append(line)
        result["handoff"] = "".join(lines)
    if transfer := task.get("transfer"):
        result["transfer"] = {**transfer, "next": next_action(transfer["next"], slug, push)}
    return result
