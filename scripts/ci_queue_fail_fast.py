import re
import subprocess

from scripts.ci_queue_retry import FAILED_RESULT, STEP_EXITED, job_text, log_errors, runner_lost

SELF = "queue-fail-fast"
EXITED = re.compile(r"##\[error\]" + STEP_EXITED.pattern)


def logged_loss(repo: str, job: dict, seen: dict[str, bool]) -> bool:
    # A lost runner may leave no log or only an annotation; only a step that exited nonzero proves a real failure.
    key = str(job["id"])
    if key not in seen:
        try:
            errors = log_errors(repo, job["id"])
        except subprocess.CalledProcessError:
            try:
                text = job_text(repo, job["id"], False)
            except subprocess.CalledProcessError:
                return True
            return runner_lost(text) or not (FAILED_RESULT.search(text) or STEP_EXITED.search(text))
        text = "\n".join(errors)
        seen[key] = runner_lost(text) or not (FAILED_RESULT.search(text) or EXITED.search(text))
    return seen[key]
