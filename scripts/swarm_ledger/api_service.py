"""Ledger API service: the one process that takes ledger writes over HTTP, the ledger page's writes routed to it by the
public host, and applies them through the ledger API route code over the shared database. It runs the schema
migration and the write side upkeep: bin purge, binning closed ledgers without a swarm, alert expiry, the inbox relay
replay and the outbox relay. Swarm, agent and service clients run the same route code in process through
scripts.tick.adapters.database_ledger; the ledger server only reads.

Usage:
  python -m scripts.swarm_ledger.api_service --serve

Env: AGENTIHOOKS_SWARM_DATABASE_URL, LEDGER_DIR, LEDGER_API_HOST (127.0.0.1), LEDGER_API_PORT (8766),
LEDGER_TLS_CERT and LEDGER_TLS_KEY to serve https, SWARM_PUBLIC_URL and SWARM_ALLOWED_HOSTS for the hosts and
origins it answers.
"""

import argparse
import functools
import itertools
import json
import os
import re
import signal
import ssl
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.gates import talk as talk_budget
from scripts.swarm_ledger import ledger_alerts as alerts_module
from scripts.swarm_ledger import ledger_blockers as blockers_module
from scripts.swarm_ledger import ledger_server as served
from scripts.swarm_ledger import ledger_task_duplicates as duplicates_module
from scripts.swarm_ledger.api.services import HttpPolicy, Services

server: Any = cast(Any, served)
core: Any = server.core
authority: Any = server.authority
ledger_alerts: Any = alerts_module
ledger_artifacts: Any = server.ledger_artifacts
ledger_bin: Any = server.ledger_bin
ledger_blockers: Any = blockers_module
ledger_layout: Any = server.ledger_layout
ledger_media: Any = server.ledger_media
ledger_task_duplicates: Any = duplicates_module
ports: Any = server.ports
request_metrics: Any = server.request_metrics
storage: Any = server.storage
talk: Any = talk_budget
BaseHandler: Any = server.Handler

HOST = os.environ.get("LEDGER_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("LEDGER_API_PORT", "8766"))
UPKEEP_S = 2.0
BIN_SWEEP_EVERY = 15
PROBE_FILE = ".healthz"
TICK_HEADER = "X-Ledger-Tick"
CONTROLS = {
    "start": ["start"],
    "pause": ["pause"],
    "stop": ["stop"],
    "stop_now": ["stop", "--now"],
    "close": ["close"],
    "reopen": ["reopen"],
}
DOCTOR = {"doctor_start": ["start"], "doctor_stop": ["stop"]}
DOCTOR_PHRASE = "rig doctor stop"
AUTONOMY = ("manual", "assist", "delegate", "full")
SCALING = ("auto", "manual")
MAX_LOAD = 10.0
MASTER_AGENTS = ("claude", "codex")
MAX_NOTE = 500
FINDING_RE = re.compile(r"^[a-z][a-z-]*/[\w.-]{1,64}$")
TRIMMED = (
    "Operator writes from revision {since} through {trimmed} left the ledger history before the inbox could take "
    "them during an outage, so no agent received them. Write them again if they still matter."
)
RELAY, ALERTS = "relay", "alerts"
UNRELAYED = "ledger-unrelayed.json"
REPLAY_EVERY = 5


def model_pairs(body: dict[str, Any]) -> list[str]:
    """The model settings a set control carries, refused when the catalog does not offer them."""
    default_models: Any = ports.swarm().default_models
    changes = {key: body[key] for key in default_models.FIELDS.values() if key in body}
    if not changes:
        return []
    if refused := default_models.refusal(changes, server.swarm_models()):
        raise ValueError(refused)
    return [f"{key}={changes[field]}" for key, field in default_models.KEYS.items() if field in changes]


def control_argv(body: Any) -> list[str]:
    """The swarm command arguments one page control asks for, refused when its body is malformed."""
    asked = cast(dict[str, Any], body) if isinstance(body, dict) else {}
    action = asked.get("action")
    if action in CONTROLS:
        return CONTROLS[action]
    if action == "terminate":
        name = asked.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9@._-]{1,200}", name) or name.startswith("-"):
            raise ValueError("terminate needs an exact agent name")
        return ["terminate", name]
    builders: dict[str, Callable[[dict[str, Any]], list[str]]] = {
        "verdict": verdict_argv,
        "restore-decision": restore_decision_argv,
        "lift": lift_argv,
        "master_move": master_move_argv,
    }
    if action in builders:
        return builders[action](asked)
    if action != "set":
        raise ValueError("action must be start, pause, stop, stop_now, close, reopen, set or verdict")
    pairs: list[str] = []
    for key, (flag, low, limit) in server.CAPS.items():
        value = asked.get(key)
        if value is None:
            continue
        if type(value) is not int or not low <= value <= limit:
            raise ValueError(f"{key} must be a whole number from {low} to {limit}")
        pairs.append(f"{flag}={value}")
    for key, flag in (("effort_min", "effort-min"), ("effort_max", "effort-max")):
        if key in asked:
            if asked[key] not in server.EFFORTS:
                raise ValueError(f"{key} must be one of {', '.join(server.EFFORTS)}")
            pairs.append(f"{flag}={asked[key]}")
    if "autonomy" in asked:
        if asked["autonomy"] not in AUTONOMY:
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY)}")
        pairs.append(f"autonomy={asked['autonomy']}")
    pairs += scaling_pairs(asked)
    pairs += model_pairs(asked)
    if "gates" in asked:
        pairs += gate_pairs(asked["gates"])
    if "master_agent" in asked:
        if asked["master_agent"] not in MASTER_AGENTS:
            raise ValueError(f"master_agent must be one of {', '.join(MASTER_AGENTS)}")
        pairs.append(f"master-agent={asked['master_agent']}")
    if "overlays" in asked:
        pairs += overlay_pairs(asked["overlays"])
    if not pairs:
        raise ValueError(
            "set needs max_eng, max_ci, max_plan, masters_step, compact_limit, effort_min, effort_max, autonomy, "
            "scaling, load_high, load_low, memory_per_agent_mb, claude_model, codex_model, master_agent, overlays or gates"
        )
    return ["set", *pairs]


def effort_refusal(slug: str, body: dict[str, Any]) -> str | None:
    """Why a set control's effort range does not fit the swarm's lanes, or None."""
    if body.get("action") != "set" or not {"effort_min", "effort_max"} & set(body):
        return None
    from redis import RedisError

    swarm: Any = ports.swarm()
    try:
        config: Any = server.swarm_store().config(slug)
    except (swarm.store.SwarmError, RedisError):
        return None
    bounds = (body.get("effort_min", config.effort_min), body.get("effort_max", config.effort_max))
    refused: str | None = swarm.effort_range.refusal(bounds, config.lanes)
    return refused


def scaling_pairs(body: dict[str, Any]) -> list[str]:
    """The scaling settings a set control carries, refused when out of range."""
    pairs: list[str] = []
    if "scaling" in body:
        if body["scaling"] not in SCALING:
            raise ValueError(f"scaling must be one of {', '.join(SCALING)}")
        pairs.append(f"scaling={body['scaling']}")
    for key, flag in (("load_high", "load-high"), ("load_low", "load-low")):
        if key in body:
            value = body[key]
            if type(value) not in (int, float) or not 0 < value <= MAX_LOAD:
                raise ValueError(f"{key} must be a number above 0 and at most {MAX_LOAD:g}")
            pairs.append(f"{flag}={value}")
    if body.get("load_low", 0) > body.get("load_high", MAX_LOAD):
        raise ValueError("load_low must be at most load_high")
    if "memory_per_agent_mb" in body:
        value = body["memory_per_agent_mb"]
        if type(value) is not int or value <= 0:
            raise ValueError("memory_per_agent_mb must be a whole number of MB above 0")
        pairs.append(f"memory-per-agent={value}")
    return pairs


def overlay_pairs(overlays: Any) -> list[str]:
    """The overlay settings a set control carries, refused when a role or name is malformed."""
    overlay_cap: int = ports.hooks().profile_chain.OVERLAY_CAP
    key: str = ports.swarm().overlays.KEY
    roles: tuple[str, ...] = ports.swarm().overlays.ROLES
    name = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
    if (
        not isinstance(overlays, dict)
        or not overlays
        or not all(
            role in roles
            and isinstance(names, list)
            and len(set(cast(list[Any], names))) == len(cast(list[Any], names))
            and all(isinstance(n, str) and name.fullmatch(n) for n in cast(list[Any], names))
            for role, names in cast(dict[Any, Any], overlays).items()
        )
    ):
        raise ValueError(f"overlays maps a base role of {', '.join(roles)} to a list of distinct overlay names")
    chosen = cast(dict[str, list[str]], overlays)
    for role, names in chosen.items():
        if len(names) > overlay_cap:
            raise ValueError(f"a role wears at most {overlay_cap} overlays; {role} was given {len(names)}")
    return [f"{key}{role}={','.join(chosen[role])}" for role in roles if role in chosen]


def gate_pairs(gates: Any) -> list[str]:
    """The gate modes a set control carries, refused when a gate or mode is unknown."""
    from scripts.gates import catalog, modes

    names = catalog.defaults()
    if (
        not isinstance(gates, dict)
        or not gates
        or not all(
            n in names and isinstance(m, str) and modes.normalize(m) in modes.supported(n)
            for n, m in cast(dict[Any, Any], gates).items()
        )
    ):
        raise ValueError(
            f"gates maps a gate of {', '.join(names)} to {', '.join(modes.label(mode) for mode in modes.MODES)}"
        )
    return [f"{name}-gate={mode}" for name, mode in cast(dict[str, str], gates).items()]


def restore_decision_argv(body: dict[str, Any]) -> list[str]:
    """The restore decision command for one agent."""
    agent, choice = body.get("agent"), body.get("choice")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A restore decision needs an agent name")
    if choice not in {"resume", "native", "context", "fresh"}:
        raise ValueError("Choose native, context, fresh or resume")
    return ["restore-decision", agent, choice]


def lift_argv(body: dict[str, Any]) -> list[str]:
    """The gate lift command for one agent."""
    agent, gate = body.get("agent"), body.get("gate")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A lift needs an agent name")
    if not isinstance(gate, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", gate):
        raise ValueError("A lift needs the gate's name")
    return ["lift", agent, gate]


def master_move_argv(body: dict[str, Any]) -> list[str]:
    """The master move command to one hive."""
    hive_id: Any = ports.hive().registry.HIVE_ID
    hive = body.get("hive")
    if not isinstance(hive, str) or not hive_id.fullmatch(hive):
        raise ValueError("A master move needs a hive id")
    return ["master", "move", "--to", hive]


def verdict_argv(body: dict[str, Any]) -> list[str]:
    """The operator's verdict command on one health finding."""
    verdicts: Any = ports.swarm().verdicts.VERDICTS
    finding, value, note = body.get("id"), body.get("verdict"), body.get("note", "")
    if not (isinstance(finding, str) and FINDING_RE.match(finding)):
        raise ValueError("id must name a finding, kind/subject")
    if value not in verdicts:
        raise ValueError(f"verdict must be one of {', '.join(verdicts)}")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError(f"note must be text of at most {MAX_NOTE} characters")
    return ["--as", "operator", "verdict", finding, str(value), f"--note={note}"]


def bin_request(body: Any) -> tuple[str, str]:
    """The bin action and ledger one legacy bin request names."""
    if not isinstance(body, dict) or cast(dict[str, Any], body).get("action") not in ("delete", "restore"):
        raise ValueError("action must be delete or restore")
    request = cast(dict[str, Any], body)
    if not isinstance(request.get("slug"), str):
        raise ValueError("slug must be a string")
    return request["action"], request["slug"]


def terminate_control(slug: str, name: str) -> tuple[dict[str, Any] | None, str]:
    """Terminate one agent of the swarm, or say why not."""
    store: Any = server.swarm_store()
    if not any(agent.name == name for agent in store.agents(slug)):
        return None, "agent is not in this swarm"
    ports.swarm().commands.submit(store, slug, "swarm", ["terminate", name])
    return server.swarm_status(slug), ""


def swarm_control(slug: str, argv: list[str], command: str = "swarm") -> tuple[dict[str, Any] | None, str]:
    """Submit one swarm command for the page, answering the swarm status or the refusal."""
    swarm: Any = ports.swarm()
    try:
        if command == "swarm" and argv[0] == "terminate":
            return terminate_control(slug, argv[1])
        swarm.commands.submit(server.swarm_store(), slug, command, argv)
        return server.swarm_status(slug), ""
    except swarm.store.SwarmError as exc:
        return None, str(exc)


def lifecycle_control(slug: str, body: dict[str, Any], operator: bool) -> tuple[dict[str, Any] | None, str]:
    """Request one lifecycle change of the swarm, answering the swarm status or the refusal."""
    swarm: Any = ports.swarm()
    try:
        stamps = server.repository.read(slug, "_meta.stamps").get("_meta", {}).get("stamps", {})
    except KeyError:
        return None, "no such ledger"
    try:
        swarm.lifecycle.request(
            server.swarm_store(), slug, body, operator=operator, task_revisions=swarm.lifecycle.revisions(stamps)
        )
    except swarm.store.SwarmError as exc:
        return None, str(exc)
    return server.swarm_status(slug), ""


def refresh_quota(slug: str) -> tuple[dict[str, Any] | None, str]:
    """Request a quota refresh for the swarm, answering the swarm status or the refusal."""
    swarm: Any = ports.swarm()
    try:
        swarm.quota_refresh.request(server.swarm_store(), slug)
    except swarm.store.SwarmError as exc:
        return None, str(exc)
    return server.swarm_status(slug), ""


def relay_to_inbox(slug: str, state: dict[str, Any]) -> list[Any]:
    """Operator writes since the oldest revision not yet relayed become inbox items for the swarm on this ledger."""
    meta = state["_meta"]
    since = unrelayed_since(RELAY, slug, meta["rev"])
    if since <= meta.get("events_trimmed", -1):
        since = record_trimmed(slug, since, meta["events_trimmed"])
    events = [
        e for e in meta.get("events", []) if since <= e.get("rev", -1) <= meta["rev"] and e.get("by") == "operator"
    ]
    if not events:
        relayed(RELAY, slug, since, meta["rev"])
        return []
    try:
        from scripts.inbox.store import connect
        from scripts.swarm_ledger import chat_receipts, watch_ledger

        swarm: Any = ports.swarm()
        inbox: Any = connect()
        store: Any = swarm.store.RedisStore(inbox.redis)
        sent: list[Any] = swarm.operator_mail.relay(inbox, store, slug, state, events, cast(Any, watch_ledger).line)
        addresses = [swarm.masters.lead_of(slug, []), *swarm.masters.held(store.redis, slug, store.agents(slug))]
        cast(Any, chat_receipts).post(server.repository, inbox, slug, events, addresses)
    except Exception as exc:
        sys.stderr.write(f"inbox relay for {slug}: {exc}\n")
        unrelayed_on(RELAY, slug, meta["rev"])
        return []
    relayed(RELAY, slug, since, meta["rev"])
    return sent


def record_trimmed(slug: str, since: int, trimmed: int) -> int:
    """Tell the operator which unrelayed writes the event window dropped; the relay resumes after them."""
    notice = {"op": "notice", "id": f"trimmed-{since}-{trimmed}", "by": "ledger", "text": TRIMMED.format(**locals())}
    try:
        _, rejected = server.repository.apply_ops(slug, ops=[notice])
    except Exception as exc:
        sys.stderr.write(f"trimmed notice for {slug}: {exc}\n")
        return since
    if rejected:
        return since
    relayed(RELAY, slug, since, trimmed)
    return trimmed + 1


def deliver_alerts(slug: str, state: dict[str, Any]) -> list[Any]:
    """Open alerts since the oldest revision not yet delivered reach their agents and the master."""
    meta = state["_meta"]
    since = unrelayed_since(ALERTS, slug, meta["rev"])
    fresh = [
        a
        for a in state.get("alerts", [])
        if since <= a.get("rev", -1) <= meta["rev"] and a["state"] == ledger_alerts.OPEN
    ]
    if not fresh:
        relayed(ALERTS, slug, since, meta["rev"])
        return []
    try:
        from scripts.inbox.store import connect

        swarm: Any = ports.swarm()
        inbox: Any = connect()
        agents = swarm.store.RedisStore(inbox.redis).agents(slug)
        live = [a for a in agents if a.state != "finished"]
        master = swarm.operator_mail.master_address(inbox.redis, slug, agents)
        addresses = {a.name: a.seat or a.name for a in live}
        alerts = [{**a, "target": addresses.get(a["target"], "master")} for a in fresh]
        render = functools.partial(ledger_alerts.message, tier=ledger_blockers.autonomy(slug), doc=state)
        sent: list[Any] = ledger_alerts.deliver(inbox, slug, alerts, meta["rev"], master, since=since, render=render)
    except Exception as exc:
        sys.stderr.write(f"alert delivery for {slug}: {exc}\n")
        unrelayed_on(ALERTS, slug, meta["rev"])
        return []
    relayed(ALERTS, slug, since, meta["rev"])
    return sent


def unrelayed() -> dict[str, Any]:
    """The revisions held for a relay replay, by kind and ledger."""
    try:
        held: dict[str, Any] = server.stored.held_revisions()
    except (storage.Error, OSError):
        return {}
    return held


def unrelayed_since(kind: str, slug: str, rev: int) -> int:
    """The oldest revision of ``slug`` still owed a ``kind`` relay, at most ``rev``."""
    return min([rev, *unrelayed().get(kind, {}).get(slug, [])])


def unrelayed_on(kind: str, slug: str, rev: int) -> None:
    """Keep the revision for replay whatever stopped the inbox from taking it."""
    server.stored.hold_revision(kind, slug, rev)


def relayed(kind: str, slug: str, since: int, rev: int) -> None:
    """Forget the revisions a relay from since through rev covered; one recorded meanwhile outside it stays."""
    if unrelayed().get(kind, {}).get(slug):
        server.stored.release_revisions(kind, slug, since, rev)


def adopt_unrelayed() -> None:
    """Hold again the revisions an earlier server recorded in its unrelayed file, then drop the file."""
    config: Any = ports.hooks().config
    path = Path(config.AGENTIHOOKS_HOME) / UNRELAYED
    try:
        found: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for kind, slugs in cast(dict[str, Any], found).items() if isinstance(found, dict) else ():
        for slug, revisions in cast(dict[str, Any], slugs).items() if isinstance(slugs, dict) else ():
            for rev in cast(list[Any], revisions) if isinstance(revisions, list) else ():
                if isinstance(rev, int):
                    unrelayed_on(kind, slug, rev)
    path.unlink()


def replay_unrelayed() -> None:
    """Relay again every revision the inbox could not take, from the stored ledger, once Redis answers."""
    replays: dict[str, Callable[[str, dict[str, Any]], list[Any]]] = {RELAY: relay_to_inbox, ALERTS: deliver_alerts}
    for kind, slugs in unrelayed().items():
        for slug in list(slugs):
            try:
                state = server.repository.get_document(slug)
            except KeyError:
                server.stored.drop_revisions(kind, slug)
                continue
            replays[kind](slug, state)


def expire_alerts() -> None:
    """Write each ledger again whose alerts passed their expiry, so the write expires them."""
    at = core.now_ms()
    for summary in server.ledger_summaries():
        slug = summary["slug"]
        state = server.repository.read(slug, "alerts")
        if any(ledger_alerts.expired(alert, at) for alert in state.get("alerts", [])):
            server.repository.apply_ops(slug)


def doctor_phrase(slug: str, state: dict[str, Any]) -> None:
    """Stop the Doctor when the operator's chat line in this write says its stop phrase."""
    meta = state["_meta"]
    said = [
        e
        for e in meta.get("events", [])
        if e.get("rev") == meta["rev"] and e.get("by") == "operator" and e.get("target") == "chat"
    ]
    if any(e.get("text", "").strip().lower() == DOCTOR_PHRASE for e in said):
        swarm_control(slug, ["stop"], "doctor")


def write_gate(slug: str) -> Any:
    """Every op write: masters fenced to the items they own, page chat to the front door, asks of the operator behind
    the blocker protocol, inside the talk budget."""
    swarm: Any = ports.swarm()
    protocol = ledger_blockers.Gate(slug, talk.Budget(slug))
    return swarm.master_fence.Fence(slug, swarm.front_door.Door(slug, protocol))


def bin_closed_without_swarm(now: int | None = None) -> None:
    """Bin every closed ledger whose swarm is gone."""
    swarm: Any = ports.swarm()
    try:
        store: Any = server.swarm_store()
        for summary in server.ledger_summaries():
            if not summary["closed_at"]:
                continue
            try:
                store.config(summary["slug"])
            except swarm.store.SwarmError:
                ledger_bin.bin_closed(summary["slug"], summary["closed_at"], now)
    except Exception as exc:
        sys.stderr.write(f"bin closed ledgers: {exc}\n")


def adopt_disk_files() -> None:
    """Adopt the media and artifact files an earlier folder server left on disk into the store."""
    for folder in sorted(core.LEDGER_DIR.glob("*.media")):
        slug = folder.name.removesuffix(".media")
        try:
            ledger_artifacts.adopt_disk(slug)
        except Exception as exc:
            sys.stderr.write(f"disk adoption {slug}: {exc}\n")


def services() -> Services:
    """The ledger API services with every write hook, over the shared repository."""
    from scripts.swarm_ledger import services as built

    ledger: Any = built.Ledger(
        core=core,
        authority=authority,
        hub=server.HUB,
        layout_rows=ledger_layout.ROWS,
        layout_lists=ledger_layout.LISTS,
        home_summaries=server.home_summaries,
        bin_summaries=server.bin_summaries,
        bin_entries=ledger_bin.entries,
        bin_ledger=ledger_bin.delete,
        workspace_tails=server.workspace_tails,
        stream_resources=server.stream_resources,
        layout=ledger_layout.read,
        write_layout=ledger_layout.write,
        write_gate=write_gate,
        relay_to_inbox=relay_to_inbox,
        deliver_alerts=deliver_alerts,
    )
    swarm: Any = built.Swarm(
        doctor=DOCTOR,
        store=server.swarm_store,
        status=server.swarm_status,
        models=server.swarm_models,
        control=swarm_control,
        lifecycle=lifecycle_control,
        refresh_quota=refresh_quota,
        control_argv=control_argv,
        doctor_phrase=doctor_phrase,
    )
    media: Any = built.Media(
        media_store=ledger_media,
        artifact_store=ledger_artifacts,
        resolve_media=ledger_media.resolve,
        resolve_artifacts=ledger_artifacts.resolve,
    )
    policy = HttpPolicy(
        allowed_hosts=server.ALLOWED_HOSTS, allowed_origins=server.ALLOWED_ORIGINS, max_body=server.MAX_BODY
    )
    return Services(repository=server.repository, ledger=ledger, swarm=swarm, media=media, policy=policy)


class Writes(ABC):
    """The ledger write handlers: served over HTTP by the API service and run in process by every ledger client."""

    headers: Message
    rfile: Any
    principal: str | None = None
    tls: ssl.SSLContext | None = None

    @abstractmethod
    def send(self, code: int, body: str, ctype: str) -> None:
        """Send one reply; the HTTP handler and the in process exchange each define how."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, slug: str) -> bool:
        """Whether the ledger exists; defined by the HTTP handler and the in process exchange."""
        raise NotImplementedError

    @abstractmethod
    def refused(self, slug: str | None = None) -> bool:
        """Refuse a caller the ledger does not admit, setting the principal of one it does."""
        raise NotImplementedError

    def reply_state(
        self,
        slug: str,
        changes: list[Any] | None = None,
        ops: list[dict[str, Any]] | None = None,
        refusals: dict[str, str] | None = None,
        screen: Any = None,
    ) -> None:
        """Apply a page save and answer with a bounded acknowledgment; the page takes state from the event stream."""
        refusals = refusals or {}
        screen = screen or ledger_task_duplicates.Screen()
        gate = write_gate(slug) if ops else None
        try:
            state, rejected = server.repository.apply_ops(
                slug,
                changes=changes,
                ops=ops,
                gate=ledger_task_duplicates.Gate(screen, gate) if screen.refused else gate,
            )
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
        if gate:
            gate.publish(rejected)
        if changes or ops:
            relay_to_inbox(slug, state)
            doctor_phrase(slug, state)
        deliver_alerts(slug, state)
        warnings = [*state["_meta"].get("warnings", []), *refusals.values(), *screen.warnings.values()]
        reply = {
            "applied": [op["id"] for op in ops or [] if op["id"] not in rejected],
            "rejected": [*rejected, *refusals],
            "_meta": {"rev": state["_meta"]["rev"], "warnings": [warning[:1000] for warning in warnings[:20]]},
        }
        return self.send(200, json.dumps(reply, ensure_ascii=False), "application/json")

    def put_swarm(self, slug: str) -> None:
        """Apply one swarm panel control from the operator's page."""
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if self.refused(slug):
            return None
        if self.principal:
            return self.send(403, "swarm controls need the operator", "text/plain")
        control: Callable[[], tuple[dict[str, Any] | None, str]]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= server.MAX_BODY:
                raise ValueError("body size out of range")
            loaded: Any = core.loads(self.rfile.read(length) or b"{}")
            body = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
            action = body.get("action")
            if action == "lifecycle":
                control = functools.partial(lifecycle_control, slug, body, not self.principal)
            elif action == "quota_refresh":
                control = functools.partial(refresh_quota, slug)
            else:
                command, argv = ("doctor", DOCTOR[action]) if action in DOCTOR else ("swarm", control_argv(body))
                if refused := effort_refusal(slug, body):
                    raise ValueError(refused)
                control = functools.partial(swarm_control, slug, argv, command)
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        refusal: Any = ports.swarm().lifecycle.Refused
        try:
            status, error = control()
        except refusal as refused:
            return self.send(refused.status, str(refused), "text/plain")
        if error:
            return self.send(502, error, "text/plain")
        return self.send(200, json.dumps(status), "application/json")

    def put_layout(self) -> None:
        """Save the page layout sizes."""
        if self.headers.get("Origin") not in server.ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= server.MAX_BODY:
                raise ValueError("body size out of range")
            saved = ledger_layout.write(ledger_layout.loads(self.rfile.read(length)))
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        return self.send(200, json.dumps(saved), "application/json")

    def post_media(self, slug: str) -> None:
        """Store one uploaded image on the ledger."""
        return self.receive(slug, ledger_media.MAX_BYTES, lambda data: ledger_media.store(slug, data))

    def post_artifact(self, slug: str) -> None:
        """Store one artifact file an agent or the operator's plan publishes on the ledger."""
        name = self.headers.get("X-Artifact-Name", "")
        request: Any
        try:
            request = core.loads(self.headers.get("X-Artifact-Request", "{}"))
        except (ValueError, TypeError) as exc:
            request = exc
        plan_requested = isinstance(request, dict) and cast(dict[str, Any], request).get("plan") is True

        def store(data: bytes) -> dict[str, Any]:
            try:
                if isinstance(request, (ValueError, TypeError)):
                    raise request
                if not isinstance(request, dict) or set(cast(dict[str, Any], request)) - {
                    "task",
                    "title",
                    "request",
                    "plan",
                }:
                    raise ValueError("artifact upload takes task, title and optional request or plan")
                asked = cast(dict[str, Any], request)
                by = self.headers.get("X-Ledger-Agent") or ("operator" if plan_requested else None)
                op = {"op": "artifact_add", "by": by, "task": "", **asked}
                ledger_artifacts.check_add(
                    {**op, "id": "upload", "title": asked.get("title", ""), "file": {"id": "0" * 64 + ".md"}}
                )
            except (ValueError, TypeError) as exc:
                raise ledger_media.Refused(400, str(exc)) from exc
            doc = server.repository.get_document(slug)
            reason = authority.refusal(self.principal, op) or ledger_artifacts.refusal(doc, op, doc["_meta"]["members"])
            if reason:
                raise ledger_media.Refused(403, reason)
            stored: dict[str, Any] = ledger_artifacts.store(slug, name, data)
            return stored

        return self.receive(slug, ledger_artifacts.MAX_BYTES, store, agents_only=True, operator_plan=plan_requested)

    def receive(
        self,
        slug: str,
        limit: int,
        store: Callable[[bytes], dict[str, Any]],
        agents_only: bool = False,
        operator_plan: bool = False,
    ) -> None:
        """Take one upload body within ``limit`` from a caller the ledger admits and store it."""
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        origin = self.headers.get("Origin")
        agent = self.headers.get("X-Ledger-Agent")
        operator = operator_plan and agent in (None, "operator") and not self.headers.get(TICK_HEADER)
        if origin not in server.ALLOWED_ORIGINS and (origin is not None or not (agent or operator)):
            return self.send(403, "origin not allowed", "text/plain")
        if self.refused(slug):
            return None
        if (agent or agents_only) and not (operator and self.principal == ""):
            meta = server.repository.read(slug, "_meta.members")["_meta"]
            if agent not in meta.get("members", {}):
                return self.send(403, "agent must join this ledger before uploading", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return self.send(413, f"an upload may be at most {limit >> 20} MB", "text/plain")
        if length <= 0:
            return self.send(400, "the upload is empty", "text/plain")
        try:
            stored = store(self.rfile.read(length))
        except ledger_media.Refused as exc:
            return self.send(exc.status, str(exc), "text/plain")
        return self.send(200, json.dumps(stored), "application/json")

    def put_ledger(self, slug: str) -> None:
        """Apply one page save: the operator's page changes and the ops the caller may write."""
        if self.refused(slug):
            return None
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= server.MAX_BODY:
                raise ValueError("body size out of range")
            body = core.loads(self.rfile.read(length) or b"{}")
            state = server.repository.get_document(slug)
            task_ids = tuple(task["id"] for task in state.get("tasks", []))
            changes, ops = core.check_body(body, task_ids)
            ledger_media.resolve(slug, ops)
            ledger_artifacts.resolve(slug, ops)
        except ValueError as exc:
            return self.send(400, f'body must be {{"changes": [...], "ops": [...]}}: {exc}', "text/plain")
        if self.principal and changes:
            return self.send(403, "page changes need the operator", "text/plain")
        refusals = {op["id"]: text for op in ops if (text := authority.refusal(self.principal, op))}
        allowed = [op for op in ops if op["id"] not in refusals]
        return self.reply_state(slug, changes, allowed, refusals, ledger_task_duplicates.screen(state, allowed))


class WriteHandler(BaseHandler, Writes):
    """The API service's HTTP handler: every ledger write route, the page session and the write health probe."""

    def __init__(self, request: Any, client_address: Any, owner: Any) -> None:
        """Handle one request that ``owner``, the HTTP server that accepted it, hands this handler."""
        BaseHandler.__init__(self, request, client_address, owner)

    @request_metrics.measured
    def do_OPTIONS(self) -> None:
        """Answer a CORS preflight for a write route, which Traefik sends here with every non-GET method."""
        self.send_response(204)
        if self.headers.get("Origin") == server.FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", server.FILE_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-Ledger-Token, X-Ledger-Agent, X-Ledger-Slug"
            )
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self) -> None:
        """Answer the health probe; the ledger server serves every read."""
        if self.refused():
            return None
        if self.path.split("?", 1)[0] == "/healthz":
            return self.send(200, json.dumps({"dir": str(core.LEDGER_DIR)}), "application/json")
        return self.send(404, "the ledger API service takes writes; read from the ledger server", "text/plain")

    @request_metrics.measured
    def do_POST(self) -> None:
        """Apply one POST write: a version one API operation, an upload, the page session, a bin action or the probe."""
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, services())
        if self.refused():
            return None
        route = self.path.split("?", 1)[0]
        if post := self.post_route(route):
            return post()
        if route != "/api/bin":
            return self.send(404, "not found", "text/plain")
        return self.post_bin()

    def post_bin(self) -> None:
        """Move a ledger to the bin, stopping its swarm, or restore it, as the page's bin action asks."""
        if self.headers.get("Origin") not in server.ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= server.MAX_BODY:
                raise ValueError("body size out of range")
            action, slug = bin_request(core.loads(self.rfile.read(length) or b"{}"))
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        reply: dict[str, Any] = {}
        if action == "delete":
            ledger_bin.delete(slug)
            if server.swarm_status(slug) is not None:
                _, error = swarm_control(slug, ["stop", "--now"])
                reply = {"swarm_error": error} if error else {}
        elif not server.repository.restore(slug):
            return self.send(404, "not in the bin", "text/plain")
        return self.send(200, json.dumps({"binned": sorted(ledger_bin.entries()), **reply}), "application/json")

    @request_metrics.measured
    def do_PATCH(self) -> None:
        """Apply one PATCH write of the version one API."""
        from scripts.swarm_ledger import api

        if not self.path.startswith("/api/v1/"):
            return self.send(404, "not found", "text/plain")
        return api.handle(self, services())

    @request_metrics.measured
    def do_PUT(self) -> None:
        """Apply one PUT write: a version one API save, the layout, a swarm control or a legacy page save."""
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, services())
        slug = self.slug()
        if self.refused():
            return None
        if self.path.split("?", 1)[0] == "/api/layout":
            return self.put_layout()
        if self.path.startswith("/api/swarm/"):
            return self.put_swarm(slug.removeprefix("swarm/"))
        if not self.path.startswith("/api/") or not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        return self.put_ledger(slug)

    def post_route(self, route: str) -> Callable[[], None] | None:
        """The POST handler a legacy route names, or None."""
        if route == "/healthz":
            return self.probe_write
        for prefix, post in (("/api/media/", self.post_media), ("/api/artifacts/", self.post_artifact)):
            if route.startswith(prefix):
                return functools.partial(post, route.removeprefix(prefix))
        if route.startswith("/api/") and route.endswith("/session"):
            return functools.partial(self.post_session, route.removeprefix("/api/").removesuffix("/session"))
        return None

    def post_session(self, slug: str) -> None:
        """Open the operator's page session on the ledger as an HttpOnly cookie; the page itself carries no token."""
        if self.headers.get("Origin") not in server.ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        status, credential, ttl = server.page_session(slug, self.headers)
        if credential is None:
            return self.send(status, "no operator session", "text/plain")
        attributes = ["Path=/", "HttpOnly", "SameSite=Strict"]
        if ttl:
            attributes.append(f"Max-Age={int(ttl)}")
        if self.tls is not None or os.environ.get("SWARM_AUTH_MODE") == "edge":
            attributes.append("Secure")
        data = json.dumps({"expires_in": ttl}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", "; ".join([f"{server.SESSION_COOKIE}{slug}={credential}", *attributes]))
        self.end_headers()
        self.wfile.write(data)

    def probe_write(self) -> None:
        """Prove this process can write its folder."""
        with core.LOCK:
            core.atomic_write(core.LEDGER_DIR / PROBE_FILE, str(core.now_ms()))
        return self.send(200, json.dumps({"dir": str(core.LEDGER_DIR)}), "application/json")


def upkeep(stopping: threading.Event, interval: float = UPKEEP_S) -> None:
    """The write side upkeep: bin purge, binning closed ledgers without a swarm, alert expiry and relay replay."""
    for passes in itertools.count():
        if stopping.is_set():
            return
        try:
            ledger_bin.tidy()
        except OSError as exc:
            sys.stderr.write(f"bin purge: {exc}\n")
        if passes % BIN_SWEEP_EVERY == 0:
            bin_closed_without_swarm()
        if passes and passes % BIN_SWEEP_EVERY == 0:
            expire_alerts()
        if passes % REPLAY_EVERY == 0:
            try:
                if unrelayed():
                    replay_unrelayed()
            except Exception as exc:
                sys.stderr.write(f"unrelayed replay: {exc}\n")
        server.scheduled_wait(interval)


def take_lease(writer: Any, ledger_writer: Any) -> None:
    """Hold the folder's writer lease while the store is a folder, refusing to start beside another writer."""
    try:
        writer.acquire(ledger_writer.owner(), wait=0.0)
    except ledger_writer.WriterConflict as conflict:
        sys.exit(str(conflict))


def answered(handler: type[Any]) -> str:
    """The HTTP methods ``handler`` answers, named in the start line so the public host's method routing reads
    against it."""
    methods = (
        ("GET", hasattr(handler, "do_GET")),
        ("POST", hasattr(handler, "do_POST")),
        ("PUT", hasattr(handler, "do_PUT")),
        ("PATCH", hasattr(handler, "do_PATCH")),
        ("OPTIONS", hasattr(handler, "do_OPTIONS")),
    )
    return ", ".join(method for method, found in methods if found)


def serve() -> None:
    """Migrate the shared schema, start the write side upkeep and the outbox relay, and answer write requests."""
    from scripts.swarm_ledger import outbox_host
    from scripts.swarm_ledger.repository import legacy

    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    stored: Any = server.stored
    ledger_writer: Any = ports.swarm().ledger_writer
    writer: Any = ledger_writer.WriterLease(core.LEDGER_DIR)
    if storage.shared():
        stored.migrate()
    else:
        take_lease(writer, ledger_writer)
    cast(Any, legacy).adopt(stored)
    adopt_unrelayed()
    threading.Thread(target=adopt_disk_files, daemon=True).start()
    httpd: Any = server.listening(handler=WriteHandler)
    stopping = threading.Event()
    threading.Thread(target=server.retrying(upkeep), args=(stopping,), daemon=True).start()

    def stop(signum: int, frame: object) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    scheme = "https" if WriteHandler.tls is not None else "http"
    print(
        f"ledger API service on {scheme}://{HOST}:{PORT} answering {answered(WriteHandler)}, dir {core.LEDGER_DIR}",
        flush=True,
    )
    try:
        with cast(Any, outbox_host).running(stored):
            httpd.serve_forever(server.POLL_S)
    finally:
        stopping.set()
        httpd.server_close()
        if not storage.shared():
            writer.release()


def main(argv: list[str] | None = None) -> None:
    """Run the ledger API service in the foreground."""
    parser = argparse.ArgumentParser(description="The ledger API service: every ledger write over HTTP.")
    parser.add_argument("--serve", action="store_true", help="run in the foreground")
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is the only mode")
    from scripts.swarm_ledger import composition

    composition.wire()
    serve()


if __name__ == "__main__":
    main()
