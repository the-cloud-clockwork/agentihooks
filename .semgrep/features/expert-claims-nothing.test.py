# ruleid: feature-expert-claims-nothing
def _worker(store, args):
    agent = _me(store, args)
    if agent.lane == MASTER:
        raise SwarmError(f"{agent.name} is the swarm master; the master works no task")
    return agent


# ok: feature-expert-claims-nothing
def _worker(store, args):
    agent = _me(store, args)
    if agent.lane == MASTER:
        raise SwarmError(f"{agent.name} is the swarm master; the master works no task")
    if agent.lane == expert_launch.LANE:
        raise SwarmError(f"{agent.name} is an expert; an expert works its mission and claims no task")
    return agent


# ruleid: feature-expert-claims-nothing
def _worker(store, args):
    agent = _me(store, args)
    if agent.lane == expert_launch.LANE:
        log(agent)
    return agent


# ruleid: feature-expert-claims-nothing
def _worker(store, args):
    agent = _me(store, args)
    if agent.lane == MASTER:
        raise SwarmError(f"{agent.name} is the swarm master; the master works no task")
    return agent if agent.lane != expert_launch.LANE else None


# ok: feature-expert-claims-nothing
def _claimant(store, args):
    return _me(store, args)
