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


def test_scale_ups_are_deduplicated_across_pods_and_ordered_per_group():
    message = "pod triggered scale-up: [{g1 4->7 (max: 20)} {g2 0->1 (max: 4)}]"
    events = [
        ev("p/a", "TriggeredScaleUp", 5, message),
        ev("p/b", "TriggeredScaleUp", 5, message),
        ev("p/c", "TriggeredScaleUp", 1, "pod triggered scale-up: [{g1 2->4 (max: 20)}]"),
    ]
    assert stages.scale_ups(events) == {"g1": [(T0 + 1000, 2), (T0 + 5000, 4)], "g2": [(T0 + 5000, 0)]}


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
    assert warm.count("n/a") == 6


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
