import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def authorization_url(text: str) -> str | None:
    match = re.search(r"https://(?:claude\.ai|claude\.com)/(?:cai/)?oauth/authorize\?[^\s\x1b]+", text)
    return match.group(0) if match else None


class AuthorizationOutput:
    def __init__(self, stream):
        self.stream = stream
        self.url = ""

    def drain(self) -> None:
        for line in iter(lambda: self.stream.readline(16384), ""):
            if not self.url:
                self.url = authorization_url(line) or ""

    def read_text(self) -> str:
        return self.url


class NativeAttempt:
    def __init__(self, process):
        self.process = process
        self.output = AuthorizationOutput(process.stdout)
        self.reader = threading.Thread(target=self.output.drain, daemon=True)
        self.completed = None

    def complete(self) -> bool:
        if self.completed is not None:
            return self.completed
        self.completed = False
        state = parse_qs(urlsplit(self.output.read_text()).query).get("state", [])
        command = shutil.which("powershell.exe")
        if len(state) != 1 or not command:
            return False
        script = (
            "$ErrorActionPreference = 'Stop'; $value = Get-Clipboard -Raw; Set-Clipboard -Value ''; "
            "if ($value.Length -le 4096) { [Console]::Out.Write($value) }"
        )
        try:
            captured = subprocess.run(
                [command, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            value = captured.stdout.strip()
            if captured.returncode or len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+#[A-Za-z0-9_-]+", value):
                return False
            if value.split("#")[1] != state[0]:
                return False
            self.process.stdin.write(value + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        self.completed = True
        return True


@contextmanager
def start(environ: Mapping[str, str], target: Path, stop: Callable) -> Iterator[NativeAttempt]:
    from scripts import master_claude_login as login

    process = subprocess.Popen(
        [shutil.which("claude") or "claude", *login.SETTINGS, *login.LOGIN],
        env={**login.child(environ, target), "BROWSER": "true"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    attempt = NativeAttempt(process)
    attempt.reader.start()
    try:
        yield attempt
    finally:
        stop(process)
        attempt.reader.join(timeout=5)
        process.stdin.close()
        process.stdout.close()
