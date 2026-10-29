from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from re import Pattern
from typing import Protocol

from scripts.swarm_ledger.repository import LedgerRepository


class LedgerCore(Protocol):
    SLUG_RE: Pattern[str]

    def loads(self, raw: bytes | str) -> dict: ...

    def check_body(self, body: dict, task_ids: tuple = ()) -> list: ...

    def apply_changes(self, doc: dict, changes: list, ctx: object) -> bool: ...


class Authority(Protocol):
    def caller(self, headers: Mapping[str, str], admin: str | None, slug: str) -> str | None: ...

    def controller(self, credential: str | None) -> bool: ...

    def refusal(self, principal: str, op: dict) -> str | None: ...

    def fence(self, slug: str, epoch: int) -> None: ...

    def hive_member(self, credential: str | None) -> object | None: ...

    def hive_agent(self, member: object, slug: str, agent: str) -> bool: ...

    def hive_agent_token(self, member: object, slug: str, agent: str) -> str: ...


class LedgerService(Protocol):
    core: LedgerCore
    authority: Authority
    hub: object
    layout_rows: Mapping[str, Collection[str]]
    layout_lists: Collection[str]

    def home_summaries(self) -> list[dict]: ...

    def bin_summaries(self) -> list[dict]: ...

    def bin_entries(self) -> dict[str, int]: ...

    def bin_ledger(self, slug: str) -> None: ...

    def workspace_tails(self, slug: str, task_id: str) -> dict: ...

    def stream_resources(self, slug: str) -> dict: ...

    def layout(self) -> dict: ...

    def write_layout(self, sizes: dict) -> dict: ...

    def write_gate(self, slug: str) -> object: ...

    def relay_to_inbox(self, slug: str, state: dict) -> object: ...

    def deliver_alerts(self, slug: str, state: dict) -> object: ...


class SwarmService(Protocol):
    doctor: Mapping[str, list[str]]

    def store(self) -> object: ...

    def status(self, slug: str) -> dict | None: ...

    def models(self) -> dict: ...

    def control(self, slug: str, argv: list[str], command: str = ...) -> tuple[dict | None, str]: ...

    def lifecycle(self, slug: str, body: dict, operator: bool) -> tuple[dict | None, str]: ...

    def refresh_quota(self, slug: str) -> tuple[dict | None, str]: ...

    def control_argv(self, body: dict) -> list[str]: ...

    def doctor_phrase(self, slug: str, state: dict) -> object: ...


class MediaService(Protocol):
    media_store: object
    artifact_store: object

    def resolve_media(self, slug: str, operations: list) -> None: ...

    def resolve_artifacts(self, slug: str, operations: list) -> None: ...


@dataclass(frozen=True)
class HttpPolicy:
    allowed_hosts: Collection[str]
    allowed_origins: Collection[str]
    max_body: int


@dataclass(frozen=True)
class Services:
    repository: LedgerRepository
    ledger: LedgerService
    swarm: SwarmService
    media: MediaService
    policy: HttpPolicy


BodyReader = Callable[[object, Services], dict]
