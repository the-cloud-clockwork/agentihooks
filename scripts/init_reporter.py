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
    def __init__(self, stream: TextIO, verbose: bool = False, seen: set[str] | None = None) -> None:
        self.stream = stream
        self.verbose = verbose
        self.seen = seen if seen is not None else set()
        self.pending = ""
        self.diagnostic = str()

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
                elif self.verbose:
                    self.stream.write(line + "\n")
            self.continuation = bool(self.diagnostic and self.pending)
        return len(text)

    def _emit_diagnostic(self) -> None:
        if self.diagnostic:
            message = _ANSI.sub("", self.diagnostic)
            kept = "Kept your existing MCP server(s)" in message
            if (self.verbose or not kept) and message not in self.seen:
                self.stream.write(self.diagnostic + "\n")
                self.seen.add(message)
            self.diagnostic = str()

    def flush(self) -> None:
        self._emit_diagnostic()
        if self.pending and (self.verbose or sys.stdin.isatty() or _DIAGNOSTIC.search(_ANSI.sub("", self.pending))):
            self.stream.write(self.pending)
        self.pending = ""
        self.stream.flush()


@contextmanager
def capture_details(verbose: bool, seen: set[str] | None = None) -> Iterator[None]:
    output, errors = InstallOutput(sys.stdout, verbose, seen), InstallOutput(sys.stderr, verbose, seen)
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
    install_target: Callable[[], None],
    adapter: TargetAdapter,
    record: Callable[[], dict],
    *,
    verbose: bool = False,
    header: bool = True,
    seen: set[str] | None = None,
) -> None:
    with capture_details(verbose, seen):
        install_target()
        installed = record()
        counts = adapter.summary_counts()
    if header:
        profile = installed.get("profile", "default")
        chain = ", ".join(installed.get("profile_chain", profile.split(",")))
        overlays = installed.get("settings_profile") or "none"
        print("AgentiHooks installed")
        print(f"Bundle {installed.get('bundle') or 'none'}")
        if installed.get("user_bundle"):
            print(f"User bundle {installed['user_bundle']}")
        print(f"Profile {profile} | Chain {chain} | Overlays {overlays}")
    print(
        f"{adapter.name.capitalize()} {adapter.home()} | "
        f"{counts['mcp']} MCP servers, {counts['skills']} skills, {counts['plugins']} plugins"
    )


def previous_install(installer: ModuleType, state: dict, target: str) -> dict:
    from scripts.profiles.scaffold import base_roles

    roles = set(base_roles())
    for candidate in (target, "claude"):
        record = installer._global_record(state, candidate)
        if not record.get("profile"):
            continue
        names = record["profile"].split(",")
        if not any(name.strip().removeprefix("package:") in roles for name in names):
            return record
    return {}


def run_init(args: Namespace, installer: ModuleType) -> None:
    from scripts.herdr_setup import init_step
    from scripts.swarm_ledger import ledger_ca

    args.profile = args.init_profile
    args.settings_profile = args.init_settings_profile or ""
    args.install_target = args.init_target
    targets = (args.install_target,) if args.install_target else DEFAULT_TARGETS
    if args.with_copilot and "copilot" not in targets:
        targets = (*targets, "copilot")
    seen: set[str] = set()
    for index, target in enumerate(targets):
        target_args = deepcopy(args)
        target_args.install_target = target
        target_args.profile = args.profile or previous_install(installer, installer._load_state(), target).get(
            "profile"
        )
        if index:
            target_args.force = False
            target_args.bundle = None
            target_args.user_bundle = None
            target_args.link_profile = []

        def record() -> dict:
            installed = dict(installer._global_record(installer._load_state(), target))
            if installed:
                installed["profile_chain"] = [
                    name for name, _ in installer._resolve_profile_chain(installed["profile"])
                ]
            installed["bundle"] = installer._get_bundle_path()
            installed["user_bundle"] = installer._get_user_bundle_path()
            return installed

        report_target(
            lambda: installer.cmd_init_unified(target_args),
            installer.get_adapter(target),
            record,
            verbose=args.verbose,
            header=index == 0,
            seen=seen,
        )
    with capture_details(args.verbose):
        init_step(args.init_herdr, sys.stdin.isatty())
        print(ledger_ca.install())
