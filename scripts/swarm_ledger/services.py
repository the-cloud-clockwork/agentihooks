from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from scripts.swarm_ledger.api.services import Authority, LedgerCore


@dataclass(frozen=True)
class Ledger:
    core: LedgerCore
    authority: Authority
    hub: object
    layout_rows: Mapping[str, Collection[str]]
    layout_lists: Collection[str]
    home_summaries: Callable[[], list[dict]]
    bin_summaries: Callable[[], list[dict]]
    bin_entries: Callable[[], dict[str, int]]
    bin_ledger: Callable[[str], None]
    workspace_tails: Callable[[str, str], dict]
    stream_resources: Callable[[str], dict]
    layout: Callable[[], dict]
    write_layout: Callable[[dict], dict]
    write_gate: Callable[[str], object]
    relay_to_inbox: Callable[[str, dict], object]
    deliver_alerts: Callable[[str, dict], object]


@dataclass(frozen=True)
class Swarm:
    doctor: Mapping[str, list[str]]
    store: Callable[[], object]
    status: Callable[[str], dict | None]
    models: Callable[[], dict]
    control: Callable[..., tuple[dict | None, str]]
    lifecycle: Callable[[str, dict, bool], tuple[dict | None, str]]
    refresh_quota: Callable[[str], tuple[dict | None, str]]
    control_argv: Callable[[dict], list[str]]
    doctor_phrase: Callable[[str, dict], object]


@dataclass(frozen=True)
class Media:
    media_store: object
    artifact_store: object
    resolve_media: Callable[[str, list], None]
    resolve_artifacts: Callable[[str, list], None]
