class FleetAccounts:
    def raw(self, environ: dict[str, str], now: float, refresh: bool) -> list:
        # ruleid: fleet-accounts-without-master-reservation
        return self.route.banded_rows(self.cap, now)

    def filtered(self, environ: dict[str, str], now: float, refresh: bool) -> list:
        # ok: fleet-accounts-without-master-reservation
        return capacity.reserved_accounts(self.route.banded_rows(self.cap, now), environ)
