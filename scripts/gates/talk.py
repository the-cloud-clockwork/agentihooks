"""The talk budget: an eng or ci agent's talk writes between outcomes, counted and refused in the ledger server.

Every route to a ledger write lands in the server, so the gate wraps the server's op path rather than a tool call.
The mode is the swarm config's gates entry (enforce, observe, off), observe until the operator changes it.
"""

from scripts.gates import log
from scripts.gates.base import Who
from scripts.gates.lift import LIFT_SECONDS, agent_lifted

NAME = "talk"
BUDGET = 10
DEFAULT_MODE = "observe"
WORKER_LANES = ("eng", "ci")
REFUSED = "talk refused"
LIFTED = "gate lifted"
TALK_THREADS = ("chat",)
OUTCOME_FIELDS = (("state", "task {}"), ("pr_url", "pull request recorded"), ("proof", "proof recorded"))


def talk_op(op):
    if op.get("op") == "add_item":
        return op.get("list") == "followups"
    thread = str(op.get("thread"))
    return op.get("op") in ("add", "edit") and "by" in op and (thread in TALK_THREADS or thread.endswith("/comments"))


def outcome_op(op):
    fields = op.get("fields") if op.get("op") == "task_update" else None
    if not isinstance(fields, dict):
        return ""
    return next((text.format(fields[key]) for key, text in OUTCOME_FIELDS if key in fields), "")


def refusal(by, count, slug):
    return (
        f"{REFUSED}: {by} made {count} talk writes since its last outcome, the budget is {BUDGET}. "
        f"Record an outcome first: push the commit, or run agentihooks swarm {slug} pr <url>. "
        "Only the operator lifts it, by typing lift the talk gate in this pane."
    )


def lifted(meta, by, now_ms):
    since = now_ms - LIFT_SECONDS * 1000
    return any(
        e.get("kind") == LIFTED and e.get("by") == by and e.get("gate") == NAME and e["at"] >= since
        for e in meta["events"]
    )


def owes(meta, by, tasks):
    import ledger_gate

    return bool(ledger_gate.unhandled_for(meta, by, tasks))


def mode_of(config):
    chosen = config.gates.get(NAME)
    return chosen if chosen in ("enforce", "off") else DEFAULT_MODE


def _connect():
    from scripts.swarm.store import redis_client

    return redis_client()


class Budget:
    def __init__(self, slug, connect=_connect, home=None):
        self.slug, self.connect, self.home = slug, connect, home
        self._redis = None

    def apply(self, doc, op, ctx, apply):
        from scripts.swarm.naming import lane_of, resolve_name

        by = resolve_name(str(op.get("by")))
        outcome, talk = outcome_op(op), talk_op(op)
        if lane_of(by) not in WORKER_LANES or not (outcome or talk):
            return apply(doc, op, ctx)
        who = Who(name=by, task=_held(doc, by))
        try:
            marks = self._marks()
        except Exception as exc:  # a crashed gate lets the write through, counted in the gate log
            log.append(
                self.slug, log.Row.of(NAME, "fail-open", who, "ledger", f"{type(exc).__name__}: {exc}"), self.home
            )
            return apply(doc, op, ctx)
        if marks is None:
            return apply(doc, op, ctx)
        mode, progress = marks
        if outcome:
            done = apply(doc, op, ctx)
            if done:
                progress.outcome(by, outcome, ctx.at)
            return done
        if self._exempt(doc, by, ctx):
            return apply(doc, op, ctx)
        count = progress.read(by).talk
        if count >= BUDGET:
            reason = refusal(by, count, self.slug)
            kind = "deny" if mode == "enforce" else "observe"
            log.append(self.slug, log.Row.of(NAME, kind, who, "ledger", reason), self.home)
            if mode == "enforce":
                ctx.refused.append(reason)
                return False
        done = apply(doc, op, ctx)
        if done:
            progress.talk(by)
        return done

    def _exempt(self, doc, by, ctx):
        from scripts.swarm.naming import NameRegistry

        return (
            owes(ctx.meta, by, doc["tasks"])
            or lifted(ctx.meta, by, ctx.at)
            or agent_lifted(self.slug, by, NAME, self.home, ctx.at / 1000)
            or bool(NameRegistry(self._redis).entry(by).get("operator"))
        )

    def _marks(self):
        from scripts.gates.progress import Progress
        from scripts.swarm.store import RedisStore, SwarmError

        if self._redis is None:
            self._redis = self.connect()
        try:
            mode = mode_of(RedisStore(self._redis).config(self.slug))
        except SwarmError:
            return None
        return None if mode == "off" else (mode, Progress(self._redis, self.slug))


def _held(doc, by):
    return next((t["id"] for t in doc["tasks"] if t.get("claimed_by") == by and t.get("state") != "done"), "")
