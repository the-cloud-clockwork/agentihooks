"""Provider-native resume: the installed CLI's own conversation files, captured without account state."""

import io
import json
import os
import re
import sqlite3
import tarfile
from collections import Counter
from contextlib import closing, suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from scripts.swarm_v2.artifacts.base import ArtifactRef, Scope
from scripts.swarm_v2.checkpoints import model, redaction
from scripts.swarm_v2.filesystem import ARCHIVE_BYTES, ARCHIVE_MEMBERS, LayoutError, contain

METRIC = "native_resume_success_by_target"
UNAVAILABLE = "native_resume_unavailable"
CAPTURED = "captured"
RESUMED = "resumed"
QUALIFIED = {"claude": ("2.1",), "codex": ("0.162",)}
DECISION = ("continue_from_context", "start_fresh")
EXCLUSIONS = (
    "account authentication stores",
    "environment variables",
    "harness databases, which the CLI rebuilds from its conversation files",
)
FORMAT = "native_format_changed"
BUSY = "database_busy"
TOO_LARGE = "native_too_large"
ESCAPE = "native_member_escapes_home"
UNREADABLE = "native_unreadable"
VERSION = re.compile(r"(\d+)\.(\d+)\.\d+")
MEMBER_MODE = 0o600


class NativeError(ValueError):
    pass


@dataclass(frozen=True)
class Unavailable:
    """No native resume; the continue-or-fresh decision stays with the operator."""

    harness: str
    cause: str
    outcome: str = UNAVAILABLE
    decision: tuple[str, ...] = DECISION

    def fields(self) -> dict:
        return {
            "continuation": model.CONTEXT,
            "native_resume_capability": "unsupported",
            "native_session_id": None,
            "native_resume": {
                "outcome": self.outcome,
                "harness": self.harness,
                "cause": self.cause,
                "decision": list(self.decision),
            },
        }


@dataclass(frozen=True)
class Resumed:
    harness: str
    session_id: str
    command: tuple[str, ...]
    outcome: str = RESUMED


def oversized(files: tuple[tuple[str, bytes], ...]) -> bool:
    return len(files) > ARCHIVE_MEMBERS or sum(len(data) for _, data in files) > ARCHIVE_BYTES


def archive(files: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as written:  # NOSONAR: writes only
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), MEMBER_MODE
            written.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@dataclass(frozen=True)
class NativeCapture:
    harness: str
    version: str
    session_id: str
    files: tuple[tuple[str, bytes], ...]
    quiescence: str
    redactions: int = 0

    def material(self) -> bytes:
        return archive(self.files)

    def fields(self) -> dict:
        ref = asdict(ArtifactRef.of(self.material()))
        counted = {redaction.FIELD: self.redactions} if self.redactions else {}
        return {
            "continuation": model.NATIVE,
            "native_resume_capability": "qualified",
            "native_session_id": self.session_id,
            "native_harness_version": self.version,
            "quiescence_method": self.quiescence,
            "inventory": {model.NATIVE_SESSION: ref},
            "artifact_hashes": {model.NATIVE_SESSION: "sha256:" + ref["sha256"]},
            "native_resume": {"outcome": CAPTURED, "harness": self.harness},
            **counted,
        }

    @property
    def outcome(self) -> str:
        return CAPTURED


class Harness(Protocol):
    name: str

    def collect(self, home: Path, session_id: str, cwd: Path) -> dict[str, bytes]: ...

    def identity(self, files: dict[str, bytes]) -> tuple[str, str]: ...

    def placed(self, session_id: str, cwd: Path, name: str) -> PurePosixPath: ...

    def command(self, session_id: str) -> tuple[str, ...]: ...


def line(version_text: str) -> str | None:
    found = VERSION.search(version_text)
    return f"{found[1]}.{found[2]}" if found else None


def encoded(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _complete(data: bytes) -> bytes:
    """An append-only conversation file is read up to its last complete record; a record still being written stays out."""
    return data[: data.rfind(b"\n") + 1]


def _records(data: bytes) -> list[dict]:
    try:
        records = [json.loads(text) for text in data.decode().splitlines()]
    except ValueError as error:
        raise NativeError(FORMAT) from error
    if not records or not all(isinstance(r, dict) and isinstance(r.get("type"), str) for r in records):
        raise NativeError(FORMAT)
    return records


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise NativeError("conversation_absent")
    return path.read_bytes()


class Claude:
    name = "claude"
    conversation = "conversation.jsonl"
    companion = "companion"

    def collect(self, home: Path, session_id: str, cwd: Path) -> dict[str, bytes]:
        folder = home / "projects" / encoded(cwd)
        files = {self.conversation: _complete(_regular(folder / f"{session_id}.jsonl"))}
        extra = folder / session_id
        if extra.is_dir():
            for path in sorted(extra.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files[f"{self.companion}/{path.relative_to(extra).as_posix()}"] = path.read_bytes()
        return files

    def identity(self, files: dict[str, bytes]) -> tuple[str, str]:
        if self.conversation not in files:
            raise NativeError(FORMAT)
        records = _records(files[self.conversation])
        named = [r["sessionId"] for r in records if "sessionId" in r]
        if not all(isinstance(session, str) for session in named):
            raise NativeError(FORMAT)
        sessions = set(named)
        versions = [r["version"] for r in records if isinstance(r.get("version"), str)]
        if len(sessions) != 1 or not versions:
            raise NativeError(FORMAT)
        return sessions.pop(), versions[-1]

    def placed(self, session_id: str, cwd: Path, name: str) -> PurePosixPath:
        folder = PurePosixPath("projects", encoded(cwd))
        if name == self.conversation:
            return folder / f"{session_id}.jsonl"
        return folder / session_id / PurePosixPath(name).relative_to(self.companion)

    def command(self, session_id: str) -> tuple[str, ...]:
        return ("claude", "--resume", session_id)


def _unlinked(root: Path, path: Path) -> Path:
    """A path under root, reached through no link, resolved; anything else is refused as an escape."""
    for base in (root, root.resolve()):
        if path.is_relative_to(base):
            relative = PurePosixPath(path.relative_to(base).as_posix())
            if _linked(base, relative):
                raise NativeError(ESCAPE)
            try:
                return contain(base, relative)
            except LayoutError as error:
                raise NativeError(ESCAPE) from error
    raise NativeError(ESCAPE)


class Codex:
    name = "codex"
    index = "state_5.sqlite"
    prefix = "rollout"

    def _indexed(self, home: Path, session_id: str) -> str | None:
        """One read that never waits on a writer's lock; SQLite's online backup retries forever on a locked source."""
        db = home / self.index
        if not db.is_file():
            return None
        try:
            with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=0)) as source:
                row = source.execute("SELECT rollout_path FROM threads WHERE id = ?", (session_id,)).fetchone()
        except sqlite3.DatabaseError as error:
            raise NativeError(BUSY if "locked" in str(error) else FORMAT) from error
        return row[0] if row else None

    def _rollout(self, home: Path, session_id: str) -> Path:
        sessions = home / "sessions"
        indexed = self._indexed(home, session_id)
        if indexed:
            return _unlinked(sessions, sessions / indexed)
        found = sorted(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        if len(found) != 1:
            raise NativeError("conversation_absent")
        return _unlinked(sessions, found[0])

    def collect(self, home: Path, session_id: str, cwd: Path) -> dict[str, bytes]:
        path = self._rollout(home, session_id)
        relative = path.relative_to((home / "sessions").resolve())
        return {f"{self.prefix}/{relative.as_posix()}": _complete(_regular(path))}

    def identity(self, files: dict[str, bytes]) -> tuple[str, str]:
        if len(files) != 1:
            raise NativeError(FORMAT)
        meta = _records(next(iter(files.values())))[0]
        payload = meta.get("payload")
        if meta["type"] != "session_meta" or not isinstance(payload, dict):
            raise NativeError(FORMAT)
        if not isinstance(payload.get("id"), str) or not isinstance(payload.get("cli_version"), str):
            raise NativeError(FORMAT)
        return payload["id"], payload["cli_version"]

    def placed(self, session_id: str, cwd: Path, name: str) -> PurePosixPath:
        return PurePosixPath("sessions") / PurePosixPath(name).relative_to(self.prefix)

    def command(self, session_id: str) -> tuple[str, ...]:
        return ("codex", "resume", session_id)


HARNESSES: dict[str, Harness] = {harness.name: harness for harness in (Claude(), Codex())}


def members(data: bytes) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:  # NOSONAR: uncompressed, bounded below
            for info in archive.getmembers():
                expanded += info.size
                parts = PurePosixPath(info.name).parts
                if len(found) >= ARCHIVE_MEMBERS or expanded > ARCHIVE_BYTES:
                    raise NativeError(TOO_LARGE)
                if not info.isreg() or info.name.startswith("/") or ".." in parts:
                    raise NativeError(ESCAPE)
                found[info.name] = archive.extractfile(info).read()  # NOSONAR: bounded above
    except tarfile.TarError as error:
        raise NativeError(FORMAT) from error
    return found


def _write_new(path: Path, data: bytes, created: list[Path]) -> None:
    """Every directory and file this creates is appended to created, so a failed placement removes exactly them."""
    missing = [parent for parent in reversed(path.parents) if not parent.exists()]
    for folder in missing:
        folder.mkdir()
        created.append(folder)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, MEMBER_MODE)
    created.append(path)
    with os.fdopen(handle, "wb") as out:
        out.write(data)


def _remove(created: list[Path]) -> None:
    for path in reversed(created):
        with suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()


def _linked(root: Path, relative: PurePosixPath) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _granted(scope: Scope, authority: dict) -> bool:
    claimed = (authority.get("task_id"), authority.get("execution_id"), authority.get("task_generation"))
    return claimed == (scope.task_id, scope.execution_id, scope.generation)


def merged(manifest: dict, native: "NativeCapture | Unavailable") -> dict:
    fields = native.fields()
    inventory = {k: v for k, v in manifest.get("inventory", {}).items() if k != model.NATIVE_SESSION}
    hashes = {k: v for k, v in manifest.get("artifact_hashes", {}).items() if k != model.NATIVE_SESSION}
    exclusions = [*manifest.get("exclusions", [])]
    exclusions += [text for text in EXCLUSIONS if text not in exclusions]
    return {
        **manifest,
        **fields,
        "inventory": {**inventory, **fields.get("inventory", {})},
        "artifact_hashes": {**hashes, **fields.get("artifact_hashes", {})},
        "exclusions": exclusions,
    }


def _unqualified(harness: str, version_text: str, qualified: dict[str, tuple[str, ...]]) -> str | None:
    if harness not in HARNESSES:
        return "unqualified_harness"
    return None if line(version_text) in qualified.get(harness, ()) else "unqualified_version"


def capture(
    harness: str,
    home: Path,
    session_id: str,
    cwd: Path,
    version_text: str,
    stopped: bool,
    qualified: dict[str, tuple[str, ...]] = QUALIFIED,
) -> "NativeCapture | Unavailable":
    if problem := _unqualified(harness, version_text, qualified):
        return Unavailable(harness, problem)
    native = HARNESSES[harness]
    try:
        files = native.collect(home, session_id, cwd)
        found, recorded = native.identity(files)
    except NativeError as error:
        return Unavailable(harness, str(error))
    except OSError:
        return Unavailable(harness, UNREADABLE)
    if found != session_id:
        return Unavailable(harness, "identity_mismatch")
    if line(recorded) != line(version_text):
        return Unavailable(harness, "version_changed")
    try:
        scrubbed = {name: redaction.scrub(data) for name, data in sorted(files.items())}
    except redaction.Unredactable as error:
        return Unavailable(harness, str(error))
    kept = tuple((name, data) for name, (data, _) in scrubbed.items())
    if oversized(kept):
        return Unavailable(harness, TOO_LARGE)
    quiescence = "stopped_process" if stopped else "consistent_export"
    count = sum(found for _, found in scrubbed.values())
    return NativeCapture(harness, recorded, session_id, kept, quiescence, count)


class Qualifier:
    """Captures and restores one harness conversation through the attempt's granted store, counting each outcome.
    An empty qualification table disables native resume and leaves context continuation as the only path."""

    def __init__(self, validator: model.Validator, qualified: dict[str, tuple[str, ...]] = QUALIFIED) -> None:
        self.validator = validator
        self.qualified = qualified
        self.outcomes: Counter[str] = Counter()

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.outcomes)}

    def _count(self, harness: str, result):
        self.outcomes[f"{harness}/{result.outcome}"] += 1
        return result

    def capture(
        self, harness: str, home: Path, session_id: str, cwd: Path, version_text: str, stopped: bool
    ) -> "NativeCapture | Unavailable":
        return self._count(harness, capture(harness, home, session_id, cwd, version_text, stopped, self.qualified))

    def checkpoint(self, native: "NativeCapture | Unavailable", manifest: dict) -> model.Verdict:
        """The native material is stored before the manifest; an unavailable capture commits a context continuation."""
        if manifest.get("reconstruction", {}).get("harness") != native.harness:
            raise NativeError("harness_mismatch")
        doc = merged(manifest, native)
        store = self.validator.store
        if isinstance(native, NativeCapture) and _granted(store.scope, doc.get("authority", {})):
            store.put(f"{manifest['checkpoint_id']}.{model.NATIVE_SESSION}", native.material())
        return self.validator.commit(doc)

    def restore(self, checkpoint_id: str, home: Path, cwd: Path, version_text: str) -> "Resumed | Unavailable":
        """The home changes only after the checkpoint, the installed version and every member's identity check out."""
        verdict = self.validator.verify(checkpoint_id)
        store = self.validator.store
        if not verdict.authoritative:
            return self._count("unknown", Unavailable("unknown", verdict.reason))
        doc = json.loads(store.get_range(store.recorded(checkpoint_id)))
        harness = doc["reconstruction"]["harness"]
        if verdict.continuation != model.NATIVE:
            return self._count(harness, Unavailable(harness, "context_only"))
        if line(version_text) != line(doc["native_harness_version"]):
            return self._count(harness, Unavailable(harness, "version_changed"))
        if problem := _unqualified(harness, version_text, self.qualified):
            return self._count(harness, Unavailable(harness, problem))
        try:
            return self._count(harness, self._place(HARNESSES[harness], doc, home, cwd))
        except NativeError as error:
            return self._count(harness, Unavailable(harness, str(error)))

    def _place(self, native: Harness, doc: dict, home: Path, cwd: Path) -> Resumed:
        store = self.validator.store
        found = members(store.get_range(ArtifactRef(**doc["inventory"][model.NATIVE_SESSION])))
        session_id = doc["native_session_id"]
        if native.identity(found)[0] != session_id:
            raise NativeError("identity_mismatch")
        try:
            placed = [(native.placed(session_id, cwd, name), data) for name, data in found.items()]
        except ValueError as error:
            raise NativeError(ESCAPE) from error
        targets = [(_unlinked(home, home / relative), data) for relative, data in placed]
        if any(path.exists() for path, _ in targets):
            raise NativeError("conversation_present")
        created: list[Path] = []
        try:
            for path, data in targets:
                _write_new(path, data, created)
            if native.identity(native.collect(home, session_id, cwd))[0] != session_id:
                raise NativeError("identity_mismatch")
        except NativeError:
            _remove(created)
            raise
        except OSError as error:
            _remove(created)
            raise NativeError("placement_failed") from error
        return Resumed(native.name, session_id, native.command(session_id))
