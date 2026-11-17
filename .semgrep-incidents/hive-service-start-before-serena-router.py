import os

from scripts import serena_router_daemon
from scripts.hive import lanes


def ungated_service_start() -> None:
    # ruleid: hive-service-start-before-serena-router
    lanes.serve(os.environ)


def gated_service_start() -> None:
    serena_router_daemon.ensure()
    # ok: hive-service-start-before-serena-router
    lanes.serve(os.environ)
