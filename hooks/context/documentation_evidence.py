import re
import subprocess
from pathlib import Path

from hooks.context.credential_guard import sensitive_kind
from hooks.secrets import redact

DOC_TYPES = {".md", ".mdx", ".rst", ".adoc"}
CODE_TYPES = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".json", ".toml", ".yaml", ".yml"}
COMMON = {"class", "return", "True", "False", "None", "import", "from", "self", "pass", "with", "else"}


import json

GIT_OPTIONS = {"text": True, "stderr": subprocess.DEVNULL, "timeout": 10}
VERIFY_REVISION = ("rev-parse", "--verify", "--end-of-options")
DEFAULT_REVISION = "HEAD"
INTEGRATION = "refs/remotes/origin/dev"
HEAD_MOVES = ("reflog", "--format=%H %gs", DEFAULT_REVISION)
HEAD_REVISION = ("rev-parse", DEFAULT_REVISION)
DIFF_NAMES = ("diff", "--name-only", "-z")
DIFF_CONTENT = ("diff", "--no-ext-diff", "--unified=3")
TRACKED_FILES = ("ls-files", "-z")
COMMIT_SUBJECT = ("log", "-1", "--format=%s")
EXCLUDED_DOCS = {"AGENTS.md", "CLAUDE.md"}
READ_OPTIONS = {"errors": "replace"}
REDACT_OPTIONS = {"mode": "memory"}
TERM_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
TERM_LIMIT = 32
MATCH_LIMIT = 80
MATCH_LENGTH = 256
CHANGE_LIMIT = 24000
FILE_LIMIT = 80
SOURCE_LIMIT = 200
SOURCE_BYTES = 131072


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, **GIT_OPTIONS).strip()


def matches(root: Path, sources: list[str], text: str) -> list[dict]:
    terms = list(dict.fromkeys(word for word in TERM_PATTERN.findall(text) if word not in COMMON))[:TERM_LIMIT]
    if not terms:
        return []
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + r")\b", re.IGNORECASE)
    found = []
    for name in sources:
        for number, line in enumerate((root / name).read_text(**READ_OPTIONS).splitlines(), 1):
            if pattern.search(line):
                found.append({"path": name, "line": number, "text": redact(line, **REDACT_OPTIONS)[:MATCH_LENGTH]})
                if len(found) == MATCH_LIMIT:
                    return found
    return found


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return not subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=root, **GIT_OPTIONS).returncode


def _delivered_base(root: Path, base: str, head: str) -> str:
    try:
        integrated = git(root, "merge-base", INTEGRATION, head)
    except subprocess.CalledProcessError:
        return base
    if not _is_ancestor(root, base, integrated):
        return base if _is_ancestor(root, integrated, base) else integrated
    entries = (line.partition(" ") for line in git(root, *HEAD_MOVES).splitlines())
    authored = {commit for commit, _, subject in entries if subject.startswith("commit")}
    on_integration = git(root, "rev-list", f"{base}..{integrated}").split()
    return base if authored.intersection(on_integration) else integrated


def collect(root: Path, base: str, context: dict | None = None) -> dict:
    root = Path(root)
    context = context or {}
    try:
        head = git(root, *VERIFY_REVISION, context.get("branch") or DEFAULT_REVISION)
    except subprocess.CalledProcessError:
        head = git(root, *HEAD_REVISION)
    base = git(root, "merge-base", base, head) if base else head
    base = _delivered_base(root, base, head)
    names = git(root, *DIFF_NAMES, base, head).split("\0")
    files = [
        name for name in names if name and not sensitive_kind(name) and Path(name).suffix in CODE_TYPES | DOC_TYPES
    ]
    changes = git(root, *DIFF_CONTENT, base, head, "--", *files[:FILE_LIMIT]) if files else ""
    tracked = git(root, *TRACKED_FILES).split("\0")
    sources = sorted(
        name
        for name in tracked
        if Path(name).suffix in DOC_TYPES
        and not sensitive_kind(name)
        and Path(name).name not in EXCLUDED_DOCS
        and not (root / name).is_symlink()
        and (root / name).is_file()
        and (root / name).stat().st_size <= SOURCE_BYTES
    )[:SOURCE_LIMIT]
    content = "\n".join(
        line[1:] for line in changes.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    return {
        "head": head,
        "files": files,
        "changes": redact(changes, **REDACT_OPTIONS)[:CHANGE_LIMIT],
        "summary": redact(git(root, *COMMIT_SUBJECT, head), **REDACT_OPTIONS),
        "doc_sources": sources,
        "docs": matches(root, sources, content or context.get("description", context.get("title", ""))),
        "source_task": json.loads(redact(json.dumps(context), **REDACT_OPTIONS)),
    }
