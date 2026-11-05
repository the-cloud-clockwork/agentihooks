PARK_MINUTES, GRACE_MINUTES = 150, 15
# ruleid: feature-overrun-no-wall-time-handoff
ASK = "Overrun on swarm {slug}: you have run {minutes} minutes on task {task}, past the {park} minute limit. Hand off."


def _step(watch, mail, agent, mark):
    minutes = _minutes(watch, agent.started_at)
    if minutes >= REPORT_MINUTES and not mark.get("reported"):
        mark["reported"] = watch.now_ms
        return [_report(watch, mail, agent, minutes)]
    if minutes >= PARK_MINUTES:
        mark["asked"] = watch.now_ms
        # ruleid: feature-overrun-no-wall-time-handoff
        watch.retire(agent)
        return []
    return []


# ruleid: feature-overrun-no-wall-time-handoff
ENDED = "Overrun on swarm {slug}: {agent} was ended {grace} minutes after the ask to hand off task {task}."


def _step_clean(watch, mail, agent, mark):
    minutes = _minutes(watch, agent.started_at)
    if minutes >= REPORT_MINUTES and not mark.get("reported"):
        mark["reported"] = watch.now_ms
        return [_report(watch, mail, agent, minutes)]
    return []


# ok: feature-overrun-no-wall-time-handoff
REPORT = "Overrun on swarm {slug}: {agent} has run {minutes} minutes on task {task} without finishing it."
