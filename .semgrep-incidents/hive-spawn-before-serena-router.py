from scripts import serena_router_daemon


class Spoke:
    def _spawn(self, request: object) -> object:
        # ruleid: hive-spawn-before-serena-router
        return self.runtime.spawn(request)


class GatedSpoke:
    def _spawn(self, request: object) -> object:
        if not serena_router_daemon.ensure():
            raise RuntimeError("spawn unavailable: serena")
        # ok: hive-spawn-before-serena-router
        return self.runtime.spawn(request)
