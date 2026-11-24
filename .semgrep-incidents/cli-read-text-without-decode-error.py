import json
from pathlib import Path


class SwarmError(RuntimeError):
    pass


def read_catching_os_error_alone(path: str, what: str) -> str:
    try:
        # ruleid: cli-read-text-without-decode-error
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {path}: {exc.strerror}") from exc


def read_catching_decode_errors_by_name(path: str, what: str) -> str:
    try:
        # ok: cli-read-text-without-decode-error
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SwarmError(f"cannot read the {what} {path}: it is not UTF-8 text") from exc


def read_fixture_outside_a_try(path: str) -> dict:
    # ok: cli-read-text-without-decode-error
    return json.loads(Path(path).read_text())
