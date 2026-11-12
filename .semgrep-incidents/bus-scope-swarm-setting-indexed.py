import os


def scoped_state(bus, bucket):
    # ruleid: bus-scope-swarm-setting-indexed
    return bus.for_swarm(os.environ["AGENTIHOOKS_SWARM"]).state(bucket)


def scoped_from(bus, environ, bucket):
    # ruleid: bus-scope-swarm-setting-indexed
    return bus.for_swarm(environ["AGENTIHOOKS_SWARM"]).state(bucket)


def defined_scope(bus, bucket):
    # ok: bus-scope-swarm-setting-indexed
    swarm = os.environ.get("AGENTIHOOKS_SWARM")
    return (bus.for_swarm(swarm) if swarm else bus).state(bucket)
