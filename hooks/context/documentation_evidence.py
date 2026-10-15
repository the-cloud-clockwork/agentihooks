import re
import subprocess
from pathlib import Path

from hooks.context.credential_guard import sensitive_kind
from hooks.secrets import redact

DOC_TYPES = {".md", ".mdx", ".rst", ".adoc"}
CODE_TYPES = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".json", ".toml", ".yaml", ".yml"}
COMMON = {"class", "return", "True", "False", "None", "import", "from", "self", "pass", "with", "else"}


import json


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=10).strip()


def matches(root: Path, sources: list[str], text: str) -> list[dict]:
    terms = list(dict.fromkeys(word for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text) if word not in COMMON))[
        :32
    ]
    if not terms:
        return []
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + r")\b", re.IGNORECASE)
    found = []
    for name in sources:
        for number, line in enumerate((root / name).read_text(errors="replace").splitlines(), 1):
            if pattern.search(line):
                found.append({"path": name, "line": number, "text": redact(line[:256], mode="memory")})
                if len(found) == 80:
                    return found
    return found


def collect(root: Path, base: str, context: dict | None = None) -> dict:
    root = Path(root)
    context = context or {}
    try:
        head = git(root, "rev-parse", "--verify", "--end-of-options", context.get("branch") or "HEAD")
    except subprocess.CalledProcessError:
        head = git(root, "rev-parse", "HEAD")
    base = git(root, "merge-base", base, head) if base else head
    names = git(root, "diff", "--name-only", "-z", base, head).split("\0")
    files = [
        name for name in names if name and not sensitive_kind(name) and Path(name).suffix in CODE_TYPES | DOC_TYPES
    ]
    changes = git(root, "diff", "--no-ext-diff", "--unified=3", base, head, "--", *files[:80]) if files else ""
    tracked = git(root, "ls-files", "-z").split("\0")
    sources = sorted(
        name
        for name in tracked
        if Path(name).suffix in DOC_TYPES
        and Path(name).name not in {"AGENTS.md", "CLAUDE.md"}
        and not (root / name).is_symlink()
        and (root / name).is_file()
        and (root / name).stat().st_size <= 131072
    )[:200]
    content = "\n".join(
        line[1:] for line in changes.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    return {
        "head": head,
        "files": files,
        "changes": redact(changes[:24000], mode="memory"),
        "summary": redact(git(root, "log", "-1", "--format=%s", head), mode="memory"),
        "doc_sources": sources,
        "docs": matches(root, sources, content or context.get("description", context.get("title", ""))),
        "source_task": json.loads(redact(json.dumps(context), mode="memory")),
    }
