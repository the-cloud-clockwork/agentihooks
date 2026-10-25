from types import SimpleNamespace

from scripts.swarm_ledger.api.services import HttpPolicy, Services

LEDGER = {
    "core": "core",
    "authority": "authority",
    "hub": "HUB",
    "layout_rows": "ledger_layout.ROWS",
    "layout_lists": "ledger_layout.LISTS",
    "home_summaries": "home_summaries",
    "bin_summaries": "bin_summaries",
    "bin_entries": "ledger_bin.entries",
    "bin_ledger": "ledger_bin.delete",
    "workspace_tails": "workspace_tails",
    "stream_resources": "stream_resources",
    "layout": "ledger_layout.read",
    "write_layout": "ledger_layout.write",
    "write_gate": "write_gate",
    "relay_to_inbox": "relay_to_inbox",
    "deliver_alerts": "deliver_alerts",
}
SWARM = {
    "doctor": "DOCTOR",
    "store": "swarm_store",
    "status": "swarm_status",
    "models": "swarm_models",
    "control": "swarm_control",
    "lifecycle": "lifecycle_control",
    "refresh_quota": "refresh_quota",
    "control_argv": "control_argv",
    "doctor_phrase": "doctor_phrase",
}
MEDIA = {
    "media_store": "ledger_media",
    "artifact_store": "ledger_artifacts",
    "resolve_media": "ledger_media.resolve",
    "resolve_artifacts": "ledger_artifacts.resolve",
}
MISSING = object()


def _pick(server: object, names: dict) -> SimpleNamespace:
    found = {}
    for name, path in names.items():
        value = server
        for part in path.split("."):
            value = getattr(value, part, MISSING)
            if value is MISSING:
                break
        if value is not MISSING:
            found[name] = value
    return SimpleNamespace(**found)


def as_services(server: object) -> Services:
    return Services(
        repository=getattr(server, "repository", None),
        ledger=_pick(server, LEDGER),
        swarm=_pick(server, SWARM),
        media=_pick(server, MEDIA),
        policy=HttpPolicy(
            allowed_hosts=getattr(server, "ALLOWED_HOSTS", ()),
            allowed_origins=getattr(server, "ALLOWED_ORIGINS", ()),
            max_body=getattr(server, "MAX_BODY", 0),
        ),
    )


def serve_document(monkeypatch, repository: object, document: dict) -> None:
    from scripts.swarm_ledger.api import resources, task_list

    monkeypatch.setattr(repository, "current", lambda slug: SimpleNamespace(state=document, views={}))
    monkeypatch.setattr(repository, "read", lambda slug, *keys: document)
    version = (int(resources.resource_revision(document, "tasks"), 16), 0)
    monkeypatch.setattr(repository, "task_version", lambda slug: version)
    monkeypatch.setattr(task_list, "HELD", {})
