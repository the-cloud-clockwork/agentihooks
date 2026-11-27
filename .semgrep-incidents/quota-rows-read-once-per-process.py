import time

from scripts.swarm import capacity


class HerdrRuntime:
    quota_accounts = None

    def _quota_transfer(self, environ: dict) -> list:
        if not hasattr(self, "_quota_accounts"):
            read = self.quota_accounts or capacity.accounts
            # ruleid: quota-rows-read-once-per-process
            self._quota_accounts = read(environ, time.time(), refresh=False)
        return self._quota_accounts

    def _rotation(self, requested: str, environ: dict) -> list:
        if not requested and not hasattr(self, "_quota_accounts") and self.quota_accounts is not None:
            # ruleid: quota-rows-read-once-per-process
            self._quota_accounts = self.quota_accounts(environ, time.time(), refresh=False)
        return self._quota_accounts

    def _quota_read(self, environ: dict) -> None:
        read = self.quota_accounts or capacity.accounts
        # ok: quota-rows-read-once-per-process
        self._quota_accounts = read(environ, time.time(), refresh=False)

    def has_capacity(self, environ: dict) -> bool:
        if self.quota_accounts is not None:
            # ok: quota-rows-read-once-per-process
            self._quota_accounts = self.quota_accounts(environ, time.time(), refresh=False)
        return hasattr(self, "_quota_accounts")

    def _quota_lazy(self, environ: dict) -> list:
        if not hasattr(self, "_quota_accounts"):
            # ok: quota-rows-read-once-per-process
            self._quota_read(environ)
        return self._quota_accounts
