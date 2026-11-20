from typing import Any


def call(slug: str, ops: list[Any] | None = None) -> dict[str, Any]:
    return {"_meta": {"rev": len(slug) + len(ops or [])}}


def resource(slug: str, path: str) -> dict[str, Any]:
    return {"_meta": {"rev": len(slug) + len(path)}}


def ack_revision(slug: str) -> int:
    # ruleid: whole-ledger-read-for-revision
    return call(slug)["_meta"]["rev"]


def module_call_revision(ledger: Any, slug: str) -> int:
    # ruleid: whole-ledger-read-for-revision
    return ledger.call(slug)["_meta"]["rev"]


def snapshot_revision(client: Any, slug: str) -> int:
    # ruleid: whole-ledger-read-for-revision
    return client.snapshot(slug)["_meta"]["rev"]


def current_revision(repository: Any, slug: str) -> int:
    # ruleid: whole-ledger-read-for-revision
    return repository.current(slug).state["_meta"]["rev"]


def document_revision(repository: Any, slug: str) -> int:
    # ruleid: whole-ledger-read-for-revision
    return repository.get_document(slug)["_meta"]["rev"]


def metadata_revision(slug: str) -> int:
    # ok: whole-ledger-read-for-revision
    return resource(slug, "metadata")["_meta"]["rev"]


def written_revision(slug: str, ops: list[Any]) -> int:
    # ok: whole-ledger-read-for-revision
    return call(slug, ops)["_meta"]["rev"]
