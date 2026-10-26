from dataclasses import dataclass, field, fields, is_dataclass
from typing import get_args, get_origin

CHECKBOX = {"checkbox": True}


@dataclass
class Record:
    id: str


@dataclass
class Entry(Record):
    by: str
    at: int
    text: str


@dataclass
class Note(Entry):
    comments: list[Entry]


@dataclass
class Task(Record):
    title: str
    description: str
    phase: str
    lane: str
    state: str
    claimed_by: str
    issue_url: str
    pr_url: str
    done: bool = field(metadata=CHECKBOX)
    out_of_scope: bool = field(metadata=CHECKBOX)
    comments: list[Entry]


@dataclass
class Phase(Record):
    title: str
    description: str
    done: bool = field(metadata=CHECKBOX)
    out_of_scope: bool = field(metadata=CHECKBOX)
    depends_on: list[str]
    planning: str
    release: bool
    comments: list[Entry]


@dataclass
class Question(Record):
    text: str
    out_of_scope: bool = field(metadata=CHECKBOX)
    comments: list[Entry]
    answers: list[Entry]


@dataclass
class FollowUp(Record):
    text: str
    done: bool = field(metadata=CHECKBOX)
    out_of_scope: bool = field(metadata=CHECKBOX)
    comments: list[Entry]


@dataclass
class Plan(Record):
    out_of_scope: bool = field(metadata=CHECKBOX)


@dataclass
class Priority(Record):
    pass


@dataclass
class Notification(Record):
    pass


@dataclass
class Artifact(Record):
    pass


@dataclass
class Alert(Record):
    pass


@dataclass
class Freeze(Record):
    pass


@dataclass
class Slice(Record):
    pass


@dataclass
class Ledger:
    title: str
    overview: str
    orchestrator: str
    chat_instructions: str
    tasks: list[Task]
    phases: list[Phase]
    questions: list[Question]
    followups: list[FollowUp]
    notes: list[Note]
    chat: list[Entry]
    priorities: list[Priority]
    notifications: list[Notification]
    artifacts: list[Artifact]
    artifact_trash: list[Artifact]
    sources: list[str]
    alerts: list[Alert]
    freezes: list[Freeze]
    plans: list[Plan]
    slices: list[Slice]


def item_type(annotation: object) -> object:
    return get_args(annotation)[0] if get_origin(annotation) is list else None


def threads(model: type) -> tuple:
    return tuple(f.name for f in fields(model) if is_dataclass(item_type(f.type)))


def checkboxes(model: type) -> tuple:
    return tuple(f.name for f in fields(model) if f.metadata.get("checkbox"))


def shape(model: type) -> tuple:
    return tuple(f.name for f in fields(model) if f.name != "id" and f.name not in threads(model))


def field_type(collection: str, name: str) -> type:
    annotation = next(f.type for f in fields(MODELS[collection]) if f.name == name)
    return get_origin(annotation) or annotation


COLLECTIONS = tuple(f.name for f in fields(Ledger) if item_type(f.type) is not None)
MODELS = {f.name: item_type(f.type) for f in fields(Ledger) if is_dataclass(item_type(f.type))}
RECORD_COLLECTIONS = frozenset(MODELS)
TEXT_FIELDS = tuple(f.name for f in fields(Ledger) if f.type is str)
LISTS = {name: shape(model) for name, model in MODELS.items() if checkboxes(model)}
THREADS = {name: threads(model) for name, model in MODELS.items() if threads(model)}
THREAD_NAMES = frozenset(thread for names in THREADS.values() for thread in names)
CHECKBOXES = tuple(dict.fromkeys(box for model in MODELS.values() for box in checkboxes(model)))
