from scripts import claude_quota_balancer as balancer
from scripts.swarm import capacity


class HiveLaunch:
    def _select(self, environ: dict, now: float) -> list:
        # ruleid: controller-local-quota-fallback
        return capacity.accounts(environ, now, refresh=False)

    def cached(self) -> list:
        # ruleid: controller-local-quota-fallback
        return balancer.cached_observations()

    def runtime(self, environ: dict, now: float) -> list:
        # ok: controller-local-quota-fallback
        return self.herdr.quota_accounts(environ, now, refresh=False)

    def reports(self, store: object, environ: dict, now: float) -> list:
        # ok: controller-local-quota-fallback
        return capacity.hive_accounts(store)(environ, now, refresh=False)


def _rotation(environ: dict, now: float) -> list:
    # ruleid: controller-local-quota-fallback
    read = capacity.accounts
    return read(environ, now, refresh=False)


def hive_controller(runtime: object, store: object) -> object:
    # ruleid: controller-local-quota-fallback
    runtime.quota_accounts = capacity.accounts
    return runtime


def local_cli(environ: dict, now: float) -> list:
    # ok: controller-local-quota-fallback
    return capacity.accounts(environ, now, refresh=False)
