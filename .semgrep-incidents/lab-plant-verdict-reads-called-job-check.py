from dataclasses import dataclass


@dataclass(frozen=True)
class Pair:
    gate: str


def reads_the_called_job(pair: Pair) -> str:
    # ruleid: lab-plant-verdict-reads-called-job-check
    return pair.gate.rpartition("/")[0] or pair.gate


def reads_the_required_job(pair: Pair) -> str:
    # ok: lab-plant-verdict-reads-called-job-check
    return pair.gate.partition("/")[0]
