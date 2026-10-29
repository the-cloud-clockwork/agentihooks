import getpass
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from scripts import openbao_kv, term_colors

ASK_STORE = "Do you want to store them in OpenBao?"
ASK_DETECTED = "OpenBao credentials detected ({url}). Use them?"
ASK_URL = "OpenBao URL: "
ASK_TOKEN = "OpenBao token: "
SKIP_OFF_TTY = "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)"
NEEDS_ENV = f"--openbao needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} off a terminal"
NEEDS_BOTH = "OpenBao needs both a URL and a token"


class StoreRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Console:
    ask: Callable[[str], str] = input
    hidden: Callable[[str], str] = getpass.getpass
    tty: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())

    def confirm(self, question: str, default: bool) -> bool:
        answer = self.ask(term_colors.yellow(f"{question} [{'Y/n' if default else 'y/N'}] ")).strip().lower()
        return default if not answer else answer.startswith("y")


def target(choice: bool | None, environ: Mapping[str, str], console: Console, opener) -> openbao_kv.OpenBao | None:
    if choice is False:
        return None
    if choice is None and not console.tty():
        print(term_colors.yellow(SKIP_OFF_TTY))
        return None
    if choice is None and not console.confirm(ASK_STORE, False):
        return None
    detected = openbao_kv.from_env(environ, opener)
    if detected and (choice or console.confirm(ASK_DETECTED.format(url=detected.url), True)):
        return detected
    if not console.tty():
        raise StoreRefused(NEEDS_ENV)
    url = console.ask(term_colors.yellow(ASK_URL)).strip()
    token = console.hidden(term_colors.yellow(ASK_TOKEN)).strip()
    if not (url and token):
        raise StoreRefused(NEEDS_BOTH)
    return openbao_kv.OpenBao(url, token, opener)


def store(bao: openbao_kv.OpenBao, path: str, entries: dict[str, str], stale: tuple[str, ...]) -> int:
    try:
        total = openbao_kv.merge(bao, path, entries, stale)
    except openbao_kv.OpenBaoError as exc:
        print(term_colors.red(f"OpenBao write failed: {exc}"))
        return 1
    print(openbao_kv.summary(path, entries, total))
    return 0
