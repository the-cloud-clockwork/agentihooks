import re


def waiting(slug: str) -> str:
    return (
        "Read agentihooks msg inbox and handle every open item. "
        f"When no work remains, run agentihooks swarm {slug} wait --inbox in a foreground tool call. "
        "It returns pending inbox work or times out after one minute; read the inbox and wait again. "
        "Keep the tool call active while waiting so new work resumes this turn without pane input. "
        "Use swarm wait --on checks, reply or task for a specific dependency."
    )


def next_action(text: str, slug: str) -> str:
    text = re.sub(
        r"\b[Rr]e[- ]?arm (?:your |a |an |the )?`?Monitors?`?(?: on (?:the )?ledger)?",
        f"Read agentihooks msg inbox and run agentihooks swarm {slug} wait --inbox",
        text,
    )
    return re.sub(r"`?\bMonitors?\b`?", f"`agentihooks swarm {slug} wait --inbox`", text)


def persona(text: str) -> str:
    text = text.replace("Monitor its checks", "Watch its checks")
    return next_action(text, "<slug>") + "\n" + waiting("<slug>") + "\n"


def handoff(task: dict, slug: str) -> dict:
    result = dict(task)
    lines, active, fenced = [], False, False
    for line in task.get("handoff", "").splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            active = line[3:].strip() == "Next"
        elif active:
            line = next_action(line, slug)
        lines.append(line)
    if "handoff" in task:
        result["handoff"] = "".join(lines)
    if transfer := task.get("transfer"):
        result["transfer"] = {**transfer, "next": next_action(transfer["next"], slug)}
    return result
