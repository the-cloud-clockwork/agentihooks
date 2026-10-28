import argparse
import os
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.request import urlopen

from scripts import release_changelog as rc

WORKERS = 8
DAYS = 30
Release = tuple[str, list[rc.Change]]


def stable_tags(tags: list[str]) -> list[str]:
    return sorted((t for t in tags if rc.STABLE.fullmatch(t)), key=rc.version)


def dev_trees() -> dict[str, str]:
    trees = {}
    for line in rc.git("log", "--first-parent", "--format=%T %H").splitlines():
        tree, sha = line.split()
        trees.setdefault(tree, sha)
    return trees


def commit_field(ref: str, fmt: str) -> str:
    return rc.run("git", "log", "-1", f"--format={fmt}", ref).strip()


def release_revision(tag: str, trees: dict[str, str]) -> str:
    try:
        return rc.graded_revision(tag)
    except ValueError:
        tree = rc.run("git", "rev-parse", f"{tag}^{{tree}}").strip()
        if tree not in trees:
            raise ValueError(f"{tag} matches no dev revision") from None
        return trees[tree]


def window_releases(tags: list[str], trees: dict[str, str], since: str) -> list[Release]:
    ordered = stable_tags(tags)
    releases = []
    for index in range(len(ordered) - 1, -1, -1):
        tag = ordered[index]
        if commit_field(tag, "%cs") < since:
            break
        previous = release_revision(ordered[index - 1], trees) if index else ""
        changes = rc.collect_changes(previous, release_revision(tag, trees))
        releases.append((tag, [c for c in changes if c.date >= since]))
    return releases


def written_tags(changelog: str) -> set[str]:
    return set(re.findall(r"^## (\S+)", changelog, flags=re.M))


def write_sections(
    releases: list[Release],
    ask: Callable[[rc.Change], str],
    graphql: Callable[[str], dict],
    repository: str,
    workers: int = WORKERS,
) -> list[str]:
    changes = [c for _, kept in releases for c in kept]
    rc.fetch_pull_requests(changes, repository, graphql)
    with ThreadPoolExecutor(workers) as pool:
        summaries = dict(zip((c.sha for c in changes), pool.map(lambda c: rc.write_entry(c, ask), changes)))
    return [
        rc.render_release(tag, commit_field(tag, "%cs"), [(c, summaries[c.sha]) for c in kept])
        for tag, kept in releases
    ]


def merge_changelog(existing: str, sections: list[str]) -> str:
    for section in sections:
        existing = rc.insert_release(existing, section)
    return existing


def merged_count(repository: str, since: str, until: str) -> int:
    query = f"repo:{repository} is:pr is:merged base:dev merged:{since}..{until}"
    return int(rc.run("gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}", "--jq", ".total_count"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("changelog", type=Path)
    parser.add_argument("sections", type=Path)
    parser.add_argument("--days", type=int, default=DAYS)
    args = parser.parse_args(argv)
    repository = os.environ["GITHUB_REPOSITORY"]
    since = (date.today() - timedelta(days=args.days)).isoformat()
    trees = dev_trees()
    releases = window_releases(rc.git("tag").split(), trees, since)
    if not releases:
        print(f"no release since {since}", file=sys.stderr)
        return 1
    url = os.environ.get(rc.URL_VAR) or rc.DEFAULT_URL
    model = os.environ.get(rc.MODEL_VAR) or rc.DEFAULT_MODEL
    access = rc.gateway_headers(dict(os.environ))
    existing = args.changelog.read_text()
    written = written_tags(existing)
    sections = write_sections(
        [release for release in releases if release[0] not in written],
        lambda c: rc.ask_model(c, url, access, model, urlopen),
        rc.gh_graphql,
        repository,
    )
    args.changelog.write_text(merge_changelog(existing, sections))
    for section in sections:
        (args.sections / f"{section.split()[1]}.md").write_text(section)
    changes = [c for _, kept in releases for c in kept]
    pulls = sum(c.number is not None for c in changes)
    until = commit_field(release_revision(releases[0][0], trees), "%cI")
    merged = merged_count(repository, since, until)
    print(f"window: {since} to {until}, releases {releases[-1][0]} to {releases[0][0]}")
    print(f"entries: {len(changes)} (pull requests {pulls}, direct commits {len(changes) - pulls})")
    print(f"merged pull requests into dev in window: {merged}")
    return 0 if pulls == merged else 1


if __name__ == "__main__":
    sys.exit(main())
