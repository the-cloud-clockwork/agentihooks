from scripts.gates import intent


def per_task_text(slug: str, mode: str, now: int, ledger: object, mail: object) -> object:
    # ruleid: intent-check-per-task-read
    return intent.Check(slug, mode, now, ledger, mail, intent.pr_view, intent.judged, text=intent.pr_text, limit=2)


def head_from_pass_view(slug: str, mode: str, now: int, ledger: object, mail: object, view: object) -> object:
    # ruleid: intent-check-per-task-read
    return intent.Check(
        slug,
        mode,
        now,
        ledger,
        mail,
        intent.pr_view,
        intent.judged,
        head=lambda url: getattr(view(url), "head", None),
        limit=2,
    )


def unbounded(slug: str, mode: str, now: int, ledger: object, mail: object, pulls: intent.Pulls) -> object:
    # ruleid: intent-check-per-task-read
    return intent.Check(slug, mode, now, ledger, mail, intent.pr_view, intent.judged, text=pulls.text)


def batched(slug: str, mode: str, now: int, ledger: object, mail: object, pulls: intent.Pulls) -> object:
    # ok: intent-check-per-task-read
    return intent.Check(slug, mode, now, ledger, mail, intent.pr_view, intent.judged, text=pulls.text, limit=2)
