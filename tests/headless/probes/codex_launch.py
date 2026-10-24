import os
import sys

from scripts import codex_router


def main(argv: list[str]) -> None:
    environ = dict(os.environ)
    accounts = sorted(codex_router.token_accounts(environ), key=lambda a: a.name)
    if not accounts:
        sys.exit("codex_launch: no AH_CX_TOKEN_* in the environment")
    wanted = environ.get("HDL0_CODEX_ACCOUNT")
    account = next((a for a in accounts if a.name == wanted), accounts[0])
    account = codex_router.credential(account, environ)
    child = codex_router.child_environment(account, environ)
    command = codex_router.command(account, environ.get("HDL0_CODEX_BIN", "codex"), argv)
    os.execvpe(command[0], command, child)


if __name__ == "__main__":
    main(sys.argv[1:])
