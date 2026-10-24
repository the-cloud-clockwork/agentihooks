import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from functools import wraps

from .reindex import binned, ledger_dir
from .search import Filters
from .search import get as get_item
from .search import search as search_hits
from .search import timeline as timeline_entries
from .store import SQLiteRecallStore, default_path


class RecallError(Exception):
    pass


def _answer(call: Callable[..., dict]) -> Callable[..., dict]:
    @wraps(call)
    def answered(*args, **kwargs) -> dict:
        try:
            return {"success": True, **call(*args, **kwargs)}
        except (RecallError, ValueError) as error:
            return {"success": False, "error": str(error)}

    return answered


def _store(environ: Mapping[str, str]) -> SQLiteRecallStore:
    path = default_path(environ)
    if not path.is_file():
        raise RecallError(f"no recall archive at {path}; build it with agentihooks recall reindex --all")
    return SQLiteRecallStore(path, read_only=True)


def default_scope(scope: str, environ: Mapping[str, str]) -> str:
    if scope:
        return scope
    from scripts.gates.base import Who

    return Who.from_env(environ).swarm or "all"


def _kinds(kinds: str) -> tuple[str, ...]:
    return tuple(kind.strip() for kind in kinds.split(",") if kind.strip())


def filters(scope: str = "", kinds: str = "", author: str = "", seat: str = "", period: str = "") -> Filters:
    since, _, until = period.partition("..")
    return Filters(scope, _kinds(kinds), author, seat, since.strip() or None, until.strip() or None)


def _scoped(chosen: Filters | None, environ: Mapping[str, str]) -> Filters:
    chosen = chosen or filters()
    return replace(chosen, scope=default_scope(chosen.scope, environ))


@_answer
def search(query: str, chosen: Filters | None = None, limit: int = 10, environ: Mapping[str, str] = os.environ) -> dict:
    store = _store(environ)
    scoped = _scoped(chosen, environ)
    return {"scope": scoped.scope, "hits": [asdict(hit) for hit in search_hits(store, query, scoped, limit)]}


@_answer
def get(ref: str, scope: str = "", environ: Mapping[str, str] = os.environ) -> dict:
    store = _store(environ)
    scope = default_scope(scope, environ)
    item = get_item(store, ref, scope)
    if item is None:
        raise RecallError(f"no recall item {ref} in scope {scope}")
    return {"item": asdict(item)}


@_answer
def timeline(chosen: Filters | None = None, limit: int = 200, environ: Mapping[str, str] = os.environ) -> dict:
    store = _store(environ)
    scoped = _scoped(chosen, environ)
    return {"scope": scoped.scope, "entries": [asdict(entry) for entry in timeline_entries(store, scoped, limit)]}


@_answer
def sources(environ: Mapping[str, str] = os.environ) -> dict:
    store = _store(environ)
    hidden = binned(ledger_dir(environ))
    listed = []
    for count in store.sources():
        kind, _, slug = count.source.partition("/")
        listed.append(
            {
                "source": count.source,
                "kind": kind,
                "slug": slug,
                "swarm_slug": count.swarm_slug,
                "records": count.records,
                "items": count.items,
                "archived": count.archived,
                "last_indexed": count.indexed_at,
                "binned": slug in hidden,
            }
        )
    return {"archive": str(store.path), "sources": listed}
