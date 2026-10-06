"""Verdict files a gate reads at the next call, written by the tick or the Doctor: gates/<gate>/<subject>, reason inside."""

import json
import os
import re
import time

from scripts.gates.log import gates_dir

UNSAFE = re.compile(r"[^A-Za-z0-9@_-]")


def safe_name(text):
    return UNSAFE.sub("_", text) or "_"


class Verdicts:
    def __init__(self, slug, gate, home=None):
        self.slug, self.gate, self.home = slug, gate, home

    def path(self, subject):
        return gates_dir(self.slug, self.home) / self.gate / safe_name(subject)

    def write(self, subject, verdict, reason, now_ms=None):
        record = {"verdict": verdict, "reason": reason, "at": int(time.time() * 1000) if now_ms is None else now_ms}
        path = self.path(subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f".{path.name}.{os.getpid()}")
        staged.write_text(json.dumps(record), encoding="utf-8")
        os.replace(staged, path)
        return record

    def read(self, subject):
        try:
            record = json.loads(self.path(subject).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def clear(self, subject):
        self.path(subject).unlink(missing_ok=True)
