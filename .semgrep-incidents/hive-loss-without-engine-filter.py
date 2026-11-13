class Unfiltered:
    def poll(self, now):
        for swarm, hive in self.rows():
            # ruleid: hive-loss-without-engine-filter
            if presence.lost(self.bus.for_swarm(swarm), hive, now):
                self.exit(swarm, hive)


class EventsReadButUnused:
    def poll(self, now):
        events = self._events()
        for swarm, hive in self.rows():
            # ruleid: hive-loss-without-engine-filter
            lost = now - self.watching >= presence.TTL_MS and presence.lost(self.bus.for_swarm(swarm), hive, now)

    def _events(self):
        return set(event_slugs(self.store))


class GuardedByEventsMembership:
    def poll(self, now):
        events = self._events()
        for swarm, hive in self.rows():
            lost = (
                swarm in events
                and now - self.watching >= presence.TTL_MS
                # ok: hive-loss-without-engine-filter
                and presence.lost(self.bus.for_swarm(swarm), hive, now)
            )


class SkipsOtherEngines:
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
