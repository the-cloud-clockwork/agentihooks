from typing import Any

from nats.js.errors import ServerError


class Unsafe:
    def _withdrawn(self, sequence: int, stale: Any) -> bool:
        self: Any
        msg = self._loop.run(self._get(sequence))
        if msg is None or not stale(msg):
            return False
        # ruleid: nats-withdraw-unguarded-delete
        self._loop.run(self._js.delete_msg(self._stream, sequence))
        return True


class Safe:
    def _withdrawn(self, sequence: int, stale: Any) -> bool:
        self: Any
        msg = self._loop.run(self._get(sequence))
        if msg is None or not stale(msg):
            return False
        try:
            # ok: nats-withdraw-unguarded-delete
            self._loop.run(self._js.delete_msg(self._stream, sequence))
        except ServerError as error:
            if error.err_code != 10057:
                raise
            return False
        return True
