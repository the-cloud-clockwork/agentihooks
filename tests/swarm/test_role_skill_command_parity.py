"""Each package role skill names only the close and post commands its role's launch prompts name."""

import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.review import cli as review_cli
from scripts.review import record
from scripts.swarm import agent_up, naming, prompt, templates
from scripts.swarm.store import AUTONOMY
from tests.review.fakes import SLUG, store

pytestmark = pytest.mark.unit

ROLES = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles"
SKILLS = sorted(ROLES.glob("*/.claude/skills/*/SKILL.md"))
REVIEWER = ROLES / "reviewer" / ".claude" / "skills" / "swarm-reviewer" / "SKILL.md"
CONTINUATION = re.compile(r"\\\n\s*")
COMMAND = re.compile(
    r"agentihooks (?:swarm \S+ (?P<verb>done|ready|park|block)|(?P<post>review post|forge post))(?P<rest>[^`\n;]*)"
)
SENTENCE_END = re.compile(r"[.,](?:\s|$)")
OPTION = re.compile(r"(?<!\S)--[\w-]+")
REVIEW_POST = re.compile(r"agentihooks review (post\b[^`\n]*)")
TASK = {"id": "t1", "title": "x", "phase": "p1", "generation": 1, "mission": "mission.md"}


def commands(text: str) -> set[tuple[str, str]]:
    """Return each close or post command the text names with each option it passes, and once with no option."""
    found: set[tuple[str, str]] = set()
    for match in COMMAND.finditer(CONTINUATION.sub(" ", text)):
        command = match["verb"] or match["post"]
        options = OPTION.findall(SENTENCE_END.split(match["rest"], 1)[0])
        found |= {(command, ""), *((command, option) for option in options)}
    return found


def lanes(role: str) -> set[str]:
    """Return every lane whose agents launch with the role's profile."""
    found = {lane for lane, profile in templates.DEFAULT_PROFILES.items() if profile == role}
    return found | ({agent_up.KINDS[role]} if role in agent_up.KINDS else set())


def kinds(lane: str) -> tuple[str, ...]:
    """Return the task kinds a lane launches: its template default kind, else every kind no lane defaults to."""
    if lane in templates.DEFAULT_KINDS:
        return (templates.DEFAULT_KINDS[lane],)
    return tuple(kind for kind in prompt.STEPS if kind not in templates.DEFAULT_KINDS.values())


def prompts(lane: str) -> Iterator[str]:
    """Yield the lane's launch prompt for each kind it launches at each autonomy level."""
    tasks = [TASK] if lane in prompt.SEATS else [{**TASK, "kind": kind} for kind in kinds(lane)]
    for task in tasks:
        for autonomy in AUTONOMY:
            yield prompt.build(SLUG, "/repo", lane, f"{naming.TYPES[lane]}@a1b2c3-0001", task, autonomy=autonomy)


def offered(role: str) -> set[tuple[str, str]]:
    """Return every close or post command some launch prompt of the role names."""
    return {command for lane in lanes(role) for text in prompts(lane) for command in commands(text)}


def skill_id(path: Path) -> str:
    return f"{path.relative_to(ROLES).parts[0]}/{path.parent.name}"


def post_section(text: str) -> str:
    """Return the body of the skill's Post section."""
    return text.split("\n## Post\n", 1)[1].split("\n## ", 1)[0]


def review_post_argv(text: str) -> list[str]:
    """Return the arguments after agentihooks review of the first review post command the text names."""
    found = REVIEW_POST.search(CONTINUATION.sub(" ", text))
    assert found, "names no agentihooks review post command"
    return shlex.split(found[1])


def given(argv: list[str]) -> set[str]:
    """Return the review command options the arguments set."""
    return {key for key, value in vars(review_cli.parser().parse_args(argv)).items() if value}


@pytest.mark.feature("role-skill-command-parity")
@pytest.mark.parametrize("skill", SKILLS, ids=skill_id)
def test_role_skill_close_and_post_commands_match_its_launch_prompt(skill: Path) -> None:
    named = commands(skill.read_text())
    assert sorted(named - offered(skill.relative_to(ROLES).parts[0])) == []


@pytest.mark.feature("role-skill-command-parity")
def test_reviewer_post_step_passes_the_review_record_refusal_options() -> None:
    refusal = record.done_refusal(store(), SLUG, "review", "review", "r1")
    assert refusal is not None
    step = post_section(REVIEWER.read_text())
    assert given(review_post_argv(step)) == given(review_post_argv(refusal))
