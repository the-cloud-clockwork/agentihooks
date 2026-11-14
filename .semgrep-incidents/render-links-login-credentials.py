from pathlib import Path

SHARED_DIRS = ("projects", "sessions", "todos")
# ruleid: render-links-login-credentials
LEAKED = (*SHARED_DIRS, "plugins", ".credentials.json", "keybindings.json")
# ok: render-links-login-credentials
SHARED = (*SHARED_DIRS, "plugins", "keybindings.json")


def link_login(out: Path, shared: Path) -> None:
    # ruleid: render-links-login-credentials
    (out / ".credentials.json").symlink_to(shared / ".credentials.json")


def link_shared(out: Path, shared: Path) -> None:
    for item in SHARED:
        # ok: render-links-login-credentials
        (out / item).symlink_to(shared / item)
