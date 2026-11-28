from typing import Any

from scripts import master_claude_login


def beat(fields: dict[str, Any]) -> None:
    # ruleid: hive-marker-only-master-login
    fields["master_login"] = {"claude": master_claude_login.signed_in()}


def ranked_beat(fields: dict[str, Any], logins: list[dict[str, Any]]) -> None:
    fields["master_logins"] = logins
    # ok: hive-marker-only-master-login
    fields["master_login"] = {"claude": logins[0]["account"] if logins else ""}
