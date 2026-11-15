"""Every swarm runs on the events engine: no tick step launches an agent, no source under scripts brings the tick
launch back, a tick pass leaves a claimable task to the hub, and a new or unmarked config reads events."""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmConfig, SwarmError
from scripts.tick import registry

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
LAUNCH_STEPS = frozenset({"spawn", "spawn due", "preempt"})
STEP_TYPES = frozenset({"CallStep", "WriteStep"})
SPAWN_HELPERS = frozenset(
    {
        ("scripts/hive/spoke.py", "Spoke._spawn"),
        ("scripts/swarm/controller.py", "FencedRuntime._spawn"),
        ("scripts/swarm/dispatch_seat.py", "_spawn"),
        ("scripts/swarm/health/night_watch.py", "_spawn"),
    }
)
REFUSAL = ("scripts/swarm/store.py", "SwarmConfig.__post_init__")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (*FUNCTIONS, ast.ClassDef)
ENGINE = re.compile(r"\bengine\b")
ENGINE_LEGACY = re.compile(r"\bengine\s*=\s*legacy\b")
REFUSED_LEGACY = (
    "class SwarmConfig:\n"
    "    def __post_init__(self):\n"
    "        if self.engine == 'legacy':\n"
    "            raise ValueError('engine must be events')\n"
)
STEP_FILE = "scripts/tick/steps/planted.py"
PLANTED = (
    (STEP_FILE, "def _spawn(tick, get):\n    return []\n", "def _spawn"),
    (STEP_FILE, "class Place:\n    async def _spawn(self):\n        return []\n", "def Place._spawn"),
    ("scripts/hive/spoke.py", "def _spawn(event):\n    return None\n", "def _spawn"),
    (STEP_FILE, "STEPS = (CallStep('spawn', PLACEMENT, run),)\n", "step spawn"),
    (STEP_FILE, "STEPS = (steps.WriteStep('spawn due', PLACEMENT, run),)\n", "step spawn due"),
    (STEP_FILE, "STEPS = (CallStep(name='preempt', site=PLACEMENT),)\n", "step preempt"),
    ("scripts/swarm/store.py", "engine: str = 'legacy'\n", "legacy engine"),
    ("scripts/swarm/tick.py", "if config.engine == 'legacy':\n    pass\n", "legacy engine"),
    ("scripts/swarm/store.py", "raw.get('engine', 'legacy')\n", "legacy engine"),
    ("scripts/swarm/cli.py", "HELP = 'engine=legacy|events'\n", "legacy engine"),
    ("scripts/swarm/cli.py", REFUSED_LEGACY, "legacy engine"),
)
CLEAN = (
    (STEP_FILE, "def _reap(tick, get):\n    return []\n"),
    (STEP_FILE, "STEPS = (CallStep('rank pass', PLACEMENT, run), CallStep(PLACEMENT, name=NAME))\n"),
    ("scripts/hive/spoke.py", "class Spoke:\n    def _spawn(self, event):\n        return None\n"),
    ("scripts/swarm/engine_migration.py", "BUSY = 'skipped: live legacy agents'\n"),
    ("scripts/swarm/store.py", "TRANSPORT = 'legacy'\n"),
    ("scripts/swarm/cli.py", "def refuse():\n    '''Refuses engine=legacy from an old config.'''\n"),
    ("scripts/swarm/store.py", REFUSED_LEGACY),
)


def _scoped(node: ast.AST, scope: str) -> Iterator[tuple[ast.AST, str]]:
    """Every node below ``node`` with the dotted name of the function or class it sits in, its own name for a def."""
    for child in ast.iter_child_nodes(node):
        inner = f"{scope}.{child.name}".lstrip(".") if isinstance(child, SCOPES) else scope
        yield child, inner
        yield from _scoped(child, inner)


def _docstrings(tree: ast.Module) -> set[int]:
    """The ids of the docstring constants in ``tree``: prose, never a value the code runs on."""
    holders = [tree, *(node for node in ast.walk(tree) if isinstance(node, SCOPES))]
    firsts = [holder.body[0] for holder in holders if holder.body]
    return {
        id(first.value) for first in firsts if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
    }


def _step_name(node: ast.AST) -> str:
    """The name a ``CallStep`` or ``WriteStep`` call declares, positional or by keyword, else empty."""
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    named = [*node.args[:1], *(keyword.value for keyword in node.keywords if keyword.arg == "name")]
    names = [value.value for value in named if isinstance(value, ast.Constant) and isinstance(value.value, str)]
    return names[0] if called in STEP_TYPES and names else ""


def _own_expression(node: ast.AST) -> ast.AST | None:
    """The code a statement runs itself: a simple statement whole, the test of an if or while, nothing for a block."""
    if isinstance(node, (ast.If, ast.While)):
        return node.test
    if isinstance(node, ast.stmt) and not hasattr(node, "body") and not isinstance(node, ast.Match):
        return node
    return None


def _legacy_engine(node: ast.AST, text: str, docs: set[int]) -> bool:
    """Whether a statement names the legacy engine: a ``legacy`` value beside ``engine``, or ``engine=legacy`` text."""
    own = _own_expression(node)
    if own is None:
        return False
    values = [
        part.value
        for part in ast.walk(own)
        if isinstance(part, ast.Constant) and isinstance(part.value, str) and id(part) not in docs
    ]
    beside = "legacy" in values and ENGINE.search(ast.get_source_segment(text, own) or "") is not None
    return beside or any(ENGINE_LEGACY.search(value) for value in values)


def _findings(rel: str, text: str) -> Iterator[str]:
    """What in one source file brings the tick launch back, each as ``path:line: what``."""
    tree = ast.parse(text)
    docs = _docstrings(tree)
    for node, scope in _scoped(tree, ""):
        line = getattr(node, "lineno", 0)
        if isinstance(node, FUNCTIONS) and node.name == "_spawn" and (rel, scope) not in SPAWN_HELPERS:
            yield f"{rel}:{line}: def {scope}"
        if _step_name(node) in LAUNCH_STEPS:
            yield f"{rel}:{line}: step {_step_name(node)}"
        if (rel, scope) != REFUSAL and _legacy_engine(node, text, docs):
            yield f"{rel}:{line}: legacy engine"


def launch_findings(root: Path) -> list[str]:
    """Every place in the scripts tree under ``root`` that brings the tick launch back."""
    found: list[str] = []
    for path in sorted((root / "scripts").rglob("*.py")):
        found += _findings(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
    return found


def _spawn_scopes(path: Path) -> set[str]:
    """The dotted names of every ``_spawn`` defined in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {scope for node, scope in _scoped(tree, "") if isinstance(node, FUNCTIONS) and node.name == "_spawn"}


def _plant(root: Path, rel: str, source: str) -> None:
    """Writes ``source`` at ``rel`` under ``root`` as one planted scripts module."""
    planted = root / rel
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(source, encoding="utf-8")


@pytest.mark.feature("swarm-events-only-launch")
def test_no_source_under_scripts_brings_the_tick_launch_back() -> None:
    assert launch_findings(ROOT) == []


@pytest.mark.feature("swarm-events-only-launch")
def test_each_spawn_helper_the_scan_spares_is_still_defined() -> None:
    missing = sorted((rel, scope) for rel, scope in SPAWN_HELPERS if scope not in _spawn_scopes(ROOT / rel))
    assert missing == []


@pytest.mark.feature("swarm-events-only-launch")
@pytest.mark.parametrize(("rel", "source", "found"), PLANTED)
def test_the_scan_finds_a_planted_tick_launch(tmp_path: Path, rel: str, source: str, found: str) -> None:
    _plant(tmp_path, rel, source)
    assert [finding.split(": ", 1)[1] for finding in launch_findings(tmp_path)] == [found]


@pytest.mark.feature("swarm-events-only-launch")
@pytest.mark.parametrize(("rel", "source"), CLEAN)
def test_the_scan_spares_code_that_never_launches_from_the_tick(tmp_path: Path, rel: str, source: str) -> None:
    _plant(tmp_path, rel, source)
    assert launch_findings(tmp_path) == []


@pytest.mark.feature("swarm-events-only-launch")
def test_no_registered_tick_step_launches_an_agent() -> None:
    names = {step.name for step in registry.steps()}
    assert sorted(names & LAUNCH_STEPS) == []


@pytest.mark.feature("swarm-events-only-launch")
def test_a_tick_pass_over_an_open_claimable_task_spawns_no_agent_and_writes_no_claim() -> None:
    import fakeredis

    from scripts.swarm import capacity
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeRuntime, tasks

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    _, ready = capacity.ready_work("sw", store, ledger.state("sw"))
    row = ledger.rows["t1"]
    assert (runtime.spawned, runtime.tasks) == ([], [])
    assert (store.claimant("sw", "t1"), row["state"], row["claimed_by"]) == (None, "open", "")
    assert [task["id"] for task in ready["eng"]] == ["t1"]


@pytest.mark.feature("swarm-events-only-launch")
def test_a_config_built_without_an_engine_runs_on_events() -> None:
    assert SwarmConfig("sw", "/repo", 1, 1).engine == EVENTS_ENGINE


@pytest.mark.feature("swarm-events-only-launch")
def test_a_stored_config_without_an_engine_field_reads_as_events() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hdel(store.key("sw", "config"), "engine")
    assert store.config("sw").engine == EVENTS_ENGINE


@pytest.mark.feature("swarm-events-only-launch")
def test_a_config_naming_the_legacy_engine_is_refused() -> None:
    with pytest.raises(SwarmError) as refused:
        SwarmConfig("sw", "/repo", 1, 1, engine="legacy")
    assert str(refused.value) == "engine must be events"


@pytest.mark.feature("swarm-events-only-launch")
def test_a_stored_legacy_engine_reads_as_events() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hset(store.key("sw", "config"), "engine", "legacy")
    assert store.config("sw").engine == EVENTS_ENGINE


@pytest.mark.feature("swarm-events-only-launch")
def test_the_hub_reads_a_swarm_stored_with_the_legacy_engine_as_events() -> None:
    import fakeredis

    from scripts.hub import reconciler

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hset(store.key("sw", "config"), "engine", "legacy")
    assert reconciler.event_slugs(store) == ["sw"]
