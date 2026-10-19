import pytest

from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.diagnostics import LOG_READS, startup
from tests import sv2_kub04_cases as cases
from tests.test_swarm_v2_kubernetes_cleanup import ready as ready
from tests.test_swarm_v2_kubernetes_diagnostics import refused

EXIT = {"exitCode": 78, "reason": "Error", "message": "startup refused"}


@pytest.mark.parametrize(
    "error", [ApiRefused(400, "BadRequest"), ConnectionError("logs unavailable"), TimeoutError("timed out")]
)
def test_unservable_logs_remove_the_refused_pod_after_the_retry_limit_with_its_exit_reason(ready, error):
    world, controller, old, _ = ready
    name = refused(world, old)
    reads = []

    def unservable(pod, container):
        reads.append((pod, container))
        raise error

    world.api.pod_logs = unservable
    for attempt in range(1, LOG_READS):
        with pytest.raises(type(error)):
            world.cleanup(controller).run(old.execution_id)
        assert name in world.api.objects
        assert len(reads) == attempt
        assert retention.retained(world.store, cases.SLUG, old.execution_id) is None
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert name not in world.api.objects
    assert len(reads) == LOG_READS
    removed = retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]
    assert removed["outcome"] == "deleted"
    assert removed["log_reads"] == LOG_READS
    assert removed["startup"] == {
        "phase": "Failed",
        "reason": "Error",
        "containers": [{"name": "agent", "terminated": EXIT}],
    }


def test_servable_logs_after_a_failed_read_keep_the_log(ready):
    world, controller, old, _ = ready
    name = refused(world, old)
    answers = [ConnectionError("logs unavailable"), "worker startup refused\n"]

    def flaky(pod, container):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    world.api.pod_logs = flaky
    with pytest.raises(ConnectionError):
        world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert name not in world.api.objects
    removed = retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]
    assert removed["log_reads"] == 1
    assert removed["startup"]["containers"] == [
        {"name": "agent", "terminated": EXIT, "log": "worker startup refused\n"}
    ]


def test_servable_logs_on_the_first_read_remove_the_pod_in_one_pass_without_a_retry_count(ready):
    world, controller, old, _ = ready
    name = refused(world, old)
    world.api.pod_logs = lambda pod, container: "worker startup refused\n"
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert name not in world.api.objects
    removed = retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]
    assert "log_reads" not in removed
    assert removed["startup"]["containers"][0]["log"] == "worker startup refused\n"


def test_logs_failing_outside_the_api_errors_are_not_counted(ready):
    world, controller, old, _ = ready
    name = refused(world, old)

    def broken(pod, container):
        raise KeyError("unexpected")

    world.api.pod_logs = broken
    for _ in range(LOG_READS):
        with pytest.raises(KeyError):
            world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects


def test_exit_reason_only_capture_reads_no_log():
    pod = {
        "metadata": {"name": "refused"},
        "status": {"phase": "Failed", "containerStatuses": [{"name": "agent", "state": {"terminated": EXIT}}]},
    }
    assert startup(pod) == {"phase": "Failed", "reason": "", "containers": [{"name": "agent", "terminated": EXIT}]}
