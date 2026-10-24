import subprocess

MAIL_TO = "AGENTIHOOKS_OPERATOR_EMAIL"


def send(subject: str, text: str, environ: dict, repo: str) -> str:
    """An empty answer when sent, else why not."""
    to = environ.get(MAIL_TO) or _git_email(repo)
    if not to:
        return f"no operator email: set {MAIL_TO}"
    from hooks.integrations.mailer import send_email

    result = send_email(to, subject, body=text)
    return "" if result.success else f"email failed: {result.error}"


def _git_email(repo: str) -> str:
    try:
        found = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip()
