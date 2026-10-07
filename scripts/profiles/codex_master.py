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
    return waiting(slug) if re.search(r"\bMonitors?\b", text) else text


def persona(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if "Monitor its checks" in line:
            line = line.replace("Monitor its checks", "Watch its checks")
        elif re.search(r"\bMonitors?\b", line):
            if line.startswith("|"):
                cells = line.split("|")
                cells[2] = f" {waiting('<slug>')} "
                line = "|".join(cells)
            elif line.startswith("#"):
                line = re.sub(r"\bMonitor\b", "Process wait", line)
            else:
                prefix = "- " if line.startswith("- ") else ""
                line = prefix + waiting("<slug>") + "\n"
        lines.append(line)
    return "".join(lines) + "\n" + waiting("<slug>") + "\n"


def handoff(task: dict, slug: str) -> dict:
    result = dict(task)
    text = task.get("handoff", "")
    before, heading, rest = text.partition("## Next\n")
    section, after, tail = rest.partition("\n## ")
    if heading:
        section = "\n".join(next_action(line, slug) for line in section.splitlines())
        result["handoff"] = before + heading + section + after + tail
    if transfer := task.get("transfer"):
        result["transfer"] = {**transfer, "next": next_action(transfer["next"], slug)}
    return result
