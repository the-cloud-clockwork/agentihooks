import subprocess

MAIL_TO = "AGENTIHOOKS_OPERATOR_EMAIL"
NOTIFY_ENV = "NOTIFICATIONS_API_KEY"
NOTIFY_URL = "AGENTIHOOKS_NOTIFICATIONS_URL"
DEFAULT_NOTIFY_URL = "https://email.homeofanton.com/api/v1/send"


def send(subject: str, text: str, environ: dict, repo: str) -> str:
    """An empty answer when sent, else why not. SMTP when its relay answers, else the Anton notifications API."""
    to = environ.get(MAIL_TO) or _git_email(repo)
    if not to:
        return f"no operator email: set {MAIL_TO}"
    from hooks.integrations import mailer

    if not mailer.EmailClient.get_client().reachable():
        return _notify(to, subject, text, environ)
    result = mailer.send_email(to, subject, body=text)
    return "" if result.success else f"email failed: {result.error}"


def _notify(to: str, subject: str, text: str, environ: dict) -> str:
    bearer = environ.get(NOTIFY_ENV)
    if not bearer:
        return f"email failed: the SMTP relay is unreachable and {NOTIFY_ENV} is not set"
    import httpx

    try:
        answer = httpx.post(
            environ.get(NOTIFY_URL) or DEFAULT_NOTIFY_URL,
            headers={"Authorization": "Bearer " + bearer},
            json={"to": to, "subject": subject, "body": text},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        return f"email failed: {exc}"
    return "" if answer.status_code == 200 else f"email failed: notifications answered {answer.status_code}"


def _git_email(repo: str) -> str:
    try:
        found = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip()
