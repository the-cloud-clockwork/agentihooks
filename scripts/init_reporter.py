from __future__ import annotations

import re
import sys
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import TextIOBase
from pathlib import Path
from types import ModuleType
from typing import TextIO

from scripts.targets import DEFAULT_TARGETS, TargetAdapter

_DIAGNOSTIC = re.compile(r"\[(?:WARN|!!)\]|\b(?:warning|error|failed|could not|not found)\b", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class InstallOutput(TextIOBase):
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.pending = ""
        self.diagnostic = ""
        self.continuation = False

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if self.diagnostic and (self.continuation or line.startswith("       ")):
                self.diagnostic += " " + line.strip()
            else:
                self._emit_diagnostic()
                if _DIAGNOSTIC.search(_ANSI.sub("", line)):
                    self.diagnostic = line
            self.continuation = bool(self.diagnostic and self.pending)
        return len(text)

    def _emit_diagnostic(self) -> None:
        if self.diagnostic:
            self.stream.write(self.diagnostic + "\n")
            self.diagnostic = ""

    def flush(self) -> None:
        self._emit_diagnostic()
        if self.pending and (sys.stdin.isatty() or _DIAGNOSTIC.search(_ANSI.sub("", self.pending))):
            self.stream.write(self.pending)
        self.pending = ""
        self.stream.flush()


@contextmanager
def capture_details(verbose: bool) -> Iterator[None]:
    if verbose:
        yield
    else:
        output, errors = InstallOutput(sys.stdout), InstallOutput(sys.stderr)
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                yield
            finally:
                output.flush()
                errors.flush()


def installed_counts(servers: dict, skills_home: Path, plugins: dict) -> dict[str, int]:
    return {
        "mcp": len(servers),
        "skills": sum(1 for skill in skills_home.glob("*/SKILL.md") if skill.is_file()),
        "plugins": sum(bool(enabled) for enabled in plugins.values()),
    }


def report_target(
    install_target: Callable[[], None], adapter: TargetAdapter, record: Callable[[], dict], *, verbose: bool = False
) -> None:
    with capture_details(verbose):
        install_target()
        installed = record()
        counts = adapter.summary_counts()
    profile = installed.get("profile", "default")
    chain = ",".join(installed.get("profile_chain", profile.split(",")))
    print(
        f"{adapter.name} {adapter.home()} | profile={profile} | chain={chain} | "
        f"mcp={counts['mcp']} skills={counts['skills']} plugins={counts['plugins']}"
    )


def run_init(args: Namespace, installer: ModuleType) -> None:
    from scripts.herdr_setup import init_step

    args.profile = args.init_profile
    args.settings_profile = args.init_settings_profile or ""
    args.install_target = args.init_target
    targets = (args.install_target,) if args.install_target else DEFAULT_TARGETS
    if args.with_copilot and "copilot" not in targets:
        targets = (*targets, "copilot")
    for index, target in enumerate(targets):
        target_args = deepcopy(args)
        target_args.install_target = target
        if index:
            target_args.force = False
            target_args.bundle = None
            target_args.link_profile = []

        def record() -> dict:
            installed = dict(installer._global_record(installer._load_state(), target))
            if installed:
                installed["profile_chain"] = [
                    name for name, _ in installer._resolve_profile_chain(installed["profile"])
                ]
            return installed

        report_target(
            lambda: installer.cmd_init_unified(target_args),
            installer.get_adapter(target),
            record,
            verbose=args.verbose,
        )
    with capture_details(args.verbose):
        init_step(args.init_herdr, sys.stdin.isatty())
