from scripts.swarm_v2.herdr import hive_lifecycle


class Service:
    # ruleid: operator-sync-without-hive-lifecycle
    def run_pass(self) -> object:
        return self.upkeep.change(self.attempts())


class Service:
    # ok: operator-sync-without-hive-lifecycle
    def run_pass(self) -> object:
        hive_lifecycle.reconcile(self.redis, self.known_hosts, self.run, self.now())
        return self.upkeep.change(self.attempts())
