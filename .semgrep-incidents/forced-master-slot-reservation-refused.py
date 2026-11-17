from typing import Any, Protocol


class Launch(Protocol):
    def send_order(self, request: Any, account: Any, execution: str) -> str: ...


class RefusesForced(Launch):
    def send_order(self, request: Any, account: Any, execution: str) -> str:
        reading = self.slots.reserve_reading(account.name, execution, account.cap, 1000)
        # ruleid: forced-master-slot-reservation-refused
        if not reading.accepted:
            return "account reservation is full"
        return "sent"


class PassesForced(Launch):
    def send_order(self, request: Any, account: Any, execution: str) -> str:
        reading = self.slots.reserve_reading(account.name, execution, account.cap, 1000)
        # ok: forced-master-slot-reservation-refused
        if not reading.accepted and not request.task.get("forced"):
            return "account reservation is full"
        return "sent"


class OtherStep:
    def reserve_room(self, request: Any, account: Any, execution: str) -> str:
        reading = self.slots.reserve_reading(account.name, execution, account.cap, 1000)
        # ok: forced-master-slot-reservation-refused
        if not reading.accepted:
            return "account reservation is full"
        return "sent"
