from collections.abc import Callable

from scripts.forge.model import Forge
from scripts.review.record import Finding


def posted(forge: Forge, number: int, body: str, by: str, task: str) -> Finding:
    # ok: review-record-write-skips-forge-post
    return Finding("", body, forge.post(number, body), by, task)


def posted_by_keyword(forge: Forge, number: int, body: str) -> Finding:
    # ok: review-record-write-skips-forge-post
    return Finding(id="", body=body, comment=forge.post(number, body), by="r", task="t")


def posted_before_the_write(forge: Forge, number: int, body: str, by: str, task: str) -> Callable[[int], Finding]:
    comment = forge.post(number, body)

    def append(count: int) -> Finding:
        # ok: review-record-write-skips-forge-post
        return Finding(f"f{count + 1}", body, comment, by, task)

    return append


def posted_another_comment(forge: Forge, number: int, body: str, other: str) -> Finding:
    comment = forge.post(number, body)
    # ruleid: review-record-write-skips-forge-post
    return Finding(comment, body, other, "r", "t")


def unposted(body: str, by: str, task: str) -> Finding:
    # ruleid: review-record-write-skips-forge-post
    return Finding("", body, "", by, task)


def copied_comment(comment: str, body: str) -> Finding:
    # ruleid: review-record-write-skips-forge-post
    return Finding(id="", body=body, comment=comment, by="r", task="t")


def _decode(item: dict[str, str]) -> Finding:
    # ok: review-record-write-skips-forge-post
    return Finding(id=item["id"], body=item["body"], comment=item["comment"], by=item["by"], task=item["task"])
