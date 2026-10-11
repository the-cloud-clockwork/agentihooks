import pytest

from scripts.swarm.host_budget import Room
from scripts.swarm_v2.kubernetes import pool
from scripts.swarm_v2.kubernetes.client import ApiRefused

pytestmark = pytest.mark.unit

POOL = {"pods": 8, "cpu_millis": 18000, "memory_mib": 30000}
LAUNCH = {"memory_mib": 4096, "cpu_millis": 2000, "ephemeral_mib": 10240}


def _pod(phase="Running", cpu="2", memory="4Gi", init=None):
    spec = {"containers": [{"name": "agent", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if init is not None:
        spec["initContainers"] = [{"name": "bundle", "resources": {"requests": init}}]
    return {"metadata": {"name": "p"}, "spec": spec, "status": {"phase": phase}}


class Listing:
    def __init__(self, pods=(), error=None):
        self.pods, self.error, self.selectors = list(pods), error, []

    def list_pods(self, selector):
        self.selectors.append(selector)
        if self.error is not None:
            raise self.error
        return self.pods


@pytest.mark.parametrize(
    ("quantity", "millis"),
    [
        ("2", 2000),
        ("500m", 500),
        ("1.5", 1500),
        ("0.1", 100),
        (".5", 500),
        ("250001u", 251),
        ("1n", 1),
        ("1000001n", 2),
        ("1k", 1_000_000),
        ("1M", 1_000_000_000),
        ("1e3", 1_000_000),
        ("1E3", 1_000_000),
        ("15e-2", 150),
        ("2e+0", 2000),
    ],
)
def test_a_cpu_quantity_reads_as_whole_millicores_rounded_up(quantity, millis):
    assert pool.cpu_millis(quantity) == millis


@pytest.mark.parametrize(
    ("quantity", "mib"),
    [
        ("4Gi", 4096),
        ("64Mi", 64),
        ("1536Mi", 1536),
        ("1048576", 1),
        ("1048577", 2),
        ("1024Ki", 1),
        ("1025Ki", 2),
        ("1G", 954),
        ("1T", 953675),
        ("1P", 953674317),
        ("1E", 953674316407),
        ("1Ti", 1048576),
        ("1Pi", 1073741824),
        ("1Ei", 1099511627776),
    ],
)
def test_a_memory_quantity_reads_as_whole_mebibytes_rounded_up(quantity, mib):
    assert pool.memory_mib(quantity) == mib


@pytest.mark.parametrize("quantity", ["", "two", "4Qi", "-1", "1e", "1Mie3", ".", "1e999999999"])
def test_a_quantity_that_is_not_a_kubernetes_quantity_is_refused(quantity):
    with pytest.raises(ValueError) as refused:
        pool.cpu_millis(quantity)
    assert str(refused.value) == f"not a resource quantity: {quantity!r}"


def test_a_pod_requests_the_larger_of_its_containers_and_its_largest_init_container_per_resource():
    pod = _pod(cpu="100m", memory="4Gi", init={"cpu": "500m", "memory": "64Mi"})
    pod["spec"]["containers"].append({"name": "side", "resources": {"requests": {"cpu": "100m"}}})

    assert pool.requests(pod) == (500, 4096)


def test_a_container_with_limits_and_no_requests_requests_its_limits():
    limited = {"name": "agent", "resources": {"limits": {"cpu": "1", "memory": "2Gi"}}}
    partial = {"name": "side", "resources": {"requests": {"cpu": "100m"}, "limits": {"cpu": "1", "memory": "64Mi"}}}

    assert pool.requests({"spec": {"containers": [limited, partial]}}) == (1100, 2112)


def test_a_container_without_requests_adds_nothing():
    pod = {"spec": {"containers": [{"name": "agent"}, {"name": "side", "resources": {}}]}}

    assert pool.requests(pod) == (0, 0)


def test_usage_counts_live_pods_and_skips_finished_ones():
    pods = [_pod(), _pod("Pending"), _pod("Succeeded"), _pod("Failed"), {**_pod(), "status": {}}]

    assert pool.usage(pods) == pool.Usage(3, 6000, 12288)


def test_room_is_the_fewest_launches_that_fit_and_names_memory_when_memory_binds():
    found = pool.pool_room(POOL, LAUNCH, pool.Usage(2, 4000, 8192))

    assert found == Room(
        5,
        "worker pool: 2 of 8 Pods, 4000m of 18000m CPU and 8192 MiB of 30000 MiB memory requested; "
        "5 more fit at 2000m and 4096 MiB each",
        "memory",
    )


def test_room_names_cpu_when_cpu_binds():
    assert pool.pool_room(POOL, LAUNCH, pool.Usage(2, 14000, 0)) == Room(
        2,
        "worker pool: 2 of 8 Pods, 14000m of 18000m CPU and 0 MiB of 30000 MiB memory requested; "
        "2 more fit at 2000m and 4096 MiB each",
        "cpu",
    )


def test_room_names_pods_when_the_pod_count_binds_and_on_a_tie():
    assert pool.pool_room(POOL, LAUNCH, pool.Usage(7, 0, 0)) == Room(
        1,
        "worker pool: 7 of 8 Pods, 0m of 18000m CPU and 0 MiB of 30000 MiB memory requested; "
        "1 more fit at 2000m and 4096 MiB each",
        "pods",
    )
    assert pool.pool_room({**POOL, "memory_mib": 24576}, LAUNCH, pool.Usage(2, 0, 0)) == Room(
        6,
        "worker pool: 2 of 8 Pods, 0m of 18000m CPU and 0 MiB of 24576 MiB memory requested; "
        "6 more fit at 2000m and 4096 MiB each",
        "pods",
    )


def test_room_exactly_full_is_zero():
    assert pool.pool_room(POOL, LAUNCH, pool.Usage(8, 0, 0)) == Room(
        0,
        "worker pool: 8 of 8 Pods, 0m of 18000m CPU and 0 MiB of 30000 MiB memory requested; "
        "0 more fit at 2000m and 4096 MiB each",
        "pods",
    )
    assert pool.pool_room(POOL, LAUNCH, pool.Usage(0, 18000, 0)) == Room(
        0,
        "worker pool: 0 of 8 Pods, 18000m of 18000m CPU and 0 MiB of 30000 MiB memory requested; "
        "0 more fit at 2000m and 4096 MiB each",
        "cpu",
    )


def test_room_over_the_pool_never_drops_below_zero():
    assert pool.pool_room(POOL, LAUNCH, pool.Usage(9, 20000, 40000)) == Room(
        0,
        "worker pool: 9 of 8 Pods, 20000m of 18000m CPU and 40000 MiB of 30000 MiB memory requested; "
        "0 more fit at 2000m and 4096 MiB each",
        "memory",
    )


def test_the_reading_lists_every_pod_in_the_worker_namespace_and_sizes_the_room():
    listing = Listing([_pod(), _pod(), _pod("Succeeded")])

    found = pool.WorkerPool(listing, POOL, LAUNCH)()

    assert listing.selectors == [""]
    assert (found.room, found.limit) == (5, "memory")


def test_an_undeclared_pool_lists_nothing_and_has_unknown_room():
    listing = Listing([_pod()])

    found = pool.WorkerPool(listing, None, LAUNCH)()

    assert listing.selectors == []
    assert found == Room(None, "worker pool undeclared: the Pod policy names no worker_pool, so spawns pass", "unknown")


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (ApiRefused(403, "Forbidden"), "ApiRefused"),
        (ConnectionError("down"), "ConnectionError"),
        (TimeoutError("slow"), "TimeoutError"),
    ],
)
def test_a_pod_list_that_fails_has_unknown_room_and_names_the_failure(error, name):
    found = pool.WorkerPool(Listing(error=error), POOL, LAUNCH)()

    assert found == Room(
        None, f"worker pool unknown: the worker Pods cannot be listed ({name}), so spawns pass", "unknown"
    )


def test_a_pod_request_that_is_not_a_quantity_has_unknown_room():
    found = pool.WorkerPool(Listing([_pod(cpu="lots")]), POOL, LAUNCH)()

    assert found == Room(
        None,
        "worker pool unknown: a worker Pod request is not a resource quantity: 'lots', so spawns pass",
        "unknown",
    )
