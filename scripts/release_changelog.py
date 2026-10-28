from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WORDS_MIN = 20
WORDS_MAX = 100
ATTEMPTS = 2
BATCH = 50
WORKERS = 8
BODY_CHARS = 4000
TIMEOUT_S = 120
DEFAULT_URL = "http://litellm.anton-prod.svc.cluster.local:4000"
DEFAULT_MODEL = "sonnet"
KEY_VAR = "CHANGELOG_LITELLM_KEY"
URL_VAR = "CHANGELOG_LITELLM_URL"
MODEL_VAR = "CHANGELOG_MODEL"
TITLE = "# Releases"
INTRO = (
    "One entry per change merged into dev, newest release first. Each entry names its date, merge commit, "
    "pull request and author, so a change can be found by date and hash."
)
SYSTEM = (
    "You explain one merged code change to the operator who owns the project. Write one plain paragraph of "
    f"{WORDS_MIN} to {WORDS_MAX} words saying what the change does and why it matters. No headings, lists, "
    "markdown, code blocks or preamble."
)
GRADED = re.compile(r"^Graded dev revision: ([0-9a-f]{40})$", re.MULTILINE)
NUMBER = re.compile(r"\(#(\d+)\)$")
STABLE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class WordCountError(ValueError):
    pass


class ModelError(RuntimeError):
    pass


@dataclass
class Change:
    sha: str
    number: int | None
    title: str
    date: str
    stat: str
    author: str = ""
    body: str = ""


def run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def git(*args: str) -> str:
    return run("git", *args)


def graded_revision(ref: str) -> str:
    match = GRADED.search(git("log", "-1", "--format=%B", ref))
    if not match:
        raise ValueError(f"{ref} records no graded dev revision")
    return match.group(1)


def previous_tag(tag: str, tags: list[str]) -> str:
    versions = sorted((tuple(map(int, m.groups())), t) for t in tags if (m := STABLE.fullmatch(t)))
    older = [t for version, t in versions if version < tuple(map(int, STABLE.fullmatch(tag).groups()))]
    return older[-1] if older else ""


def collect_changes(previous: str, current: str) -> list[Change]:
    span = f"{previous}..{current}" if previous else current
    changes = []
    for record in git("log", "--first-parent", "--format=%H%x1f%cs%x1f%s", span).splitlines():
        sha, date, subject = record.split("\x1f")
        match = NUMBER.search(subject)
        title = subject[: match.start()].rstrip() if match else subject
        stat = git("show", "--shortstat", "--format=", sha).strip()
        changes.append(Change(sha, int(match.group(1)) if match else None, title, date, stat))
    return changes


def gh_graphql(query: str) -> dict:
    return json.loads(run("gh", "api", "graphql", "-f", f"query={query}"))["data"]


def fetch_pull_requests(changes: list[Change], repository: str, graphql=gh_graphql) -> None:
    owner, name = repository.split("/")
    numbered = [change for change in changes if change.number is not None]
    for start in range(0, len(numbered), BATCH):
        batch = numbered[start : start + BATCH]
        fields = " ".join(f"p{c.number}: pullRequest(number: {c.number}) {{ author {{ login }} body }}" for c in batch)
        found = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')["repository"]
        for change in batch:
            pull = found.get(f"p{change.number}") or {}
            change.author = (pull.get("author") or {}).get("login", "ghost")
            change.body = pull.get("body") or ""


def word_count(text: str) -> int:
    return len(text.split())


def check_words(text: str) -> str:
    count = word_count(text)
    if not WORDS_MIN <= count <= WORDS_MAX:
        raise WordCountError(f"{count} words, need {WORDS_MIN} to {WORDS_MAX}")
    return " ".join(text.split())


def prompt(change: Change) -> list[dict]:
    user = (
        f"Title: {change.title}\nPull request: #{change.number}\nDiff stat: {change.stat}\n\n"
        f"Description:\n{change.body[:BODY_CHARS]}"
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def ask_model(change: Change, url: str, key: str, model: str, opener=urlopen) -> str:
    body = json.dumps({"model": model, "messages": prompt(change), "max_tokens": 400}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    try:
        with opener(
            Request(url.rstrip("/") + "/v1/chat/completions", data=body, headers=headers), timeout=TIMEOUT_S
        ) as response:
            return json.loads(response.read())["choices"][0]["message"]["content"]
    except HTTPError as error:
        raise ModelError(f"{model}: HTTP {error.code}") from None
    except (URLError, OSError) as error:
        raise ModelError(f"{model}: {type(error).__name__}") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError(f"{model}: unreadable response") from None


def write_entry(change: Change, ask) -> str:
    for attempt in range(ATTEMPTS):
        try:
            return check_words(ask(change))
        except (WordCountError, ModelError) as error:
            failure = error
            print(f"#{change.number} attempt {attempt + 1}: {error}", file=sys.stderr)
    raise failure


def render_release(tag: str, date: str, entries: list[tuple[Change, str]]) -> str:
    lines = [f"## {tag} · {date}", ""]
    for change, text in entries:
        pull = f"#{change.number}" if change.number is not None else "direct commit"
        lines += [
            f"### {change.date} · {change.sha[:12]} · {pull} · @{change.author or 'unknown'}",
            "",
            f"**{change.title}**",
            "",
            text,
            "",
        ]
    if not entries:
        lines += ["No merged changes.", ""]
    return "\n".join(lines)


def prepend_release(existing: str, section: str) -> str:
    heading = section.split("\n", 1)[0]
    if f"\n{heading}\n" in f"\n{existing}":
        return existing
    rest = existing.split("\n## ", 1)
    older = f"## {rest[1]}" if len(rest) == 2 else ""
    return "\n".join([TITLE, "", INTRO, "", section.rstrip("\n"), "", older]).rstrip("\n") + "\n"


def release_section(tag: str, repository: str, ask, graphql=gh_graphql) -> str:
    tags = git("tag", "--list", "v*").split()
    previous = previous_tag(tag, tags)
    current = graded_revision(tag)
    changes = collect_changes(graded_revision(previous) if previous else "", current)
    fetch_pull_requests(changes, repository, graphql)
    with ThreadPoolExecutor(WORKERS) as pool:
        texts = list(pool.map(lambda change: write_entry(change, ask), changes))
    return render_release(tag, git("log", "-1", "--format=%cs", tag).strip(), list(zip(changes, texts)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_changelog")
    parser.add_argument("tag")
    parser.add_argument("changelog", type=Path)
    parser.add_argument("section", type=Path)
    args = parser.parse_args(argv)
    url = os.environ.get(URL_VAR) or DEFAULT_URL
    model = os.environ.get(MODEL_VAR) or DEFAULT_MODEL
    key = os.environ.get(KEY_VAR, "")
    section = release_section(
        args.tag, os.environ["GITHUB_REPOSITORY"], lambda change: ask_model(change, url, key, model)
    )
    args.section.write_text(section)
    existing = args.changelog.read_text() if args.changelog.exists() else ""
    args.changelog.write_text(prepend_release(existing, section))
    return 0


if __name__ == "__main__":
    sys.exit(main())
