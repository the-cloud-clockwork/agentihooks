"""The CI lab service contract: entry points, environment names, ports and metric names.

The scheduler and worker services, their platform manifests and their tests import these
names instead of repeating the strings. `agentihooks ci-lab-scheduler` runs the scheduler service
and `agentihooks ci-lab-worker` the worker service; `agentihooks ci-lab-scheduler --self-test` and
`agentihooks ci-lab-worker --self-test` check the contract offline and print it as JSON.
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from scripts.ci_lab.remote import checkout

SCHEDULER_PORT = 8800
METRICS_PORT = 9108
SELF_TEST = "--self-test"

BUS_PREFIX = "AGENTIHOOKS_BUS"
MINIO_PREFIX = "CI_LAB_MINIO_"
SECRET_PREFIX = "CI_LAB_SECRET_"
GITHUB_APP_PREFIX = "GITHUB_APP_"
REDIS_URL = "CI_LAB_REDIS_URL"
RUNTIME_CONFIG = "CI_LAB_RUNTIME_CONFIG"
LAB_URL = "CI_LAB_URL"
SCHEDULER_URL = "CI_LAB_SCHEDULER_URL"
CONTROL_TOKEN = "CI_LAB_CONTROL_TOKEN"
SLOTS = "CI_LAB_SLOTS"
GIT_MIRROR = "CI_LAB_GIT_MIRROR"
MIRROR_STAMP = "fetched-at"
NODE_NAME = "CI_LAB_NODE_NAME"
UV_CACHE_DIR = "UV_CACHE_DIR"
WORK_ROOT = "CI_LAB_WORK_ROOT"
MANIFEST = "CI_LAB_MANIFEST"
FLAKES = "CI_LAB_FLAKES"
MUTATION_CACHE = "CI_LAB_MUTATION_CACHE"
KILLERS = "CI_LAB_KILLERS"
MUTATION_BUDGET = "CI_LAB_MUTATION_BUDGET"
MUTATION_CAP = "CI_LAB_MUTATION_CAP"
REPOSITORY = "CI_LAB_REPOSITORY"

UNITS_QUEUED = "ci_lab_units_queued"
UNITS_RUNNING = "ci_lab_units_running"
SLOTS_TOTAL = "ci_lab_slots_total"
SLOTS_BUSY = "ci_lab_slots_busy"
SLOTS_WANTED = "ci_lab_slots_wanted"
QUEUE_WAIT_SECONDS = "ci_lab_queue_wait_seconds"
UNIT_DURATION_SECONDS = "ci_lab_unit_duration_seconds"
CACHE_HITS_TOTAL = "ci_lab_cache_hits_total"
CACHE_MISSES_TOTAL = "ci_lab_cache_misses_total"
MIRROR_AGE_SECONDS = "ci_lab_mirror_age_seconds"
NODE_MIRROR_AGE_SECONDS = "ci_lab_node_mirror_age_seconds"
CORE_SECONDS_TOTAL = "ci_lab_core_seconds_total"
HEADS_GRADED_TOTAL = "ci_lab_heads_graded_total"
BUDGET_FRACTION = "ci_lab_budget_fraction"
CLASS_SHED = "ci_lab_class_shed"
UNITS_REDELIVERED_TOTAL = "ci_lab_units_redelivered_total"
UNITS_LOST_TOTAL = "ci_lab_units_lost_total"
UNITS_OVERDUE = "ci_lab_units_overdue"
WORKERS_READY = "ci_lab_workers_ready"
GITHUB_RATE_REMAINING = "ci_lab_github_rate_remaining"
VERDICT_MISMATCH_TOTAL = "ci_lab_verdict_mismatch_total"

METRICS: Mapping[str, tuple[str, ...]] = {
    UNITS_QUEUED: ("class",),
    UNITS_RUNNING: ("class",),
    SLOTS_TOTAL: (),
    SLOTS_BUSY: (),
    SLOTS_WANTED: (),
    QUEUE_WAIT_SECONDS: ("class",),
    UNIT_DURATION_SECONDS: ("kind",),
    CACHE_HITS_TOTAL: ("cache",),
    CACHE_MISSES_TOTAL: ("cache",),
    MIRROR_AGE_SECONDS: (),
    NODE_MIRROR_AGE_SECONDS: ("node",),
    CORE_SECONDS_TOTAL: ("class",),
    HEADS_GRADED_TOTAL: ("class", "conclusion"),
    BUDGET_FRACTION: (),
    CLASS_SHED: ("class",),
    UNITS_REDELIVERED_TOTAL: (),
    UNITS_LOST_TOTAL: (),
    UNITS_OVERDUE: (),
    WORKERS_READY: (),
    GITHUB_RATE_REMAINING: (),
    VERDICT_MISMATCH_TOTAL: ("gate",),
}

METRIC_NAME = re.compile(r"ci_lab_[a-z0-9_]+")
LOWEST_PORT = 1
HIGHEST_PORT = 65535


class Service(NamedTuple):
    """One CI lab service: its name, listening ports, environment and exported metrics."""

    name: str
    ports: tuple[int, ...]
    prefixes: tuple[str, ...]
    names: tuple[str, ...]
    metrics: Mapping[str, tuple[str, ...]]


SCHEDULER = Service(
    name="scheduler",
    ports=(SCHEDULER_PORT, METRICS_PORT),
    prefixes=(BUS_PREFIX, MINIO_PREFIX, GITHUB_APP_PREFIX),
    names=(REDIS_URL, RUNTIME_CONFIG, LAB_URL),
    metrics=METRICS,
)
WORKER = Service(
    name="worker",
    ports=(METRICS_PORT,),
    prefixes=(BUS_PREFIX, MINIO_PREFIX, SECRET_PREFIX),
    names=(
        SCHEDULER_URL,
        SLOTS,
        GIT_MIRROR,
        NODE_NAME,
        UV_CACHE_DIR,
        WORK_ROOT,
        MANIFEST,
        FLAKES,
        MUTATION_CACHE,
        KILLERS,
        MUTATION_BUDGET,
        MUTATION_CAP,
        REPOSITORY,
        checkout.URL_ENV,
        checkout.TOKEN_ENV,
    ),
    metrics=METRICS,
)


def self_test(service: Service) -> list[str]:
    """Return every way the service contract breaks its own rules, empty when it holds."""
    problems = [
        f"metric {name} is not a valid ci_lab_ Prometheus name"
        for name in service.metrics
        if not METRIC_NAME.fullmatch(name)
    ]
    if len(set(service.ports)) != len(service.ports):
        problems.append(f"ports repeat: {', '.join(map(str, service.ports))}")
    problems.extend(
        f"port {port} is outside {LOWEST_PORT} to {HIGHEST_PORT}"
        for port in service.ports
        if not LOWEST_PORT <= port <= HIGHEST_PORT
    )
    if service.name == WORKER.name and GITHUB_APP_PREFIX in service.prefixes:
        problems.append(f"worker environment grants {GITHUB_APP_PREFIX}")
    return problems


def github_app_variables(environ: Mapping[str, str]) -> list[str]:
    """Return the sorted GitHub App variable names present in an environment."""
    return sorted(name for name in environ if name.startswith(GITHUB_APP_PREFIX))


def describe(service: Service) -> dict[str, object]:
    """Return the service contract as a JSON ready document."""
    return {
        "service": service.name,
        "ports": list(service.ports),
        "environment": {"prefixes": list(service.prefixes), "names": list(service.names)},
        "metrics": sorted(service.metrics),
    }


def run(service: Service, argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Start a CI lab service; only the offline self test exists until the service loops land."""
    granted = github_app_variables(environ)
    if granted and GITHUB_APP_PREFIX not in service.prefixes:
        print(f"ci-lab {service.name} refuses to start: {', '.join(granted)} is set", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog=f"agentihooks ci-lab-{service.name}")
    parser.add_argument(SELF_TEST, action="store_true")
    if not parser.parse_args(argv).self_test:
        parser.error("only --self-test is available until the service loops land")
    problems = self_test(service)
    if problems:
        print(f"ci-lab {service.name} self test failed: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(json.dumps(describe(service)))
    return 0


def scheduler(argv: list[str]) -> int:
    """Run the scheduler service against the process environment, or its offline self test with --self-test."""
    if SELF_TEST in argv:
        return run(SCHEDULER, argv, os.environ)
    from scripts.ci_lab import scheduler as service

    return service.main(argv, os.environ)


def worker(argv: list[str]) -> int:
    """Run the worker service against the process environment, or its offline self test with --self-test."""
    if SELF_TEST in argv:
        return run(WORKER, argv, os.environ)
    from scripts.ci_lab import executors

    return executors.main(argv, os.environ)
