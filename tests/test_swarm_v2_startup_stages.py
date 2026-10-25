import json

import pytest

from scripts.swarm_v2 import startup_bench as bench
from scripts.swarm_v2 import startup_stages as stages
from scripts.swarm_v2.resource_profiles import ClaimRefused

pytestmark = pytest.mark.unit

T0 = 1_791_729_000_000
NODE = "anton-ec2-0ad35d2621bca80ff"
OLD = "k8s-worker-1"


def ev(pod, reason, seconds, message=""):
    event = {"pod": pod, "reason": reason, "at": T0 + round(seconds * 1000)}
    if reason in stages.MESSAGE_REASONS:
        event["message"] = message
    return event


def cold_events(pod="default/a", group="anton-k3s-swarm", before=0, pull="in 20s (20s including waiting)"):
    return [
        ev(pod, "FailedScheduling", 0),
        ev(pod, "TriggeredScaleUp", 9, f"pod triggered scale-up: [{{{group} {before}->{before + 1} (max: 4)}}]"),
        ev(pod, "Scheduled", 70, f"Successfully assigned {pod} to {NODE}"),
        ev(pod, "Pulling", 70.5),
        ev(pod, "Pulled", 90.5, f'Successfully pulled image "x" {pull}. Image size: 1 bytes.'),
        ev(pod, "Started", 95),
    ]


def read(events, pods=None, launched=21, requested=12, source=stages.NEW_INSTANCE):
    return {
        "read_at": "2026-10-11T14:57:00Z",
        "events": events,
        "nodes": {
            NODE: {"instance": "i-0ad35d2621bca80ff", "ready": T0 + 67_000},
            OLD: {"instance": "", "ready": T0 - 3_600_000},
        },
        "pods": pods or {},
        "launches": {
            "i-0ad35d2621bca80ff": {
                "group": "anton-k3s-swarm",
                "requested": T0 + requested * 1000,
                "launched": T0 + launched * 1000,
                "source": source,
            }
        },
    }


def warm_events(pod="anton-prod/web", message='Container image "x" already present on machine'):
    return [
        ev(pod, "Scheduled", 1, f"Successfully assigned {pod} to {OLD}"),
        ev(pod, "Pulled", 2, message),
        ev(pod, "Started", 2.5),
    ]


def only(entries, pod):
    return next(entry for entry in entries if entry["pod"] == pod)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ('Successfully pulled image "x" in 537ms (537ms including waiting). Image size: 1 bytes.', 537),
        ('Successfully pulled image "x" in 3.534s (3.534s including waiting)', 3534),
        ('Successfully pulled image "x" in 1m2.5s (1m2.5s including waiting)', 62500),
        ('Successfully pulled image "x" in 2m0s (2m0s including waiting)', 120000),
        ('Container image "x" already present on machine and can be accessed by the pod', 0),
        ("Pulled something else", None),
    ],
)
def test_pull_duration_reads_every_kubelet_unit(message, expected):
    assert stages.pull_ms(message) == expected


def test_instant_reads_seconds_fractions_and_offsets():
    assert stages.instant("2026-10-11T14:36:30Z") == stages.instant("2026-10-11T14:36:30.000+00:00")
    assert stages.instant("2026-10-11T14:36:30.295Z") - stages.instant("2026-10-11T14:36:30Z") == 295


def test_a_scale_from_zero_attributes_every_stage_separately():
    pods = {"default/a": {"created": T0 - 500, "ready": T0 + 97_000}}
    [entry] = stages.attribute(read(cold_events(), pods))
    assert entry["stages"] == {
        "trigger": 9000,
        "instance": 12000,
        "join": 46000,
        "schedule": 3000,
        "image": 20000,
        "pull_retry": 0,
        "pod_start": 5000,
        "bootstrap": 2000,
        "handshake": None,
    }
    assert (entry["node_kind"], entry["node_source"], entry["from_zero"]) == ("cold", "new_instance", True)
    assert (entry["image_cache"], entry["outcome"], entry["transient"]) == ("uncached", "started", "")
    assert entry["node"] == NODE and entry["failure_ms"] is None and entry["sandbox_retries"] == 0


def test_a_pod_riding_an_earlier_scale_up_is_charged_from_that_scale_up():
    first = cold_events("default/first")[:2]
    second = [ev("default/b", "FailedScheduling", 5)] + [
        ev("default/b", "TriggeredScaleUp", 30, "pod triggered scale-up: [{anton-k3s-swarm 1->2 (max: 4)}]"),
        ev("default/b", "Scheduled", 70, f"Successfully assigned default/b to {NODE}"),
        ev("default/b", "Started", 72),
    ]
    entry = only(stages.attribute(read(first + second, requested=10)), "default/b")
    assert entry["stages"]["trigger"] == 4000
    assert entry["stages"]["instance"] == 12000
    assert entry["from_zero"] is True


def test_a_scale_up_after_the_launch_request_is_not_its_cause():
    events = [
        ev("default/b", "FailedScheduling", 0),
        ev("default/b", "TriggeredScaleUp", 15, "pod triggered scale-up: [{anton-k3s-swarm 1->2 (max: 4)}]"),
        ev("default/b", "Scheduled", 70, f"Successfully assigned default/b to {NODE}"),
    ]
    [entry] = stages.attribute(read(events))
    assert entry["node_kind"] == "warm" and entry["stages"]["instance"] is None
    assert entry["from_zero"] is False


def test_a_node_requested_before_the_pod_waited_is_a_warm_node():
    events = cold_events()
    events[0] = ev("default/a", "FailedScheduling", 13)
    [entry] = stages.attribute(read(events, requested=12))
    assert entry["node_kind"] == "warm" and entry["node_source"] == "existing" and not entry["from_zero"]
    assert all(entry["stages"][stage] is None for stage in stages.NODE_STAGES)


def test_a_scale_up_from_a_non_empty_pool_is_cold_but_not_from_zero():
    [entry] = stages.attribute(read(cold_events(before=2), source=stages.WARM_POOL))
    assert (entry["node_kind"], entry["node_source"], entry["from_zero"]) == ("cold", "warm_pool", False)


def test_a_cold_node_without_a_ready_time_or_launch_is_not_attributed():
    unready = read(cold_events())
    unready["nodes"][NODE]["ready"] = None
    unlaunched = read(cold_events())
    unlaunched["launches"] = {}
    for case in (unready, unlaunched):
        [entry] = stages.attribute(case)
        assert entry["node_kind"] == "warm"


def test_a_warm_node_schedules_from_pod_creation_and_reads_the_cache():
    pods = {"anton-prod/web": {"created": T0 + 600, "ready": T0 + 3_500}}
    [entry] = stages.attribute(read(warm_events(), pods))
    assert entry["stages"]["schedule"] == 400
    assert entry["stages"]["image"] == 0 and entry["image_cache"] == "cached"
    assert entry["stages"]["pod_start"] == 1500 and entry["stages"]["bootstrap"] == 1000
    assert entry["node_kind"] == "warm" and entry["node_source"] == "existing"


def test_mixed_pulls_are_partial_and_a_pod_without_pulls_is_unknown():
    mixed = warm_events() + [ev("anton-prod/web", "Pulled", 2.2, 'pulled "y" in 1s')]
    [entry] = stages.attribute(read(mixed))
    assert entry["image_cache"] == "partial" and entry["stages"]["image"] == 1000
    bare = [ev("anton-prod/bare", "Scheduled", 1, f"Successfully assigned anton-prod/bare to {OLD}")]
    [entry] = stages.attribute(read(bare))
    assert entry["image_cache"] == "unknown" and entry["outcome"] == "pending"


def test_bootstrap_needs_a_ready_time_at_or_after_the_last_start():
    stale = {"anton-prod/web": {"created": T0, "ready": T0 + 2_000}}
    [entry] = stages.attribute(read(warm_events(), stale))
    assert entry["stages"]["bootstrap"] is None
    exact = {"anton-prod/web": {"created": T0, "ready": T0 + 2_500}}
    [entry] = stages.attribute(read(warm_events(), exact))
    assert entry["stages"]["bootstrap"] == 0


def test_pod_start_never_goes_negative_when_the_pull_overlaps_scheduling():
    events = warm_events(message='pulled "x" in 5s')
    [entry] = stages.attribute(read(events))
    assert entry["stages"]["pod_start"] == 0


def test_a_failure_the_pod_started_past_is_transient_and_counted():
    events = warm_events() + [ev("anton-prod/web", "FailedCreatePodSandBox", 1.5)] * 2
    [entry] = stages.attribute(read(sorted(events, key=lambda e: e["at"])))
    assert (entry["outcome"], entry["transient"], entry["sandbox_retries"]) == ("started", "sandbox", 2)
    assert entry["failure_ms"] == 500


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'Failed to pull image "k:5000/w": dial tcp 172.18.0.2:5000: connect: connection refused',
            "registry_unavailable",
        ),
        ('Failed to pull image "k:5000/w": rpc error: code = NotFound desc = manifest unknown', "image_not_found"),
        ('Failed to pull image "g/w": 401 Unauthorized', "registry_unauthorized"),
        ('Failed to pull image "g/w": failed to resolve reference "g/w": i/o timeout', "registry_unavailable"),
        ('Failed to pull image "g/w": something new', "image_pull"),
    ],
)
def test_pull_failures_are_classified_by_their_message(message, expected):
    events = [
        ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}"),
        ev("p/x", "Pulling", 1.2),
        ev("p/x", "Failed", 4, message),
    ]
    [entry] = stages.attribute(read(events))
    assert (entry["outcome"], entry["failure_ms"]) == (expected, 3000)


def test_a_backoff_alone_is_an_image_pull_failure_and_other_failures_keep_their_class():
    backoff = [ev("p/x", "BackOff", 3, 'Back-off pulling image "g/w"')]
    assert stages.failure_class(backoff) == "image_pull"
    assert stages.failure_class([ev("p/x", "Failed", 3, "Error: ImagePullBackOff")]) == "image_pull"
    assert stages.failure_class([ev("p/x", "BackOff", 3, "Back-off restarting failed container")]) == ""
    assert stages.failure_class([ev("p/x", "Failed", 3, "Error: container exited")]) == ""
    assert stages.failure_class([ev("p/x", "FailedCreatePodSandBox", 3)]) == "sandbox"
    assert stages.failure_class([ev("p/x", "FailedScheduling", 3)]) == "unschedulable"
    placed = [ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}"), ev("p/x", "FailedScheduling", 0)]
    assert stages.failure_class(placed) == ""


def test_an_unscheduled_failure_times_from_its_first_pull_or_not_at_all():
    pulled = [ev("p/x", "Pulling", 1), ev("p/x", "Failed", 4, 'Failed to pull image "x": EOF')]
    [entry] = stages.attribute(read(pulled))
    assert entry["failure_ms"] == 3000 and entry["node"] == ""
    untimed = [ev("p/y", "Failed", 4, "Error: ImagePullBackOff")]
    [entry] = stages.attribute(read(untimed))
    assert entry["failure_ms"] is None


def test_pods_without_placement_or_failure_evidence_are_skipped():
    assert stages.attribute(read([ev("p/x", "Pulling", 1), ev("p/x", "Started", 2)])) == []


def test_the_report_keeps_failures_beside_starts_and_splits_node_from_image_time():
    failed = [
        ev("p/f", "Scheduled", 1, f"Successfully assigned p/f to {OLD}"),
        ev("p/f", "Pulling", 1),
        ev("p/f", "Failed", 6, 'Failed to pull image "x": connection refused'),
    ]
    samples = stages.attribute(read(cold_events() + warm_events() + failed))
    result = stages.report(samples)
    assert result["metric"] == "startup_stage_p50_p95" and result["unit"] == "ms"
    cold = result["scenarios"]["cold-node/uncached-image"]
    assert cold["node_provision_ms"]["p50"] == 9000 + 12000 + 46000 + 3000
    assert cold["image_init_ms"]["p50"] == 20000
    assert cold["from_zero"] == 1 and cold["node_sources"] == {"new_instance": 1}
    unknown = result["scenarios"]["warm-node/unknown-image"]
    assert (unknown["samples"], unknown["started"], unknown["failures"]) == (1, 0, {"registry_unavailable": 1})
    assert unknown["failure_ms"]["p95"] == 5000 and unknown["stages"] == {}
    warm = result["scenarios"]["warm-node/cached-image"]
    assert warm["node_provision_ms"] is None and "trigger" not in warm["stages"]


def test_the_report_counts_pending_and_transient_separately():
    pending = [ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}")]
    retried = warm_events() + [ev("anton-prod/web", "FailedCreatePodSandBox", 1.5)]
    row = stages.report(stages.attribute(read(pending + retried)))["scenarios"]
    assert row["warm-node/unknown-image"]["pending"] == 1 and row["warm-node/unknown-image"]["failures"] == {}
    assert row["warm-node/cached-image"]["transient"] == {"sandbox": 1}
    assert row["warm-node/cached-image"]["failure_ms"]["p50"] == 500


def test_percentiles_use_nearest_rank_over_started_samples():
    events = []
    for index, seconds in enumerate((1, 2, 3, 4, 20)):
        pod = f"p/{index}"
        events += [
            ev(pod, "Scheduled", 0, f"Successfully assigned {pod} to {OLD}"),
            ev(pod, "Pulled", 0, f'pulled "x" in {seconds}s'),
            ev(pod, "Started", seconds),
        ]
    spread = stages.report(stages.attribute(read(events)))["scenarios"]["warm-node/uncached-image"]["stages"]["image"]
    assert (spread["n"], spread["p50"], spread["p95"], spread["max"]) == (5, 3000, 20000, 20000)


def test_only_from_zero_uncached_cold_samples_carry_the_cold_label():
    samples = stages.attribute(read(cold_events()))
    assert stages.claim(samples, stages.COLD_LABEL) == samples


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (lambda: read(warm_events()), "the Pod landed on a node that existed before it waited"),
        (lambda: read(cold_events(before=3)), "the scale up did not start from an empty pool"),
        (lambda: read(cold_events(pull="already present on machine")), "the image cache was cached"),
    ],
)
def test_a_warm_sample_cannot_be_advertised_as_cold(build, reason):
    samples = stages.attribute(build())
    with pytest.raises(ClaimRefused) as refused:
        stages.claim(samples, stages.COLD_LABEL)
    assert str(refused.value) == "a warm sample cannot be advertised as cold scale from zero"
    assert list(refused.value.refused.values()) == [reason]


def test_one_warm_sample_spoils_a_cold_claim_and_empty_or_unknown_claims_are_refused():
    samples = stages.attribute(read(cold_events() + warm_events()))
    with pytest.raises(ClaimRefused) as refused:
        stages.claim(samples, stages.COLD_LABEL)
    assert list(refused.value.refused) == ["anton-prod/web"]
    with pytest.raises(ClaimRefused, match="no samples"):
        stages.claim([], stages.COLD_LABEL)
    with pytest.raises(ClaimRefused, match="unsupported claim label"):
        stages.claim(samples[:1], "fast")


def test_pull_failures_past_the_grace_or_never_timed_are_unbounded():
    samples = [
        {"pod": "a", "outcome": "registry_unavailable", "failure_ms": 120_000},
        {"pod": "b", "outcome": "image_pull", "failure_ms": 120_001},
        {"pod": "c", "outcome": "image_not_found", "failure_ms": None},
        {"pod": "d", "outcome": "sandbox", "failure_ms": None},
        {"pod": "e", "outcome": "started", "failure_ms": None},
        {"pod": "f", "outcome": "registry_unauthorized", "failure_ms": 1},
        {"pod": "g", "outcome": "started", "transient": "registry_unavailable", "failure_ms": 120_001},
        {"pod": "h", "outcome": "started", "transient": "sandbox", "failure_ms": 120_001},
        {"pod": "i", "outcome": "started", "transient": "image_pull", "failure_ms": 2},
    ]
    assert stages.unbounded(samples) == ["b", "c", "g"]
    assert stages.unbounded(samples, grace_ms=0) == ["a", "b", "c", "f", "g", "i"]


def raw_capture():
    def item(reason, when, kind="Pod", message="m", name="a", field="firstTimestamp"):
        return {
            "reason": reason,
            field: when,
            "message": message,
            "involvedObject": {"kind": kind, "namespace": "default", "name": name},
        }

    return {
        "read_at": "2026-10-11T14:57:00Z",
        "events": {
            "items": [
                item(
                    "Scheduled",
                    "2026-10-11T14:38:52.100000Z",
                    message=f"Successfully assigned default/a to {NODE}",
                    field="eventTime",
                ),
                item("FailedScheduling", "2026-10-11T14:36:08Z", message="0/14 nodes are available"),
                item("Killing", "2026-10-11T14:40:00Z"),
                item("Starting", "2026-10-11T14:37:15Z", kind="Node", name=NODE),
                {"reason": "Pulled", "involvedObject": {"kind": "Pod", "namespace": "default", "name": "b"}},
            ]
        },
        "nodes": {
            "items": [
                {
                    "metadata": {"name": NODE},
                    "spec": {"providerID": "aws:///eu-west-1c/i-0ad35d2621bca80ff"},
                    "status": {
                        "conditions": [
                            {"type": "MemoryPressure", "status": "False", "lastTransitionTime": "2026-10-11T14:37:10Z"},
                            {"type": "Ready", "status": "True", "lastTransitionTime": "2026-10-11T14:37:16Z"},
                        ]
                    },
                },
                {
                    "metadata": {"name": "k8s-master"},
                    "spec": {},
                    "status": {
                        "conditions": [
                            {"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-11T14:00:00Z"}
                        ]
                    },
                },
            ]
        },
        "pods": {
            "items": [
                {
                    "metadata": {"namespace": "default", "name": "a", "creationTimestamp": "2026-10-11T14:36:08Z"},
                    "status": {},
                }
            ]
        },
        "activities": {
            "Activities": [
                {
                    "AutoScalingGroupName": "anton-k3s-swarm",
                    "StartTime": "2026-10-11T14:36:30.295Z",
                    "StatusCode": "Successful",
                    "Description": "Launching a new EC2 instance: i-0ad35d2621bca80ff",
                },
                {
                    "AutoScalingGroupName": "anton-k3s-ondemand",
                    "StartTime": "2026-10-11T14:21:02.691Z",
                    "StatusCode": "Successful",
                    "Description": "Launching a new EC2 instance from warm pool: i-0b28",
                },
                {
                    "AutoScalingGroupName": "anton-k3s-ondemand",
                    "StartTime": "2026-10-11T14:07:54.414Z",
                    "StatusCode": "Successful",
                    "Description": "Launching a new EC2 instance into warm pool: i-0c",
                },
                {
                    "AutoScalingGroupName": "anton-k3s-swarm",
                    "StartTime": "2026-10-11T14:00:00Z",
                    "StatusCode": "Failed",
                    "Description": "Launching a new EC2 instance: i-0dd",
                },
                {
                    "AutoScalingGroupName": "anton-k3s-swarm",
                    "StartTime": "2026-10-11T14:01:00Z",
                    "StatusCode": "Successful",
                    "Description": "Terminating EC2 instance: i-0ee",
                },
            ]
        },
        "instances": {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-0ad35d2621bca80ff", "LaunchTime": "2026-10-11T14:36:30.000Z"}]}
            ]
        },
    }


def test_reduce_keeps_only_what_attribution_reads():
    reduced = stages.reduce(raw_capture())
    assert reduced["events"] == [
        {"pod": "default/a", "reason": "FailedScheduling", "at": stages.instant("2026-10-11T14:36:08Z")},
        {
            "pod": "default/a",
            "reason": "Scheduled",
            "at": stages.instant("2026-10-11T14:38:52.1Z"),
            "message": f"Successfully assigned default/a to {NODE}",
        },
    ]
    assert reduced["nodes"] == {
        "k8s-master": {"instance": "", "ready": None},
        NODE: {"instance": "i-0ad35d2621bca80ff", "ready": stages.instant("2026-10-11T14:37:16Z")},
    }
    assert reduced["pods"] == {"default/a": {"created": stages.instant("2026-10-11T14:36:08Z"), "ready": None}}
    assert reduced["launches"] == {
        "i-0ad35d2621bca80ff": {
            "group": "anton-k3s-swarm",
            "requested": stages.instant("2026-10-11T14:36:30.295Z"),
            "launched": stages.instant("2026-10-11T14:36:30Z"),
            "source": "new_instance",
        },
        "i-0b28": {
            "group": "anton-k3s-ondemand",
            "requested": stages.instant("2026-10-11T14:21:02.691Z"),
            "launched": None,
            "source": "warm_pool",
        },
    }
    assert reduced["read_at"] == "2026-10-11T14:57:00Z"


def test_the_report_names_sequential_container_image_pulls_with_their_own_durations():
    raw = raw_capture()
    for container, image, when, duration in [
        ("bundle", "registry/bundle:release", "2026-10-11T14:38:54Z", "2s"),
        ("worker", "registry/worker:release", "2026-10-11T14:39:24Z", "30s"),
    ]:
        raw["events"]["items"].append(
            {
                "reason": "Pulled",
                "firstTimestamp": when,
                "message": f'Successfully pulled image "{image}" in {duration} (including waiting)',
                "involvedObject": {
                    "kind": "Pod",
                    "namespace": "default",
                    "name": "a",
                    "fieldPath": f"spec.initContainers{{{container}}}"
                    if container == "bundle"
                    else f"spec.containers{{{container}}}",
                },
            }
        )
    raw["events"]["items"].append(
        {
            "reason": "Started",
            "firstTimestamp": "2026-10-11T14:39:25Z",
            "involvedObject": {"kind": "Pod", "namespace": "default", "name": "a"},
        }
    )
    [sample] = stages.attribute(stages.reduce(raw))
    assert sample["image_pulls"] == [
        {"container": "bundle", "image": "registry/bundle:release", "duration_ms": 2000},
        {"container": "worker", "image": "registry/worker:release", "duration_ms": 30000},
    ]
    assert sample["stages"]["image"] == 32000
    result = stages.report([sample])
    assert result["scenarios"]["warm-node/uncached-image"]["image_pulls"] == [
        {
            "container": "bundle",
            "image": "registry/bundle:release",
            "duration_ms": {"n": 1, "min": 2000, "p50": 2000, "p95": 2000, "max": 2000},
        },
        {
            "container": "worker",
            "image": "registry/worker:release",
            "duration_ms": {"n": 1, "min": 30000, "p50": 30000, "p95": 30000, "max": 30000},
        },
    ]
    rendered = bench.markdown(result, "sequential pulls")
    assert "| warm-node/uncached-image | bundle | registry/bundle:release | 2.0 / 2.0 (n 1) |" in rendered
    assert "| warm-node/uncached-image | worker | registry/worker:release | 30.0 / 30.0 (n 1) |" in rendered


def test_a_report_of_an_older_attributed_sample_has_no_container_pull_details():
    [sample] = stages.attribute(read(cold_events()))
    del sample["image_pulls"]
    result = stages.report([sample])
    row = result["scenarios"]["cold-node/uncached-image"]
    assert row["image_pulls"] == []
    assert row["stages"]["image"]["p50"] == 20000


def test_scale_ups_are_deduplicated_across_pods_and_ordered_per_group():
    message = "pod triggered scale-up: [{g1 4->7 (max: 20)} {g2 0->1 (max: 4)}]"
    events = [
        ev("p/a", "TriggeredScaleUp", 5, message),
        ev("p/b", "TriggeredScaleUp", 5, message),
        ev("p/c", "TriggeredScaleUp", 1, "pod triggered scale-up: [{g1 2->4 (max: 20)}]"),
    ]
    assert stages.scale_ups(events) == {"g1": [(T0 + 1000, 2), (T0 + 5000, 4)], "g2": [(T0 + 5000, 0)]}


OCID = "ocid1.instance.oc1.eu-frankfurt-1.abc"
POOL = "ocid1.instancepool.oc1.eu-frankfurt-1.research"


def scaled_event(when, message, reason="ScaledUpGroup", field="firstTimestamp"):
    return {
        "reason": reason,
        field: when,
        "message": message,
        "involvedObject": {"kind": "ConfigMap", "namespace": "kube-system", "name": "cluster-autoscaler-oci-status"},
    }


def oci_capture():
    raw = raw_capture()
    raw["nodes"]["items"][0]["spec"]["providerID"] = OCID
    raw["events"]["items"] += [
        scaled_event("2026-10-11T14:36:20Z", f"Scale-up: setting group {POOL} size to 1 instead of 0 (max: 10)"),
        scaled_event(
            "2026-10-11T14:50:00.25Z",
            f"Scale-up: setting group {POOL} size to 3 instead of 1 (max: 10)",
            field="eventTime",
        ),
        scaled_event("2026-10-11T14:36:21Z", f"Scale-up: setting group {POOL} size to 1 instead of 0", reason="Other"),
        scaled_event("2026-10-11T14:36:22Z", "Scale-up: group size unchanged"),
        {
            **scaled_event("2026-10-10T09:00:00Z", f"Scale-up: setting group {POOL} size to 1 instead of 0 (max: 10)"),
            "lastTimestamp": "2026-10-11T14:40:00Z",
            "count": 3,
        },
        {"reason": "ScaledUpGroup", "message": f"Scale-up: setting group {POOL} size to 2 instead of 1"},
    ]
    raw["activities"] = {"Activities": []}
    raw["instances"] = {
        "Reservations": [],
        "Oci": [{"id": OCID, "time-created": "2026-10-11T14:36:41.500000+00:00", "instance-pool": POOL}],
    }
    return raw


def test_the_autoscaler_scaled_up_group_events_give_each_group_its_scale_ups():
    assert stages.scaled_groups(oci_capture()["events"]["items"]) == {
        POOL: [
            [stages.instant("2026-10-10T09:00:00Z"), 0],
            [stages.instant("2026-10-11T14:36:20Z"), 0],
            [stages.instant("2026-10-11T14:40:00Z"), 0],
            [stages.instant("2026-10-11T14:50:00.25Z"), 1],
        ]
    }
    assert stages.scaled_groups([]) == {}


def test_reduce_charges_an_oci_instance_to_its_pool_at_its_creation():
    reduced = stages.reduce(oci_capture())
    assert reduced["nodes"][NODE]["instance"] == OCID
    assert reduced["scaled_groups"] == stages.scaled_groups(oci_capture()["events"]["items"])
    created = stages.instant("2026-10-11T14:36:41.5Z")
    assert reduced["launches"] == {
        OCID: {"group": POOL, "requested": created, "launched": created, "source": stages.NEW_INSTANCE}
    }


def test_a_read_without_oci_listings_has_no_scaled_groups_or_oci_launches():
    reduced = stages.reduce(raw_capture())
    assert reduced["scaled_groups"] == {}
    assert list(reduced["launches"]) == ["i-0ad35d2621bca80ff", "i-0b28"]


def test_an_oci_instance_without_its_pool_tag_fails_the_read():
    raw = oci_capture()
    raw["instances"]["Oci"][0]["instance-pool"] = None
    with pytest.raises(ValueError, match=OCID):
        stages.reduce(raw)


def test_a_scaled_up_group_event_scales_from_zero_when_the_pod_event_comes_late():
    events = [e for e in cold_events(group=POOL) if e["reason"] != "TriggeredScaleUp"]
    events.append(ev("default/a", "TriggeredScaleUp", 80, f"pod triggered scale-up: [{{{POOL} 0->1 (max: 10)}}]"))
    pods = {"default/a": {"created": T0 - 500, "ready": T0 + 97_000}}
    oci = {**read(events, pods), "scaled_groups": {POOL: [[T0 + 9000, 0]]}}
    oci["launches"] = {"i-0ad35d2621bca80ff": {**oci["launches"]["i-0ad35d2621bca80ff"], "group": POOL}}
    [entry] = stages.attribute(oci)
    assert (entry["node_kind"], entry["from_zero"]) == ("cold", True)
    assert {k: entry["stages"][k] for k in stages.NODE_STAGES} == {
        "trigger": 9000,
        "instance": 12000,
        "join": 46000,
        "schedule": 3000,
    }
    assert stages.claim([entry], stages.COLD_LABEL) == [entry]
    assert stages.attribute({**oci, "scaled_groups": {}})[0]["node_kind"] == "warm"
    one_more = {**oci, "scaled_groups": {POOL: [[T0 + 9000, 1]]}}
    assert stages.attribute(one_more)[0]["from_zero"] is False


def test_scaled_up_group_events_join_the_pod_scale_ups_of_the_same_group():
    pods = {"default/a": {"created": T0 - 500, "ready": T0 + 97_000}}
    both = {**read(cold_events(group=POOL), pods), "scaled_groups": {POOL: [[T0 - 60_000, 1]]}}
    both["launches"] = {"i-0ad35d2621bca80ff": {**both["launches"]["i-0ad35d2621bca80ff"], "group": POOL}}
    [entry] = stages.attribute(both)
    assert (entry["from_zero"], entry["stages"]["trigger"]) == (True, 9000)


def write_capture(folder):
    folder.mkdir()
    raw = raw_capture()
    for name in bench.LISTINGS:
        (folder / f"{name}.json").write_text(json.dumps(raw[name]))
    (folder / "read_at").write_text(raw["read_at"] + "\n")


def test_cli_reduce_report_and_claim_round_trip(tmp_path, capsys):
    write_capture(tmp_path / "capture")
    assert bench.main(["reduce", "--capture", str(tmp_path / "capture"), "--output", str(tmp_path / "read.json")]) == 0
    reduced = json.loads((tmp_path / "read.json").read_text())
    assert sorted(reduced["inputs"]) == sorted(bench.LISTINGS)
    assert reduced["read_at"] == "2026-10-11T14:57:00Z"
    assert all(len(digest) == 64 for digest in reduced["inputs"].values())
    args = [
        "report",
        "--read",
        str(tmp_path / "read.json"),
        "--source",
        "fixture run",
        "--output",
        str(tmp_path / "out" / "report.json"),
        "--markdown",
        str(tmp_path / "report.md"),
    ]
    assert bench.main(args) == 0
    written = json.loads((tmp_path / "out" / "report.json").read_text())
    assert written["source"] == "fixture run" and written["samples"][0]["pod"] == "default/a"
    page = (tmp_path / "report.md").read_text()
    assert "Source: fixture run." in page and "| warm-node/unknown-image | 1 | 0 | none |" in page
    code = bench.main(["claim", "--read", str(tmp_path / "read.json"), "--scenario", "warm-node/unknown-image"])
    assert code == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["samples"] == {"default/a": "the Pod landed on a node that existed before it waited"}


def test_cli_claim_accepts_a_cold_scenario(tmp_path, capsys):
    path = tmp_path / "read.json"
    path.write_text(json.dumps(read(cold_events())))
    assert bench.main(["claim", "--read", str(path), "--scenario", "cold-node/uncached-image"]) == 0
    assert json.loads(capsys.readouterr().out) == {"claimed": "cold_scale_from_zero", "samples": 1}


def test_markdown_renders_seconds_with_counts_and_marks_missing_cells():
    result = stages.report(stages.attribute(read(cold_events() + warm_events())))
    page = bench.markdown(result, "Anton read")
    row = next(line for line in page.splitlines() if line.startswith("| cold-node/uncached-image"))
    assert "| none | none | 70.0 / 70.0 (n 1) | 20.0 / 20.0 (n 1) |" in row
    warm = next(line for line in page.splitlines() if line.startswith("| warm-node/cached-image"))
    assert warm.count("n/a") == 7


def test_a_pod_listed_after_its_scheduling_event_is_a_later_incarnation():
    later = {"anton-prod/web": {"created": T0 + 1_001, "ready": T0 + 3_000}}
    [entry] = stages.attribute(read(warm_events(), later))
    assert entry["stages"]["schedule"] is None and entry["stages"]["bootstrap"] is None
    same = {"anton-prod/web": {"created": T0 + 1_000, "ready": T0 + 3_000}}
    [entry] = stages.attribute(read(warm_events(), same))
    assert entry["stages"]["schedule"] == 0 and entry["stages"]["bootstrap"] == 500


def test_each_sample_records_when_it_began():
    [cold] = stages.attribute(read(cold_events()))
    assert cold["at"] == T0 + 70_000
    pulled = [ev("p/x", "Pulling", 1), ev("p/x", "Failed", 4, 'Failed to pull image "x": EOF')]
    assert stages.attribute(read(pulled))[0]["at"] == T0 + 1000
    untimed = [ev("p/y", "Failed", 4, "Error: ImagePullBackOff")]
    assert stages.attribute(read(untimed))[0]["at"] == T0 + 4000


def test_a_warm_pool_instance_cannot_carry_the_cold_label():
    samples = stages.attribute(read(cold_events(), source=stages.WARM_POOL))
    assert samples[0]["from_zero"] is True
    with pytest.raises(ClaimRefused) as refused:
        stages.claim(samples, stages.COLD_LABEL)
    assert refused.value.refused == {"default/a": "the instance came from a warm pool"}


def test_failure_time_is_the_first_event_of_the_failure_class_it_names():
    events = [
        ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}"),
        ev("p/x", "FailedCreatePodSandBox", 2),
        ev("p/x", "BackOff", 3, "Back-off restarting failed container"),
        ev("p/x", "BackOff", 5, 'Back-off pulling image "x"'),
        ev("p/x", "Failed", 7, 'Failed to pull image "x": connection refused'),
    ]
    assert stages.failure(events) == ("registry_unavailable", T0 + 5000)
    [entry] = stages.attribute(read(events))
    assert (entry["outcome"], entry["failure_ms"]) == ("registry_unavailable", 4000)
    assert stages.failure(events[:3]) == ("sandbox", T0 + 2000)
    assert stages.failure(events[:1]) == ("", None)
    waited = [ev("p/y", "FailedScheduling", 9)]
    assert stages.failure(waited) == ("unschedulable", T0 + 9000)


def test_time_lost_to_failed_pulls_is_charged_to_the_image_not_the_pod():
    events = [
        ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}"),
        ev("p/x", "Pulling", 1.2),
        ev("p/x", "Failed", 2, 'Failed to pull image "x": connection refused'),
        ev("p/x", "BackOff", 3, 'Back-off pulling image "x"'),
        ev("p/x", "Pulled", 40, 'Successfully pulled image "x" in 10s (10s including waiting)'),
        ev("p/x", "Started", 41),
    ]
    [entry] = stages.attribute(read(events))
    assert (entry["outcome"], entry["transient"]) == ("started", "registry_unavailable")
    assert entry["stages"]["image"] == 10000
    assert entry["stages"]["pull_retry"] == 28800
    assert entry["stages"]["pod_start"] == 1200
    row = stages.report([entry])["scenarios"]["warm-node/uncached-image"]
    assert row["image_init_ms"]["p50"] == 38800
    page = bench.markdown(stages.report([entry]), "s")
    assert "| 1 | 1 | none | registry_unavailable 1 |" in page


def test_a_failed_pull_without_a_recorded_attempt_or_success_has_no_retry_time():
    failed = [
        ev("p/x", "Scheduled", 1, f"Successfully assigned p/x to {OLD}"),
        ev("p/x", "Failed", 2, 'Failed to pull image "x": connection refused'),
        ev("p/x", "Pulled", 40, 'Successfully pulled image "x" in 10s'),
    ]
    assert stages.attribute(read(failed))[0]["stages"]["pull_retry"] == 0
    never = [ev("p/y", "Scheduled", 1, f"Successfully assigned p/y to {OLD}"), ev("p/y", "Pulling", 1)]
    assert stages.attribute(read(never))[0]["stages"]["pull_retry"] is None
    early = [
        ev("p/z", "Scheduled", 1, f"Successfully assigned p/z to {OLD}"),
        ev("p/z", "Pulling", 30),
        ev("p/z", "Failed", 31, 'Failed to pull image "x": EOF'),
        ev("p/z", "Pulled", 35, 'Successfully pulled image "x" in 10s'),
    ]
    assert stages.attribute(read(early))[0]["stages"]["pull_retry"] == 0


def test_instant_counts_milliseconds_from_the_epoch():
    assert stages.instant("1970-01-01T00:00:01Z") == 1000


def test_a_scale_up_at_the_wait_and_a_request_at_the_scale_up_still_cause_the_node():
    events = [
        ev("default/a", "FailedScheduling", 0),
        ev("default/a", "TriggeredScaleUp", 0, "pod triggered scale-up: [{anton-k3s-swarm 0->1 (max: 4)}]"),
        ev("default/a", "Scheduled", 70, f"Successfully assigned default/a to {NODE}"),
        ev("default/a", "Started", 72),
    ]
    [entry] = stages.attribute(read(events, requested=0))
    assert (entry["node_kind"], entry["from_zero"]) == ("cold", True)
    assert (entry["stages"]["trigger"], entry["stages"]["instance"]) == (0, 21000)


def test_a_pull_of_one_millisecond_is_uncached():
    [entry] = stages.attribute(
        read(warm_events(message='Successfully pulled image "x" in 1ms (1ms including waiting)'))
    )
    assert entry["image_cache"] == "uncached"


def test_a_failed_start_carries_no_transient_failure():
    events = [
        ev("anton-prod/web", "Scheduled", 1, f"Successfully assigned anton-prod/web to {OLD}"),
        ev("anton-prod/web", "Pulling", 1.5),
        ev("anton-prod/web", "Failed", 2, 'Failed to pull image "x": connection refused'),
    ]
    [entry] = stages.attribute(read(events))
    assert (entry["outcome"], entry["transient"]) == ("registry_unavailable", "")


def test_failure_and_backoff_events_without_a_message_are_not_pull_failures():
    events = [{"pod": "p/x", "reason": "Failed", "at": T0}, {"pod": "p/x", "reason": "BackOff", "at": T0}]
    assert stages.failure(events) == ("", None)


def test_empty_and_unsupported_claims_name_no_refused_samples():
    for samples, label, text in (
        ([], stages.COLD_LABEL, "no samples to claim"),
        ([{}], "fast", "unsupported claim label: fast"),
    ):
        with pytest.raises(ClaimRefused) as refused:
            stages.claim(samples, label)
        assert (str(refused.value), refused.value.refused) == (text, {})


def test_reduce_reads_listings_with_absent_fields():
    pod = {"kind": "Pod", "namespace": "d", "name": "z"}
    ready = {"type": "Ready", "status": "True", "lastTransitionTime": "2026-10-11T14:36:20Z"}
    raw = {
        "read_at": "2026-10-11T14:57:00Z",
        "events": {
            "items": [
                {"reason": "Scheduled", "firstTimestamp": "2026-10-11T14:36:08Z"},
                {"reason": "Failed", "eventTime": "2026-10-11T14:36:09Z", "involvedObject": pod},
            ]
        },
        "nodes": {"items": [{"metadata": {"name": "n"}, "spec": {}, "status": {}}]},
        "pods": {
            "items": [
                {"metadata": {"namespace": "d", "name": "bare", "creationTimestamp": "2026-10-11T14:36:00Z"}},
                {
                    "metadata": {"namespace": "d", "name": "ready", "creationTimestamp": "2026-10-11T14:36:00Z"},
                    "status": {"conditions": [ready]},
                },
            ]
        },
        "activities": {
            "Activities": [
                {"AutoScalingGroupName": "g", "StartTime": "2026-10-11T14:00:00Z", "StatusCode": "Successful"}
            ]
        },
        "instances": {"Reservations": []},
    }
    reduced = stages.reduce(raw)
    created = stages.instant("2026-10-11T14:36:00Z")
    assert reduced["events"] == [
        {"pod": "d/z", "reason": "Failed", "at": stages.instant("2026-10-11T14:36:09Z"), "message": ""}
    ]
    assert reduced["nodes"] == {"n": {"instance": "", "ready": None}}
    assert reduced["pods"] == {
        "d/bare": {"created": created, "ready": None},
        "d/ready": {"created": created, "ready": stages.instant("2026-10-11T14:36:20Z")},
    }
    assert reduced["launches"] == {}


def test_the_cli_is_named_and_needs_a_command():
    assert bench._parser().prog == "startup_bench"
    with pytest.raises(SystemExit) as exited:
        bench.main([])
    assert exited.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["reduce", "--output", "o.json"],
        ["reduce", "--capture", "c"],
        ["report", "--source", "s", "--output", "o.json"],
        ["report", "--read", "r.json", "--output", "o.json"],
        ["report", "--read", "r.json", "--source", "s"],
        ["claim", "--scenario", "cold-node/uncached-image"],
        ["claim", "--read", "r.json"],
    ],
)
def test_every_required_option_is_required(argv):
    with pytest.raises(SystemExit) as exited:
        bench.main(argv)
    assert exited.value.code == 2


def test_written_records_are_sorted_indented_json_in_new_folders(tmp_path):
    path = tmp_path / "a" / "b" / "record.json"
    bench._write(path, {"b": 1, "a": [1]})
    assert path.read_text() == '{\n  "a": [\n    1\n  ],\n  "b": 1\n}\n'


WORKER_LOG = "\n".join(
    (
        '2026-10-12T10:04:10.603104698Z {"event": "restore_verified", "recovery": "fresh"}',
        '2026-10-12T10:04:10.603630080Z {"worker_start": "prepared", "attempt": "exe-1", "uid": 10001}',
        "2026-10-12T10:04:11.100000000Z not a record",
        "2026-10-12T10:04:11.200000000Z 12",
        '2026-10-12T10:04:11.688520777Z {"supervisor": "prompt_delivered", "incarnation": "i1", "bytes": 1}',
        '2026-10-12T10:04:30.000000000Z {"supervisor": "prompt_delivered", "incarnation": "i2", "bytes": 1}',
    )
)


def test_worker_log_marks_run_from_the_prepared_report_to_the_first_prompt_delivery_after_it():
    assert stages.log_marks(WORKER_LOG) == {
        "prepared": stages.instant("2026-10-12T10:04:10.603630Z"),
        "delivered": stages.instant("2026-10-12T10:04:11.688520Z"),
    }


def test_a_prompt_delivery_before_the_prepared_report_or_without_one_is_not_a_handshake():
    early = '2026-10-12T10:04:09Z {"supervisor": "prompt_delivered"}\n2026-10-12T10:04:10Z {"worker_start": "prepared"}'
    assert stages.log_marks(early) == {"prepared": stages.instant("2026-10-12T10:04:10Z")}
    assert stages.log_marks('2026-10-12T10:04:11Z {"supervisor": "prompt_delivered"}') == {}
    assert stages.log_marks("") == {}


def test_a_restarted_attempt_times_its_handshake_from_its_own_prepared_report():
    restarted = "\n".join(
        (
            '2026-10-12T10:04:10Z {"worker_start": "prepared", "attempt": "exe-1"}',
            '2026-10-12T10:05:00Z {"worker_start": "prepared", "attempt": "exe-1"}',
            '2026-10-12T10:05:02Z {"supervisor": "prompt_delivered"}',
        )
    )
    assert stages.log_marks(restarted) == {
        "prepared": stages.instant("2026-10-12T10:05:00Z"),
        "delivered": stages.instant("2026-10-12T10:05:02Z"),
    }


def test_the_handshake_stage_is_the_worker_log_gap_and_absent_without_both_marks():
    events = warm_events("w/exe")
    pods = {"w/exe": {"created": T0, "ready": T0 + 9000, "prepared": T0 + 4000, "delivered": T0 + 5085}}
    [entry] = stages.attribute(read(events, pods))
    assert entry["stages"]["handshake"] == 1085 and entry["stages"]["bootstrap"] == 6500
    del pods["w/exe"]["delivered"]
    [entry] = stages.attribute(read(events, pods))
    assert entry["stages"]["handshake"] is None
    result = stages.report(stages.attribute(read(events, {"w/exe": {**pods["w/exe"], "delivered": T0 + 5085}})))
    assert result["scenarios"]["warm-node/cached-image"]["stages"]["handshake"]["p50"] == 1085


def test_a_prepared_worker_without_a_prompt_delivery_is_counted_undelivered():
    pods = {
        "w/a": {"created": T0, "ready": T0 + 9000, "prepared": T0 + 4000},
        "w/b": {"created": T0, "ready": T0 + 9000, "prepared": T0 + 4000, "delivered": T0 + 5000},
        "w/c": {"created": T0, "ready": T0 + 9000},
    }
    entries = stages.attribute(read(warm_events("w/a") + warm_events("w/b") + warm_events("w/c"), pods))
    assert [e["handshake_undelivered"] for e in entries] == [True, False, False]
    assert stages.report(entries)["scenarios"]["warm-node/cached-image"]["handshake_undelivered"] == 1


def test_reduce_joins_each_worker_log_to_its_pod():
    raw = raw_capture()
    raw["logs"] = {"default/a": WORKER_LOG, "default/gone": WORKER_LOG}
    reduced = stages.reduce(raw)
    assert reduced["pods"] == {
        "default/a": {
            "created": stages.instant("2026-10-11T14:36:08Z"),
            "ready": None,
            "prepared": stages.instant("2026-10-12T10:04:10.603630Z"),
            "delivered": stages.instant("2026-10-12T10:04:11.688520Z"),
        }
    }


def test_cli_reduce_reads_worker_logs_and_hashes_each(tmp_path):
    write_capture(tmp_path / "capture")
    (tmp_path / "capture" / "logs").mkdir()
    (tmp_path / "capture" / "logs" / "default_a.log").write_text(WORKER_LOG)
    assert bench.main(["reduce", "--capture", str(tmp_path / "capture"), "--output", str(tmp_path / "read.json")]) == 0
    reduced = json.loads((tmp_path / "read.json").read_text())
    assert sorted(reduced["inputs"]) == sorted((*bench.LISTINGS, "logs"))
    assert list(reduced["inputs"]["logs"]) == ["default_a.log"]
    assert len(reduced["inputs"]["logs"]["default_a.log"]) == 64
    assert reduced["pods"]["default/a"]["delivered"] - reduced["pods"]["default/a"]["prepared"] == 1085
    assert "Worker prepared to native agent ready and prompt delivered" in bench.markdown(stages.report([]), "empty")


def test_a_worker_log_name_splits_its_namespace_at_the_first_underscore_only(tmp_path):
    write_capture(tmp_path / "capture")
    pods = json.loads((tmp_path / "capture" / "pods.json").read_text())
    created = "2026-10-11T14:36:08Z"
    pods["items"].append({"metadata": {"namespace": "w", "name": "x_y_z", "creationTimestamp": created}})
    (tmp_path / "capture" / "pods.json").write_text(json.dumps(pods))
    (tmp_path / "capture" / "logs").mkdir()
    (tmp_path / "capture" / "logs" / "w_x_y_z.log").write_text(WORKER_LOG)
    reduced = bench.read(tmp_path / "capture")
    assert reduced["pods"]["w/x_y_z"]["delivered"] - reduced["pods"]["w/x_y_z"]["prepared"] == 1085
