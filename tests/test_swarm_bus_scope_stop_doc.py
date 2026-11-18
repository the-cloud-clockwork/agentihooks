from pathlib import Path

from scripts.events.scope import scoped_subject
from scripts.hive import execution_stop, nats_grant, stop_names

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "swarm-bus-scope.md"
HIVES = ROOT / "docs" / "pillars" / "hives.md"


def _section(text: str, heading: str) -> str:
    return text.split(f"{heading}\n", 1)[1].split("\n## ", 1)[0]


def test_doc_table_lists_the_stop_stream_of_each_swarm() -> None:
    table = _section(DOC.read_text(), "## Subjects and streams per swarm").splitlines()
    stream = stop_names.stream("rig").replace("-726967-", "-<hex>-")

    assert any(
        line.startswith("| ") and f"`{stream}`" in line and f"`swarm.<swarm>.{stop_names.FAMILY}.>`" in line
        for line in table
    )


def test_doc_names_the_wire_subject_and_the_durable_of_one_hives_stops() -> None:
    section = _section(DOC.read_text(), "## Execution stops")
    wire = scoped_subject("rig", stop_names.FAMILY, execution_stop.stop_subject("rig", "m1"))

    assert f"`{wire.replace('.rig.', '.<swarm>.').removesuffix('.m1')}.<hive>`" in section
    assert f"`{stop_names.consumer('rig', 'm1')}`" in section


def test_doc_lists_every_stop_grant_and_deny_of_a_scoped_member() -> None:
    section = _section(DOC.read_text(), "## Execution stops")
    consumer = stop_names.consumer("rig", "m1")

    for grant in nats_grant.stop_publish("rig", "m1"):
        assert f"`{grant.replace('-726967-', '-<hex>-').replace(consumer, '<consumer>')}`" in section, grant
    for subject in (*nats_grant.MEMBER_DENY, *nats_grant.MEMBER_SUB_DENY):
        assert f"`{subject}`" in section, subject


def test_doc_names_the_guards_of_the_stop_consumer() -> None:
    section = _section(DOC.read_text(), "## Execution stops")

    assert "`stop-on-events-log`" in section
    assert "`hive-own-stop-consumer`" in section


def test_hives_page_links_the_stop_section() -> None:
    assert "(../reference/swarm-bus-scope.md#execution-stops)" in HIVES.read_text()
