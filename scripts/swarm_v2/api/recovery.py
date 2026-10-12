import re
from collections.abc import Callable
from dataclasses import asdict
from threading import Lock

from redis.exceptions import RedisError

from scripts.hive import auth
from scripts.swarm.store import MASTER, RedisStore, SwarmError
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.kubernetes.failures import Recovery
from scripts.swarm_v2.runtime.commands import Role

PATH = re.compile(r"/v1/executions/([^/]+)/(restore-decision|checkpoints|restore-outcome)")


class RecoveryAPI:
    def __init__(
        self,
        store: RedisStore,
        slug: str,
        recovery: Callable[[], Recovery | None],
        operator: Callable[[str, str], object] | None = None,
        worker: Callable[[str], object] | None = None,
    ) -> None:
        self.store, self.slug, self.recovery, self.operator = store, slug, recovery, operator
        self.worker = worker
        self.lock = Lock()

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        subject = PATH.fullmatch(path)
        if method != "POST" or subject is None:
            return 404, GrantRefused("invalid_request", "no such recovery endpoint").detail()
        try:
            if not isinstance(body, dict):
                raise GrantRefused("invalid_request", "a recovery decision needs an object")
            if not authorization.startswith("Bearer "):
                raise GrantRefused("unauthenticated", "a bearer credential is required")
            token = authorization.removeprefix("Bearer ")
            if subject[2] == "restore-outcome":
                return self._outcome(subject[1], token, body)
            self._authorize(token, body.get("agent", ""))
            if subject[2] == "checkpoints":
                return self._checkpoints(subject[1], body)
            if body.get("choice") not in ("resume", "fresh"):
                raise GrantRefused("invalid_request", "a recovery decision is fresh or resume")
            recovery = self.recovery()
            if recovery is None:
                raise GrantRefused("dependency_unavailable", "controller recovery is unavailable")
            with self.lock:
                return 200, asdict(recovery.request(subject[1], body["choice"]))
        except GrantRefused as error:
            status = {"invalid_request": 400, "unauthenticated": 401, "forbidden_scope": 403}
            return status.get(error.error_class, 503), error.detail()
        except SwarmError as error:
            return 409, GrantRefused("recovery_refused", str(error)).detail()
        except RedisError:
            return 503, GrantRefused("dependency_unavailable", "the recovery store is unavailable").detail()

    def _outcome(self, execution_id: str, token: str, body: dict) -> tuple[int, dict]:
        registration = self.worker(token) if self.worker is not None else None
        if registration is None or registration.execution_id != execution_id:
            raise GrantRefused("forbidden_scope", "restore outcome is not bound to this execution")
        recovery = self.recovery()
        if recovery is None:
            raise GrantRefused("dependency_unavailable", "controller recovery is unavailable")
        with self.lock:
            return 200, recovery.confirm(execution_id, body)

    def _checkpoints(self, execution_id: str, body: dict) -> tuple[int, dict]:
        recovery = self.recovery()
        if recovery is None:
            raise GrantRefused("dependency_unavailable", "controller recovery is unavailable")
        rows = recovery.checkpoints.list(execution_id)
        if "checkpoint_id" not in body:
            return 200, {"checkpoints": rows}
        identifier = body["checkpoint_id"]
        if identifier not in {entry["checkpoint_id"] for entry in rows}:
            raise SwarmError("checkpoint is not verified for this execution")
        return 200, recovery.checkpoints.fetch(execution_id, identifier)

    def _authorize(self, token: str, name: object) -> None:
        caller = self.operator(self.slug, token) if self.operator is not None else None
        if caller is not None and caller.role == Role.OPERATOR:
            return
        if not isinstance(name, str) or not name or not auth.agent_member(self.store.redis, token, self.slug, name):
            raise GrantRefused("unauthenticated", "the credential is not bound to this swarm and caller")
        if not any(
            agent.name == name and agent.lane == MASTER and agent.state != "finished"
            for agent in self.store.agents(self.slug)
        ):
            raise GrantRefused("forbidden_scope", "only the master or operator can choose resume or fresh")
