def poll(self, now):
    for swarm, hive in self.rows():
        # ruleid: hive-loss-without-engine-filter
        if presence.lost(self.bus.for_swarm(swarm), hive, now):
            self.exit(swarm, hive)


def poll(self, now):
    events = set(event_slugs(self.store))
    for swarm, hive in self.rows():
        # ok: hive-loss-without-engine-filter
        if swarm in events and presence.lost(self.bus.for_swarm(swarm), hive, now):
            self.exit(swarm, hive)


def poll(self, now):
    for swarm, hive in self.rows():
        if self.store.config(swarm).engine != EVENTS_ENGINE:
            continue
        # ok: hive-loss-without-engine-filter
        if presence.lost(self.bus.for_swarm(swarm), hive, now):
            self.exit(swarm, hive)


def ending(bus, hive, now):
    # ok: hive-loss-without-engine-filter
    return presence.lost(bus, hive, now)
