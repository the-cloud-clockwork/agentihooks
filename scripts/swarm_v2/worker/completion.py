import json
from pathlib import Path

from scripts.swarm_v2.supervision_protocol import decode

SCHEMA_VERSION = 1
TERMINATION_LOG = Path("/dev/termination-log")


def report(scope: dict, fields: dict) -> None:
    if fields.get("status") != "complete" or not TERMINATION_LOG.is_file():
        return
    authority = scope["authority"]
    TERMINATION_LOG.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "execution_id": authority["execution_id"],
                "generation": authority["generation"],
                "transcript_end": fields["transcript_end"],
            }
        ),
        encoding="utf-8",
    )


def transcript_end(message: str, execution_id: str, generation: int) -> int | None:
    value = decode(message)
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("execution_id") != execution_id
        or type(value.get("generation")) is not int
        or value["generation"] != generation
        or type(value.get("transcript_end")) is not int
        or value["transcript_end"] < 0
    ):
        return None
    return value["transcript_end"]
