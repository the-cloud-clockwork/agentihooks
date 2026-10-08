import json
import sys
from pathlib import Path
from types import ModuleType

import ledger_alerts
import ledger_close
import ledger_core as core
import ledger_notifications
import ledger_priorities
import ledger_size

from . import bin_storage, shadow

SYNCED = {}


def signature(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def synced(html_path, json_path, reconcile):
    entry = SYNCED.get(json_path)
    if entry is None or entry[1] != signature(json_path) or (reconcile and entry[0] != signature(html_path)):
        return None
    return {**entry[2], "_meta": dict(entry[2]["_meta"])}


def load_state(json_path, seed, core=core):
    if json_path.exists():
        state = core.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("_meta"), dict) or "seeds" not in state["_meta"]:
            raise ValueError(f"{json_path} has no ledger _meta")
        meta = state.pop("_meta")
        meta.setdefault("events", [])
        return core.normalize(state), meta, False
    if seed is None:
        raise ValueError(f"{json_path} is missing and the HTML seed is unreadable")
    doc = {k: v for k, v in seed.items() if k not in ("_rev", "notifications")}
    doc["phases"] = [{k: v for k, v in phase.items() if k != "review"} for phase in doc["phases"]]
    meta = {"rev": 0, "stamps": {}, "events": [], "seeds": {"0": doc}, "seed_error": None, "updated_at": core.now_ms()}
    return doc, meta, True


def sync(slug, changes=None, ops=None, gate=None, core=core):
    """Fold agent seed edits and operator changes/ops into the JSON, then rewrite the seed.

    Returns (state, rejected). The JSON is written before the HTML so a crash in between
    leaves an old `_rev` in the seed, which the next sync diffs against its own seed.
    """
    html_path, json_path = core.paths(slug)
    with core.LOCK, shadow.storage_lock(core.LEDGER_DIR):
        html = html_path.read_text(encoding="utf-8")
        try:
            seed, seed_error = core.parse_seed(html), None
        except ValueError as exc:
            seed, seed_error = None, f"HTML seed unreadable, agent edits ignored until fixed: {exc}"
        doc, meta, created = load_state(json_path, seed, core)
        ctx = core.Context(meta, core.now_ms())
        meta.setdefault("members", {})
        meta["created_at"] = core.earliest(meta, ctx.at)
        seed_rev = None if seed is None else seed.get("_rev", meta["rev"])
        base = None if seed is None else meta["seeds"].get(str(seed_rev))
        if base is not None:
            core.reconcile_fields(doc, core.normalize(base), seed, ctx)
            core.reconcile_threads(doc, core.normalize(base), seed, ctx)
        elif seed is not None:
            ctx.refused.append(
                f"the page copy at revision {seed_rev} is too old to merge, its agent edits were ignored"
            )
        rejected = core.apply_changes(doc, changes or [], ctx)
        ordered_ops = sorted(ops or [], key=lambda op: op["op"] == "stats_sync")
        rejected += [op["id"] for op in ordered_ops if not core.gated(gate, doc, op, ctx)]
        import ledger_artifacts
        import ledger_media

        ledger_artifacts.sweep(slug, doc, ctx)
        ledger_media.attach_paths(slug, doc, ctx.events)
        ledger_priorities.derive(doc, ctx)
        ledger_notifications.derive(doc, ctx)
        del doc["chat"][: -core.CHAT_KEPT]
        size = core.warnings(doc)
        found = size + ctx.refused
        ledger_alerts.derive(
            doc,
            ctx,
            [*((ledger_alerts.SIZE, w) for w in size), *((ledger_alerts.SYNC, w) for w in ctx.refused)],
            meta.get("warnings") or [],
        )
        if ctx.events or ctx.dirty or seed_error != meta.get("seed_error") or found != meta.get("warnings") or created:
            meta.update(rev=ctx.rev, updated_at=ctx.at, seed_error=seed_error, warnings=found)
            meta["events"] = (meta["events"] + ctx.events)[-core.EVENTS_KEPT :]
        meta["seeds"][str(meta["rev"])] = doc
        meta["seeds"] = {k: v for k, v in meta["seeds"].items() if int(k) > meta["rev"] - core.SEEDS_KEPT}
        state = {**doc, "_meta": meta}
        core.write_if_changed(json_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        if seed is not None:
            core.rewrite_seed(html_path, html, doc, meta["rev"])
        shadow.persist(core.LEDGER_DIR, slug, state)
        SYNCED[json_path] = (signature(html_path), signature(json_path), {**doc, "_meta": dict(meta)})
        return state, rejected


def all_summaries():
    found = []
    for path in sorted(core.LEDGER_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        json_path = core.paths(path.stem)[1]
        try:
            seed = core.parse_seed(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            seed = None
        try:
            doc, meta, _ = core.load_state(json_path, seed)
        except (ValueError, OSError):
            continue
        items = [i for i in doc.get("tasks") or doc.get("phases") or [] if not i.get("out_of_scope")]
        done = sum(1 for i in items if i.get("done") is True)
        found.append(
            {
                "slug": path.stem,
                "title": doc.get("title") or path.stem,
                "overview": ledger_close.intro(doc.get("overview") or ""),
                "closed_at": doc.get("closed_at"),
                "size": ledger_size.size_of(doc),
                "open": len(items) - done,
                "done": done,
                "updated_at": meta.get("updated_at"),
            }
        )
    return found


def create(slug, content, size="small"):
    import ledger_link
    import new_ledger

    html_path, json_path = core.paths(slug)
    if html_path.exists():
        return False
    if json_path.exists():
        sys.exit(f"{json_path} exists without its HTML; move it aside before creating a new ledger")
    errors = new_ledger.check(content)
    if errors:
        sys.exit("content rejected:\n  " + "\n  ".join(errors))
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    doc = new_ledger.build_doc(content, size)
    core.validate(doc)
    core.atomic_write(html_path, new_ledger.render(doc, slug, ledger_link.address()[1]))
    core.sync(slug)
    return True


class FileLedgerRepository:
    def __init__(self, domain: ModuleType = core):
        self.domain = domain

    def get_document(self, slug: str, reconcile: bool = True) -> dict:
        with self.domain.LOCK:
            state = synced(*self.domain.paths(slug), reconcile)
        if state is not None:
            return state
        if reconcile:
            return sync(slug, core=self.domain)[0]
        with self.domain.LOCK:
            doc, meta, _ = load_state(self.domain.paths(slug)[1], None, self.domain)
        return {**doc, "_meta": meta}

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        return sync(slug, changes=changes, ops=ops, gate=gate, core=self.domain)

    def events_since(self, slug: str, revision: int) -> list:
        with self.domain.LOCK:
            _, meta, _ = load_state(self.domain.paths(slug)[1], None, self.domain)
            return [e for e in meta["events"] if e["rev"] > revision]

    def list_summaries(self) -> list:
        return all_summaries()

    def create(self, slug: str, content: dict, size: str = "small") -> bool:
        return create(slug, content, size)

    def delete(self, slug: str, now: int | None = None) -> None:
        bin_storage.delete(slug, now)

    def restore(self, slug: str, now: int | None = None) -> bool:
        return bin_storage.restore(slug, now)

    def read_page(self, slug: str) -> str:
        return core.paths(slug)[0].read_text(encoding="utf-8")

    def write_page(self, slug: str, page: str) -> None:
        core.atomic_write(core.paths(slug)[0], page)

    def exists(self, slug: str) -> bool:
        return core.paths(slug)[0].exists()

    def read_snapshot(self, slug: str) -> dict:
        return core.loads(core.paths(slug)[1].read_text(encoding="utf-8"))

    def pages(self) -> list[Path]:
        return list(core.LEDGER_DIR.glob("*.html"))
