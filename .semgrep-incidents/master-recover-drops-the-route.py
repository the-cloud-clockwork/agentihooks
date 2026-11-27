from dataclasses import replace
from typing import Any


def dropped(store: Any, runtime: Any, tick: Any, name: str) -> None:
    record = store.execution_occupants("sw").get("master@sw")
    # ruleid: master-recover-drops-the-route
    store.put_agent("sw", tick.placed_record(record, runtime.recover(name)))


def partly(store: Any, runtime: Any, tick: Any, name: str) -> None:
    record = store.execution_occupants("sw").get("master@sw")
    # ruleid: master-recover-drops-the-route
    store.put_agent("sw", replace(tick.placed_record(record, runtime.recover(name)), account=record.account))


def kept(store: Any, runtime: Any, tick: Any, name: str) -> None:
    record = store.execution_occupants("sw").get("master@sw")
    adopted = replace(
        # ok: master-recover-drops-the-route
        tick.placed_record(record, runtime.recover(name)),
        account=record.account,
        placement=record.placement,
        route_kind=record.route_kind,
    )
    store.put_agent("sw", adopted)


def fresh(store: Any, runtime: Any, tick: Any, bare: Any, name: str) -> None:
    store.execution_occupants("sw").get("master@sw")
    # ok: master-recover-drops-the-route
    store.put_agent("sw", tick.placed_record(bare, runtime.recover(name)))
