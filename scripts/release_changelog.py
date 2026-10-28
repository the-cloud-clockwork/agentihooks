from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WORDS_MIN = 20
WORDS_MAX = 100
ATTEMPTS = 2
BATCH = 50
BODY_CHARS = 4000
TIMEOUT_S = 120
DEFAULT_URL = "https://llm.homeofanton.com"
DEFAULT_MODEL = "sonnet"
KEY_VAR = "CHANGELOG_LITELLM_KEY"
ACCESS_VARS = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
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
FIELD = "\x1f"
LOG_FORMAT = "--format=%H%x1f%cs%x1f%an%x1f%s"
MARKUP = re.compile(r"^#|<(?=/?[A-Za-z!])|\]\(|```")
CONTENT_TYPE = "Content-Type"
AUTHORIZATION = "Authorization"
JSON = "application/json"


class EntryError(ValueError):
    pass


class WordCountError(EntryError):
    pass


class MarkupError(EntryError):
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


def run(*command: str, check: bool = True) -> str:
    return subprocess.run(command, check=check, capture_output=True, text=True).stdout


def git(*args: str) -> str:
    return run("git", *args)


def graded_revision(ref: str) -> str:
    match = GRADED.search(git("cat-file", "commit", f"{ref}^{{commit}}"))
    if not match:
        raise ValueError(f"{ref} records no graded dev revision")
    return match.group(1)


def version(tag: str) -> tuple[int, ...]:
    match = STABLE.fullmatch(tag)
    if not match:
        raise ValueError(f"{tag} is not a stable release tag")
    return tuple(map(int, match.groups()))


def previous_tag(tag: str, tags: list[str]) -> str:
    older = sorted((version(t), t) for t in tags if STABLE.fullmatch(t) and version(t) < version(tag))
    return older[-1][1] if older else ""


def collect_changes(previous: str, current: str) -> list[Change]:
    span = f"{previous}..{current}" if previous else current
    changes = []
    for record in git("log", "--first-parent", LOG_FORMAT, span).splitlines():
        sha, date, author, subject = record.split(FIELD)
        match = NUMBER.search(subject)
        title = subject[: match.start()].rstrip() if match else subject
        stat = git("show", "--shortstat", "--format=", sha).strip()
        changes.append(Change(sha, int(match.group(1)) if match else None, title, date, stat, author))
    return changes


def gh_graphql(query: str) -> dict:
    answer = json.loads(run("gh", "api", "graphql", "-f", f"query={query}", check=False))
    if any(error.get("type") != "NOT_FOUND" for error in answer.get("errors", [])):
        raise RuntimeError(f"GraphQL errors: {answer['errors']}")
    return answer["data"]


def fetch_pull_requests(changes: list[Change], repository: str, graphql: Callable[[str], dict] = gh_graphql) -> None:
    owner, name = repository.split("/")
    numbered = [change for change in changes if change.number is not None]
    for start in range(0, len(numbered), BATCH):
        batch = numbered[start : start + BATCH]
        fields = " ".join(
            f"p{c.number}: issueOrPullRequest(number: {c.number}) {{ ... on PullRequest {{ author {{ login }} body }} }}"
            for c in batch
        )
        found = graphql(f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}')["repository"]
        for change in batch:
            pull = found.get(f"p{change.number}") or {}
            if "author" in pull:
                change.author = (pull["author"] or {}).get("login", "ghost")
            change.body = pull.get("body") or ""


def word_count(text: str) -> int:
    return len(text.split())


def check_words(text: str) -> str:
    count = word_count(text)
    if not WORDS_MIN <= count <= WORDS_MAX:
        raise WordCountError(f"{count} words, need {WORDS_MIN} to {WORDS_MAX}")
    flat = " ".join(text.split())
    if MARKUP.search(flat):
        raise MarkupError("markup in the explanation")
    return flat


def prompt(change: Change) -> list[dict]:
    user = (
        f"Title: {change.title}\nPull request: #{change.number}\nDiff stat: {change.stat}\n\n"
        f"Description:\n{change.body[:BODY_CHARS]}"
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def gateway_headers(env: dict[str, str]) -> dict[str, str]:
    return {AUTHORIZATION: f"Bearer {env[KEY_VAR]}"} | {var.title().replace("_", "-"): env[var] for var in ACCESS_VARS}


def ask_model(change: Change, url: str, access: dict[str, str], model: str, opener: Callable = urlopen) -> str:
    body = json.dumps({"model": model, "messages": prompt(change), "max_tokens": 400}).encode()
    headers = {CONTENT_TYPE: JSON, **access}
    try:
        with opener(
            Request(url.removesuffix("/") + "/v1/chat/completions", data=body, headers=headers), timeout=TIMEOUT_S
        ) as response:
            content = json.loads(response.read())["choices"][0]["message"]["content"]
    except HTTPError as error:
        raise ModelError(f"{model}: HTTP {error.code}") from None
    except (URLError, OSError) as error:
        raise ModelError(f"{model}: {type(error).__name__}") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError(f"{model}: unreadable response") from None
    if not isinstance(content, str):
        raise ModelError(f"{model}: unreadable response")
    return content


def write_entry(change: Change, ask: Callable[[Change], str]) -> str:
    failures: list[Exception] = []
    for attempt in range(ATTEMPTS):
        try:
            return check_words(ask(change))
        except (EntryError, ModelError) as error:
            failures.append(error)
            print(f"#{change.number} attempt {attempt + 1}: {error}", file=sys.stderr)
    raise failures[-1]


def render_release(tag: str, date: str, entries: list[tuple[Change, str]]) -> str:
    lines = [f"## {tag} · {date}", ""]
    for change, text in entries:
        pull = f"#{change.number}" if change.number is not None else "direct commit"
        lines += [
            f"### {change.date} · {change.sha[:12]} · {pull} · @{change.author}",
            "",
            f"**{change.title}**",
            "",
            text,
            "",
        ]
    if not entries:
        lines += ["No merged changes.", ""]
    return "\n".join(lines)


def insert_release(existing: str, section: str) -> str:
    releases = [f"## {part}" for part in existing.split("\n## ")[1:]]
    if any(release.split()[1] == section.split()[1] for release in releases):
        return existing
    ordered = sorted([*releases, section], key=lambda release: version(release.split()[1]), reverse=True)
    return f"{TITLE}\n\n{INTRO}\n\n" + "\n".join(ordered)


def release_section(
    tag: str, repository: str, ask: Callable[[Change], str], graphql: Callable[[str], dict] = gh_graphql
) -> str:
    version(tag)
    tags = git("tag").split()
    previous = previous_tag(tag, tags)
    current = graded_revision(tag)
    changes = collect_changes(graded_revision(previous) if previous else "", current)
    fetch_pull_requests(changes, repository, graphql)
    texts = [write_entry(change, ask) for change in changes]
    return render_release(tag, git("log", "-1", "--format=%cs", tag).strip(), list(zip(changes, texts)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    explain = commands.add_parser("explain")
    explain.add_argument("tag")
    explain.add_argument("section", type=Path)
    insert = commands.add_parser("insert")
    insert.add_argument("section", type=Path)
    insert.add_argument("changelog", type=Path)
    args = parser.parse_args(argv)
    if args.command == "insert":
        args.changelog.write_text(insert_release(args.changelog.read_text(), args.section.read_text()))
        return 0
    url = os.environ.get(URL_VAR) or DEFAULT_URL
    model = os.environ.get(MODEL_VAR) or DEFAULT_MODEL
    access = gateway_headers(dict(os.environ))
    section = release_section(
        args.tag,
        os.environ["GITHUB_REPOSITORY"],
        lambda change: ask_model(change, url, access, model, urlopen),
        gh_graphql,
    )
    args.section.write_text(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
