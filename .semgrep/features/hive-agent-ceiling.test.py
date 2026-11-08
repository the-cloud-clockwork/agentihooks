# ruleid: feature-hive-agent-ceiling
def loaded_hives(store):
    for hive in hives:
        reading = headroom.read(store.redis, hive["id"], now)
        found = admission.capped(admission.room(reading, pending=held), used, hive["max_agents"])
    return hives


# ok: feature-hive-agent-ceiling
def loaded_hives(store):
    for hive in hives:
        reading = headroom.read(store.redis, hive["id"], now)
        hive["cap"] = hive["max_agents"]
        hive["max_agents"] = ceiling.ceiling(hive["cap"], reading)
        found = admission.capped(admission.room(reading, pending=held), used, hive["max_agents"])
    return hives


# ruleid: feature-hive-agent-ceiling
def ceiling(cap, measured):
    found = computed(measured)
    if found is None:
        return cap or 0
    return 20


# ok: feature-hive-agent-ceiling
def ceiling(cap, measured):
    found = computed(measured)
    if found is None:
        return cap or 0
    return found if cap is None else min(cap, found)
