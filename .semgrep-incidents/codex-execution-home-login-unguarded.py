from pathlib import Path


def every_launch(spec: object, source: Path, home: Path) -> None:
    auth = source / "auth.json"
    if auth.is_file():
        # ruleid: codex-execution-home-login-unguarded
        (home / "auth.json").symlink_to(auth.resolve())


def routed_only(spec: object, source: Path, home: Path) -> None:
    auth = source / "auth.json"
    if auth.is_file() and spec.allowed_tokens:
        # ruleid: codex-execution-home-login-unguarded
        (home / "auth.json").symlink_to(auth.resolve())


def default_login_only(spec: object, source: Path, home: Path) -> None:
    auth = source / "auth.json"
    if auth.is_file() and not spec.allowed_tokens:
        # ok: codex-execution-home-login-unguarded
        (home / "auth.json").symlink_to(auth.resolve())
