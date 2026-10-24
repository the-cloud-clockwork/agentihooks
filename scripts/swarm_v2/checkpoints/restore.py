"""Restore on a replacement attempt. Native continuity is reported only after the checkpoint verifies through the
attempt's granted store and the restored conversation is the one the recovery decision expects; a context-only
restore is reported as reconstructed, and only when the decision chose it."""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.swarm_v2.checkpoints import git_capture, model, native

METRIC = "restore_outcomes_by_type"
NATIVE, RECONSTRUCTED, FRESH = "native", "reconstructed", "fresh"
TYPES = (NATIVE, RECONSTRUCTED, FRESH)
RESTORING = (NATIVE, RECONSTRUCTED)
REFUSED = "refused"
STORE_MISSING = "no checkpoint store is configured for this worker"

Source = Callable[[model.Validator, str, Path], model.Verdict]


class RestoreRefused(ValueError):
    pass


@dataclass(frozen=True)
class Directive:
    """What a launch record names: the checkpoint, the recovery type the decision accepted, the native session the
    fenced attempt held and the durable transcript watermark the restore continues from."""

    checkpoint_id: str
    recovery: str
    session_id: str
    watermark: int

    @classmethod
    def parse(cls, value: object) -> "Directive":
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RestoreRefused("malformed restore directive")
        directive = cls(**value)
        if directive.recovery not in TYPES or type(directive.watermark) is not int or directive.watermark < 0:
            raise RestoreRefused("malformed restore directive")
        if not all(isinstance(text, str) for text in (directive.checkpoint_id, directive.session_id)):
            raise RestoreRefused("malformed restore directive")
        if (directive.recovery == FRESH) != (directive.checkpoint_id == ""):
            raise RestoreRefused("malformed restore directive")
        if (directive.recovery == NATIVE) != bool(directive.session_id):
            raise RestoreRefused("malformed restore directive")
        return directive

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Restored:
    recovery: str
    checkpoint_id: str
    watermark: int
    session_id: str = ""
    command: tuple[str, ...] = ()


class Restorer:
    """Native state lands before source state, so a version or identity refusal leaves the work tree untouched."""

    def __init__(self, qualifier: native.Qualifier, source: Source = git_capture.restore) -> None:
        self.qualifier, self.source = qualifier, source
        self.outcomes: Counter[str] = Counter()

    @property
    def validator(self) -> model.Validator:
        return self.qualifier.validator

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.outcomes)}

    def restore(self, directive: Directive, worktree: Path, home: Path, cwd: Path, version_text: str) -> Restored:
        if directive.recovery == FRESH:
            return self._count(Restored(FRESH, "", directive.watermark))
        verdict = self.validator.verify(directive.checkpoint_id)
        if not verdict.authoritative:
            return self._refuse(verdict.reason)
        if directive.recovery == NATIVE and verdict.continuation != model.NATIVE:
            return self._refuse("context_only")
        doc = self._manifest(directive.checkpoint_id)
        watermark = doc["transcript_durable_cursor"]["sequence"]
        resumed = None
        if directive.recovery == NATIVE:
            if doc["native_session_id"] != directive.session_id:
                return self._refuse("conversation_mismatch")
            resumed = self.qualifier.restore(directive.checkpoint_id, home, cwd, version_text)
            if isinstance(resumed, native.Unavailable):
                return self._refuse(resumed.cause)
        if git_capture.SOURCE_ROLES & doc["inventory"].keys():
            try:
                self.source(self.validator, directive.checkpoint_id, worktree)
            except (git_capture.SourceError, OSError) as error:
                return self._refuse(f"source: {error}")
        if resumed is None:
            return self._count(Restored(RECONSTRUCTED, directive.checkpoint_id, watermark))
        restored = Restored(NATIVE, directive.checkpoint_id, watermark, resumed.session_id, resumed.command)
        return self._count(restored)

    def _manifest(self, checkpoint_id: str) -> dict:
        store = self.validator.store
        return json.loads(store.get_range(store.recorded(checkpoint_id)))

    def _count(self, restored: Restored) -> Restored:
        self.outcomes[restored.recovery] += 1
        return restored

    def _refuse(self, reason: str) -> Restored:
        self.outcomes[f"{REFUSED}/{reason.split(':')[0]}"] += 1
        raise RestoreRefused(reason)


@dataclass(frozen=True)
class Paths:
    worktree: Path
    home: Path
    cwd: Path
    version_text: str


def launched(spec: dict, restorer: Restorer | None = None, paths: Paths | None = None) -> Restored | None:
    """The restore a worker launch record names, run before the agent starts; None when it names none."""
    if "restore" not in spec:
        return None
    directive = Directive.parse(spec["restore"])
    if directive.recovery == FRESH:
        return Restored(FRESH, "", directive.watermark)
    if restorer is None or paths is None:
        raise RestoreRefused(STORE_MISSING)
    return restorer.restore(directive, paths.worktree, paths.home, paths.cwd, paths.version_text)
