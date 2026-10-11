"""Attribute worker startup delay to node provision and image initialization stages from a read of the cluster."""

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from scripts.swarm_v2.kubernetes.failures import PULL_GRACE_MS
from scripts.swarm_v2.resource_profiles import ClaimRefused, distribution

STAGES = ("trigger", "instance", "join", "schedule", "image", "pod_start", "bootstrap")
NODE_STAGES = ("trigger", "instance", "join")
IMAGE_STAGES = ("image", "pod_start")
COLD_LABEL = "cold_scale_from_zero"
STARTED, PENDING = "started", "pending"
FAILURE_REASONS = ("Failed", "BackOff", "FailedCreatePodSandBox")
EXISTING, NEW_INSTANCE, WARM_POOL = "existing", "new_instance", "warm_pool"
CACHED, UNCACHED, PARTIAL, UNKNOWN = "cached", "uncached", "partial", "unknown"
REGISTRY_UNAVAILABLE, IMAGE_NOT_FOUND, UNAUTHORIZED = "registry_unavailable", "image_not_found", "registry_unauthorized"
IMAGE_PULL, SANDBOX, UNSCHEDULABLE = "image_pull", "sandbox", "unschedulable"
KEPT_REASONS = frozenset(
    (
        "Scheduled",
        "FailedScheduling",
        "TriggeredScaleUp",
        "Pulling",
        "Pulled",
        "Started",
        "Failed",
        "BackOff",
        "FailedCreatePodSandBox",
    )
)
MESSAGE_REASONS = frozenset(("Scheduled", "TriggeredScaleUp", "Pulled", "Failed", "BackOff"))
PULL_FAILURES = (
    (UNAUTHORIZED, re.compile(r"unauthorized|denied|forbidden|\b40[13]\b", re.I)),
    (IMAGE_NOT_FOUND, re.compile(r"not found|manifest unknown|name unknown", re.I)),
    (
        REGISTRY_UNAVAILABLE,
        re.compile(
            r"connection refused|no such host|i/o timeout|dial tcp|EOF|connection reset|\b50[234]\b|"
            r"service unavailable|handshake timeout|deadline exceeded",
            re.I,
        ),
    ),
)
LAUNCH = re.compile(r"Launching a new EC2 instance(?P<warm> from warm pool)?: (?P<id>i-[0-9a-f]+)$")
SCALE = re.compile(r"\{(?P<group>\S+) (?P<before>\d+)->(?P<after>\d+)")
PULL_TIME = re.compile(r" in (?:(?P<m>\d+)m)?(?:(?P<s>[\d.]+)s|(?P<ms>[\d.]+)ms)\b")
ASSIGNED = re.compile(r" to (?P<node>\S+)$")


def instant(text: str) -> int:
    return round(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def pull_ms(message: str) -> int | None:
    """Zero for an image already present on the node, None when the message carries no duration."""
    if "already present on machine" in message:
        return 0
    found = PULL_TIME.search(message)
    if not found:
        return None
    if found["ms"]:
        return round(float(found["ms"]))
    return round((int(found["m"] or 0) * 60 + float(found["s"])) * 1000)


def _ready_at(conditions: Iterable[Mapping]) -> int | None:
    for condition in conditions:
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return instant(condition["lastTransitionTime"])
    return None


def _event(item: Mapping) -> dict | None:
    if item.get("reason") not in KEPT_REASONS:
        return None
    when = item.get("eventTime") or item.get("firstTimestamp")
    target = item.get("involvedObject", {})
    if not when or target.get("kind") != "Pod":
        return None
    kept = {"pod": f"{target.get('namespace')}/{target.get('name')}", "reason": item["reason"], "at": instant(when)}
    if item["reason"] in MESSAGE_REASONS:
        kept["message"] = item.get("message", "")
    return kept


def reduce(raw: Mapping) -> dict:
    """Keep only the fields stage attribution reads from kubectl and aws listings, sorted for a stable record."""
    events = sorted(filter(None, map(_event, raw["events"]["items"])), key=lambda e: (e["pod"], e["at"], e["reason"]))
    nodes = {
        node["metadata"]["name"]: {
            "instance": node["spec"].get("providerID", "").rsplit("/", 1)[-1],
            "ready": _ready_at(node["status"].get("conditions", [])),
        }
        for node in raw["nodes"]["items"]
    }
    pods = {
        f"{pod['metadata']['namespace']}/{pod['metadata']['name']}": {
            "created": instant(pod["metadata"]["creationTimestamp"]),
            "ready": _ready_at(pod.get("status", {}).get("conditions", [])),
        }
        for pod in raw["pods"]["items"]
    }
    return {
        "read_at": raw["read_at"],
        "events": events,
        "nodes": dict(sorted(nodes.items())),
        "pods": dict(sorted(pods.items())),
        "launches": _launches(raw["activities"]["Activities"], raw["instances"]["Reservations"]),
    }


def _launches(activities: Sequence[Mapping], reservations: Sequence[Mapping]) -> dict:
    started = {i["InstanceId"]: instant(i["LaunchTime"]) for r in reservations for i in r["Instances"]}
    launches = {}
    for activity in sorted(activities, key=lambda a: a["StartTime"]):
        found = LAUNCH.search(activity.get("Description", ""))
        if found and activity.get("StatusCode") == "Successful":
            launches[found["id"]] = {
                "group": activity["AutoScalingGroupName"],
                "requested": instant(activity["StartTime"]),
                "launched": started.get(found["id"]),
                "source": WARM_POOL if found["warm"] else NEW_INSTANCE,
            }
    return dict(sorted(launches.items()))


def _first(events: Sequence[Mapping], reason: str) -> Mapping | None:
    return next((e for e in events if e["reason"] == reason), None)


def failure_class(events: Sequence[Mapping]) -> str:
    """Empty when the Pod's events show no start failure."""
    for event in events:
        if event["reason"] == "Failed" and "pull" in event.get("message", "").lower():
            return next((name for name, pattern in PULL_FAILURES if pattern.search(event["message"])), IMAGE_PULL)
    if any(e["reason"] == "BackOff" and "pulling image" in e.get("message", "") for e in events):
        return IMAGE_PULL
    if _first(events, "FailedCreatePodSandBox"):
        return SANDBOX
    if not _first(events, "Scheduled") and _first(events, "FailedScheduling"):
        return UNSCHEDULABLE
    return ""


def _cache(pulls: Sequence[int]) -> str:
    if not pulls:
        return UNKNOWN
    if all(ms == 0 for ms in pulls):
        return CACHED
    return UNCACHED if all(ms > 0 for ms in pulls) else PARTIAL


def scale_ups(events: Iterable[Mapping]) -> dict[str, list[tuple[int, int]]]:
    """Each group's scale ups as (time, size before), deduplicated across the Pods that recorded the same one."""
    seen: dict[str, set[tuple[int, int]]] = {}
    for event in events:
        if event["reason"] == "TriggeredScaleUp":
            for found in SCALE.finditer(event["message"]):
                seen.setdefault(found["group"], set()).add((event["at"], int(found["before"])))
    return {group: sorted(ups) for group, ups in sorted(seen.items())}


def _gap(later: int | None, earlier: int | None) -> int | None:
    return None if later is None or earlier is None else later - earlier


def _node_stages(events, scheduled, node, launch, scale_ups) -> tuple[dict, bool]:
    """The serving instance counts as provisioned for this Pod only when it was requested after the Pod waited; its
    cause is the latest scale up of its group at or before the request, whichever Pod's event recorded it."""
    waited = _first(events, "FailedScheduling")
    if not (waited and launch and launch["requested"] >= waited["at"] and node.get("ready")):
        return {}, False
    causes = [up for up in scale_ups.get(launch["group"], ()) if up[0] <= launch["requested"]]
    if not causes:
        return {}, False
    scaled, before = causes[-1]
    stages = {
        "trigger": max(scaled - waited["at"], 0),
        "instance": _gap(launch["launched"], scaled),
        "join": _gap(node["ready"], launch["launched"]),
        "schedule": _gap(scheduled, node["ready"]),
    }
    return stages, before == 0


def _image_stages(events, scheduled, pod) -> tuple[dict, list[int]]:
    pulls = [ms for e in events if e["reason"] == "Pulled" for ms in [pull_ms(e["message"])] if ms is not None]
    started = max((e["at"] for e in events if e["reason"] == "Started"), default=None)
    image = sum(pulls) if pulls else None
    start = _gap(started, scheduled)
    stages = {
        "image": image,
        "pod_start": None if start is None else max(start - (image or 0), 0),
        "bootstrap": _gap(pod.get("ready"), started) if started and (pod.get("ready") or 0) >= started else None,
    }
    return stages, pulls


def sample(name: str, events: Sequence[Mapping], read: Mapping, ups: Mapping) -> dict | None:
    """None when the read holds no scheduling or failure evidence for the Pod; a failure the Pod started past is kept
    as `transient` rather than as its outcome."""
    placed = _first(events, "Scheduled")
    failure = failure_class(events)
    if not placed and not failure:
        return None
    started = _first(events, "Started") is not None
    node_name = ASSIGNED.search(placed["message"])["node"] if placed else ""
    node = read["nodes"].get(node_name, {})
    launch = read["launches"].get(node.get("instance", ""))
    scheduled = placed["at"] if placed else None
    stages, from_zero = _node_stages(events, scheduled, node, launch, ups)
    cold = bool(stages)
    if not cold:
        stages = {"schedule": _gap(scheduled, read["pods"].get(name, {}).get("created"))}
    image, pulls = _image_stages(events, scheduled, read["pods"].get(name, {}))
    stages.update(image)
    begun = scheduled if placed else _first(events, "Pulling")["at"] if _first(events, "Pulling") else None
    failed = next((e["at"] for e in events if e["reason"] in FAILURE_REASONS), None)
    return {
        "pod": name,
        "node": node_name,
        "node_kind": "cold" if cold else "warm",
        "node_source": launch["source"] if cold else EXISTING,
        "from_zero": from_zero,
        "image_cache": _cache(pulls),
        "outcome": STARTED if started else failure or PENDING,
        "transient": failure if started else "",
        "sandbox_retries": sum(1 for e in events if e["reason"] == "FailedCreatePodSandBox"),
        "failure_ms": _gap(failed, begun) if failure else None,
        "stages": {stage: stages.get(stage) for stage in STAGES},
    }


def attribute(read: Mapping) -> list[dict]:
    grouped: dict[str, list[Mapping]] = {}
    for event in read["events"]:
        grouped.setdefault(event["pod"], []).append(event)
    ups = scale_ups(read["events"])
    found = (sample(name, events, read, ups) for name, events in sorted(grouped.items()))
    return [entry for entry in found if entry]


def scenario(entry: Mapping) -> str:
    return f"{entry['node_kind']}-node/{entry['image_cache']}-image"


def _total(entry: Mapping, stages: Sequence[str]) -> int | None:
    values = [entry["stages"][stage] for stage in stages]
    return None if None in values else sum(values)


def _spread(values: Iterable[int | None]) -> dict | None:
    present = [v for v in values if v is not None]
    return distribution(present) if present else None


def summarize(group: Sequence[Mapping]) -> dict:
    started = [e for e in group if e["outcome"] == STARTED]
    failed = [e for e in group if e["outcome"] not in (STARTED, PENDING)]
    stages = {stage: _spread(e["stages"][stage] for e in started) for stage in STAGES}
    return {
        "samples": len(group),
        "started": len(started),
        "pending": sum(1 for e in group if e["outcome"] == PENDING),
        "failures": dict(sorted(Counter(e["outcome"] for e in failed).items())),
        "transient": dict(sorted(Counter(e["transient"] for e in started if e["transient"]).items())),
        "failure_ms": _spread(e["failure_ms"] for e in group),
        "from_zero": sum(1 for e in group if e["from_zero"]),
        "node_sources": dict(sorted(Counter(e["node_source"] for e in group).items())),
        "stages": {stage: spread for stage, spread in stages.items() if spread},
        "node_provision_ms": _spread(_total(e, NODE_STAGES) for e in started),
        "image_init_ms": _spread(_total(e, IMAGE_STAGES) for e in started),
    }


def report(samples: Sequence[Mapping]) -> dict:
    """`startup_stage_p50_p95` per scenario, with failures counted beside the starts rather than dropped."""
    grouped: dict[str, list[Mapping]] = {}
    for entry in samples:
        grouped.setdefault(scenario(entry), []).append(entry)
    return {
        "metric": "startup_stage_p50_p95",
        "unit": "ms",
        "scenarios": {name: summarize(group) for name, group in sorted(grouped.items())},
    }


def _cold_refusal(entry: Mapping) -> str:
    if entry["node_kind"] != "cold":
        return "the Pod landed on a node that existed before it waited"
    if not entry["from_zero"]:
        return "the scale up did not start from an empty pool"
    if entry["image_cache"] != UNCACHED:
        return f"the image cache was {entry['image_cache']}"
    return ""


def claim(samples: Sequence[Mapping], label: str) -> list[Mapping]:
    """Only samples that scaled an empty pool onto a new node and pulled uncached may carry the cold label."""
    if label != COLD_LABEL:
        raise ClaimRefused(f"unsupported claim label: {label}", {})
    if not samples:
        raise ClaimRefused("no samples to claim", {})
    refused = {e["pod"]: reason for e in samples for reason in [_cold_refusal(e)] if reason}
    if refused:
        raise ClaimRefused("a warm sample cannot be advertised as cold scale from zero", refused)
    return list(samples)


def unbounded(samples: Sequence[Mapping], grace_ms: int = PULL_GRACE_MS) -> list[str]:
    """Pull failures, final or transient, classified later than the grace or never timed are unbounded."""
    pulls = (REGISTRY_UNAVAILABLE, IMAGE_NOT_FOUND, UNAUTHORIZED, IMAGE_PULL)
    return [
        e["pod"]
        for e in samples
        if (e["outcome"] in pulls or e.get("transient") in pulls)
        and (e["failure_ms"] is None or e["failure_ms"] > grace_ms)
    ]
