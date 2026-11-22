# ruleid: feature-expert-seat-recovery
def _owns(store, slug, agent, ledger):
    if agent not in store.agents(slug):
        return False
    if agent.lane == MASTER:
        return True
    task = ledger.task(slug, agent.task) or {}
    return task.get("claimed_by") == agent.name


# ok: feature-expert-seat-recovery
def _owns(store, slug, agent, ledger):
    if agent not in store.agents(slug):
        return False
    if agent.lane == MASTER:
        return True
    if agent.lane == expert_launch.LANE:
        return expert_lineage.runs(store, slug, agent.task)
    task = ledger.task(slug, agent.task) or {}
    return task.get("claimed_by") == agent.name


# ruleid: feature-expert-seat-recovery
def _owns(store, slug, agent, ledger):
    if agent.lane == expert_launch.LANE:
        return True
    task = ledger.task(slug, agent.task) or {}
    return task.get("claimed_by") == agent.name


# ok: feature-expert-seat-recovery
def _reason(store, slug, agent):
    return ""
