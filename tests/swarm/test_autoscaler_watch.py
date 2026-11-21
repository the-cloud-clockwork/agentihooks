"""The autoscaler watch counts each node group's failed scale ups in a row from the cluster autoscaler status and
alerts the masters on two in a row, a timed out scale up or a launched instance past its join budget."""

import json
import os
from collections.abc import Callable
from typing import Any

import pytest

from scripts.swarm import autoscaler_watch
from scripts.swarm.store import RedisStore

NAME = "kube-system/cluster-autoscaler-oci-status"
POOL = "ocid1.instancepool.oc1.eu-frankfurt-1.ci"
OTHER = "ocid1.instancepool.oc1.eu-frankfurt-1.research"
EARLY, LATE, LATER = "2026-11-21T05:00:00Z", "2026-11-21T05:20:00Z", "2026-11-21T05:40:00Z"
REFUSED = "cloudProviderError"
TIMED_OUT = f"Scale-up timed out for node group {POOL} after 12m0.5s"
RAISED = f"raised the autoscaler alert for {POOL}"
Answer = str | tuple[int, object] | Exception


def group(
    name: str = POOL,
    status: str = "NoActivity",
    code: str = "",
    message: str = "",
    since: str = EARLY,
    ready: int = 5,
    late: int = 0,
) -> str:
    backoff = f"    backoffInfo:\n      errorCode: {code}\n      errorMessage: {json.dumps(message)}\n" if code else ""
    return (
        f"- name: {name}\n"
        "  health:\n"
        "    status: Healthy\n"
        "    nodeCounts:\n"
        "      registered:\n"
        f"        total: {ready + 1}\n"
        f"        ready: {ready}\n"
        "        notStarted: 1\n"
        f"      longUnregistered: {late}\n"
        "      unregistered: 0\n"
        "    cloudProviderTarget: 6\n"
        "    minSize: 0\n"
        "    maxSize: 7\n"
        '    lastProbeTime: "2026-11-21T06:00:00Z"\n'
        "  scaleUp:\n"
        f"    status: {status}\n"
        f"{backoff}"
        '    lastProbeTime: "2026-11-21T06:00:00Z"\n'
        f'    lastTransitionTime: "{since}"\n'
    )


def failed(code: str = REFUSED, message: str = "launch refused", since: str = EARLY, **fields: Any) -> str:
    return group(status="Backoff", code=code, message=message, since=since, **fields)


def status(*groups: str) -> str:
    return "time: 2026-11-21 06:00:00.000000000 +0000 UTC\nautoscalerStatus: Running\nnodeGroups:\n" + "".join(groups)


def alert(count: int, reason: str, name: str = POOL, ready: int = 5, late: int = 0) -> str:
    return (
        f"Cluster autoscaler node group {name} (min 0, max 7 nodes) has {count} failed scale ups in a row, "
        f"last failure: {reason}. {ready} of 6 target nodes are ready and {late} launched instances are past the "
        "join budget."
    )


class Api:
    """Answer each status read with the next answer: a status text, a status code and body, or an error to raise."""

    def __init__(self, answers: list[Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def send(self, method: str, path: str) -> tuple[int, object]:
        self.calls.append((method, path))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, tuple) else (200, {"data": {"status": answer}})


class Ledger:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str, str]] = []
        self.closed: list[tuple[str, str, str]] = []

    def raise_alert(self, slug: str, source: str, text: str) -> None:
        self.raised.append((slug, source, text))

    def close_alert(self, slug: str, alert_id: str, outcome: str) -> None:
        self.closed.append((slug, alert_id, outcome))


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Api]:
    def install(*answers: Answer) -> Api:
        fake = Api(list(answers))
        monkeypatch.setenv(autoscaler_watch.SETTING, NAME)
        monkeypatch.setattr(autoscaler_watch.KubeHttp, "in_cluster", lambda environ: fake)
        return fake

    return install


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def passes(store: RedisStore, ledger: Ledger, count: int, doc: dict[str, Any] | None = None) -> list[list[str]]:
    return [autoscaler_watch.alert_pass("sw", store, ledger, doc or {}) for _ in range(count)]


OPEN = {"id": "al-1", "source": "autoscaler", "state": "open"}
ALERTS = {
    "alerts": [{**OPEN, "id": "al-0", "state": "done"}, OPEN, {"id": "al-2", "source": "release", "state": "open"}]
}


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_two_failed_scale_ups_in_a_row_raise_one_alert_naming_the_group_reason_and_count(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(failed(since=EARLY)), status(failed(since=LATE)))
    ledger = Ledger()
    assert passes(store, ledger, 2) == [[], [RAISED]]
    assert ledger.raised == [("sw", "autoscaler", alert(2, f"{REFUSED}: launch refused"))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_one_failed_scale_up_raises_nothing_however_often_it_is_read(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(failed()), status(failed()), status(failed()))
    ledger = Ledger()
    assert passes(store, ledger, 3) == [[], [], []]
    assert ledger.raised == []


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_new_failure_message_under_the_kept_transition_time_counts_as_the_next_failure(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(failed()), status(failed(message="launch refused again at once")))
    ledger = Ledger()
    assert passes(store, ledger, 2) == [[], [RAISED]]
    assert ledger.raised == [("sw", "autoscaler", alert(2, f"{REFUSED}: launch refused again at once"))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_timed_out_scale_up_raises_on_its_own(api: Callable[..., Api], store: RedisStore) -> None:
    api(status(failed(code="timeout", message=TIMED_OUT)))
    ledger = Ledger()
    assert passes(store, ledger, 1) == [[RAISED]]
    assert ledger.raised == [("sw", "autoscaler", alert(1, f"timeout: {TIMED_OUT}"))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_launched_instances_past_the_join_budget_raise_on_their_own_each_time_they_rise(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(group(late=2)), status(group(late=2)), status(group(late=3)), status(group(late=1)))
    ledger = Ledger()
    assert passes(store, ledger, 4) == [[RAISED], [], [RAISED], []]
    assert [text for _, _, text in ledger.raised] == [alert(0, "none", late=2), alert(0, "none", late=3)]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_rise_in_ready_nodes_resets_the_count_and_a_drop_does_not(api: Callable[..., Api], store: RedisStore) -> None:
    api(
        status(failed(since=EARLY, ready=5)),
        status(group(status="InProgress", since=LATE, ready=6)),
        status(failed(since=LATER, ready=6)),
        status(failed(since="2026-11-21T06:00:00Z", ready=4)),
    )
    ledger = Ledger()
    assert passes(store, ledger, 4) == [[], [], [], [RAISED]]
    assert ledger.raised == [("sw", "autoscaler", alert(2, f"{REFUSED}: launch refused", ready=4))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_groups_failing_in_the_same_read_share_one_alert(api: Callable[..., Api], store: RedisStore) -> None:
    api(status(failed(code="timeout", message=TIMED_OUT), group(name=OTHER, ready=2, late=1)))
    ledger = Ledger()
    assert passes(store, ledger, 1) == [[f"raised the autoscaler alert for {POOL}, {OTHER}"]]
    both = f"{alert(1, f'timeout: {TIMED_OUT}')} {alert(0, 'none', name=OTHER, ready=2, late=1)}"
    assert ledger.raised == [("sw", "autoscaler", both)]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_an_open_autoscaler_alert_holds_a_second_raise_and_other_alerts_do_not(
    api: Callable[..., Api], store: RedisStore
) -> None:
    timed_out = {"code": "timeout", "message": TIMED_OUT}
    api(status(failed(**timed_out)), status(failed(since=LATE, **timed_out)), status(failed(since=LATER, **timed_out)))
    ledger = Ledger()
    assert autoscaler_watch.alert_pass("sw", store, ledger, {"alerts": [OPEN]}) == []
    assert autoscaler_watch.alert_pass("sw", store, ledger, {"alerts": [{**OPEN, "state": "claimed"}]}) == []
    others = {"alerts": [{**OPEN, "state": "done"}, {"id": "al-2", "source": "release", "state": "open"}]}
    assert autoscaler_watch.alert_pass("sw", store, ledger, others) == [RAISED]
    assert ledger.raised == [("sw", "autoscaler", alert(3, f"timeout: {TIMED_OUT}"))]
    assert ledger.closed == []


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_evidence_an_open_alert_held_back_raises_once_no_autoscaler_alert_is_open(
    api: Callable[..., Api], store: RedisStore
) -> None:
    timed_out = failed(code="timeout", message=TIMED_OUT, name=OTHER, ready=2)
    api(status(group(), timed_out), status(group(), timed_out), status(group(), timed_out))
    ledger = Ledger()
    assert autoscaler_watch.alert_pass("sw", store, ledger, {"alerts": [OPEN]}) == []
    assert passes(store, ledger, 2) == [[f"raised the autoscaler alert for {OTHER}"], []]
    assert ledger.raised == [("sw", "autoscaler", alert(1, f"timeout: {TIMED_OUT}", name=OTHER, ready=2))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_held_evidence_of_a_group_that_recovered_raises_nothing(api: Callable[..., Api], store: RedisStore) -> None:
    api(status(failed(code="timeout", message=TIMED_OUT, ready=5)), status(group(status="InProgress", ready=6)))
    ledger = Ledger()
    assert autoscaler_watch.alert_pass("sw", store, ledger, {"alerts": [OPEN]}) == []
    assert autoscaler_watch.alert_pass("sw", store, ledger, {}) == []
    assert ledger.raised == []


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_recovery_closes_the_open_autoscaler_alert_once_every_group_recovered(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(
        status(failed(code="timeout", message=TIMED_OUT, ready=5), group(name=OTHER, ready=2)),
        status(group(status="InProgress", since=LATE, ready=6), group(name=OTHER, ready=2, late=1)),
        status(group(since=LATER, ready=6), group(name=OTHER, ready=2)),
    )
    ledger = Ledger()
    assert passes(store, ledger, 3, ALERTS) == [
        [],
        [],
        ["closed the autoscaler alert al-1: every node group recovered"],
    ]
    assert ledger.closed == [("sw", "al-1", autoscaler_watch.RECOVERED)]
    assert ledger.raised == []


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_group_in_backoff_or_with_a_count_no_ready_rise_reset_holds_the_close(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(
        status(failed(ready=5)),
        status(failed(ready=6)),
        status(failed(since=LATE, ready=6)),
        status(group(since=LATER, ready=6)),
        status(group(since=LATER, ready=7)),
    )
    ledger = Ledger()
    closed = ["closed the autoscaler alert al-1: every node group recovered"]
    assert passes(store, ledger, 5, ALERTS) == [[], [], [], [], closed]
    assert ledger.closed == [("sw", "al-1", autoscaler_watch.RECOVERED)]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_an_unset_setting_reads_no_status_and_keeps_no_count(
    api: Callable[..., Api], store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = api()
    monkeypatch.delenv(autoscaler_watch.SETTING)
    assert autoscaler_watch.alert_pass("sw", store, Ledger(), ALERTS) == []
    assert (fake.calls, store.redis.keys("*")) == ([], [])


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_failed_status_read_names_why_and_keeps_the_count(api: Callable[..., Api], store: RedisStore) -> None:
    api(
        status(failed(since=EARLY)),
        (403, {"reason": "Forbidden"}),
        ConnectionError("the service account token is unreadable"),
        (200, {}),
        status(failed(since=LATE)),
    )
    ledger = Ledger()
    why = f"read no autoscaler status from {NAME}:"
    assert passes(store, ledger, 5, ALERTS) == [
        [],
        [f"{why} the API server answered 403"],
        [f"{why} ConnectionError: the service account token is unreadable"],
        [f"{why} KeyError: 'data'"],
        [],
    ]
    assert (ledger.raised, ledger.closed) == ([], [])


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_the_count_survives_a_failed_read_and_raises_on_the_next_failure(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(failed(since=EARLY)), (500, {}), status(failed(since=LATE)))
    ledger = Ledger()
    assert passes(store, ledger, 3)[2] == [RAISED]
    assert ledger.raised == [("sw", "autoscaler", alert(2, f"{REFUSED}: launch refused"))]


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_each_node_group_record_lives_under_the_swarm_autoscaler_watch_key(
    api: Callable[..., Api], store: RedisStore
) -> None:
    api(status(failed(since=EARLY)))
    assert autoscaler_watch.alert_pass("sw", store, Ledger(), {}) == []
    record = json.loads(store.redis.get(store.key("sw", "autoscaler-watch")))[POOL]
    assert (record["count"], record["pair"], record["held"]) == (1, [EARLY, "launch refused"], False)


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_the_status_is_read_from_the_named_config_map_with_the_pod_environment(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, seen = Api([status(group())]), []
    monkeypatch.setenv(autoscaler_watch.SETTING, NAME)
    monkeypatch.setattr(autoscaler_watch.KubeHttp, "in_cluster", lambda environ: seen.append(environ) or fake)
    assert autoscaler_watch.alert_pass("sw", store, Ledger(), {}) == []
    assert fake.calls == [("GET", "/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-oci-status")]
    assert len(seen) == 1 and seen[0] is os.environ


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_a_raise_text_is_cut_to_the_length_the_ledger_takes(api: Callable[..., Api], store: RedisStore) -> None:
    api(status(failed(code="timeout", message="x" * 3000)))
    ledger = Ledger()
    assert passes(store, ledger, 1) == [[RAISED]]
    [(_, _, text)] = ledger.raised
    assert text == alert(1, "timeout: " + "x" * 3000)[:2000]
