import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.request import urlopen

from scripts import release_changelog as rc

WORKERS = 8
DAYS = 30
STABLE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def stable_tags(tags: list[str]) -> list[str]:
    return sorted((t for t in tags if STABLE.fullmatch(t)), key=lambda t: tuple(map(int, STABLE.fullmatch(t).groups())))


def dev_trees(ref: str) -> dict[str, str]:
    trees = {}
    for line in rc.run("git", "log", "--first-parent", "--format=%T %H", ref).split("\n"):
        if line.strip():
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


def window_releases(tags: list[str], trees: dict[str, str], since: str) -> list[tuple[str, list[rc.Change]]]:
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


def write_sections(releases, ask, graphql, repository: str, workers: int = WORKERS) -> list[str]:
    changes = [c for _, kept in releases for c in kept]
    rc.fetch_pull_requests(changes, repository, graphql)
    with ThreadPoolExecutor(workers) as pool:
        summaries = dict(zip((c.sha for c in changes), pool.map(lambda c: rc.write_entry(c, ask), changes)))
    return [
        rc.render_release(tag, commit_field(tag, "%cs"), [(c, summaries[c.sha]) for c in kept])
        for tag, kept in releases
    ]


def split_releases(text: str) -> list[str]:
    return ["## " + part.strip("\n") + "\n" for part in re.split(r"^## ", text, flags=re.M)[1:]]


def merge_changelog(existing: str, sections: list[str]) -> str:
    releases = {s.split(" ", 2)[1]: s for s in sections}
    releases.update({s.split(" ", 2)[1]: s for s in split_releases(existing)})
    text = ""
    for tag in stable_tags(list(releases)):
        text = rc.prepend_release(text, releases[tag])
    return text


def merged_count(repository: str, since: str, until: str) -> int:
    query = f"repo:{repository} is:pr is:merged base:dev merged:{since}..{until}"
    return int(rc.run("gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}", "--jq", ".total_count"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.release_changelog_seed")
    parser.add_argument("changelog", type=Path)
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--dev", default="origin/dev")
    args = parser.parse_args(argv)
    repository = os.environ["GITHUB_REPOSITORY"]
    since = (date.today() - timedelta(days=args.days)).isoformat()
    tags = rc.run("git", "tag", "--list", "v*").split()
    trees = dev_trees(args.dev)
    releases = window_releases(tags, trees, since)
    if not releases:
        print(f"no release since {since}", file=sys.stderr)
        return 1
    url = os.environ.get(rc.URL_VAR) or rc.DEFAULT_URL
    model = os.environ.get(rc.MODEL_VAR) or rc.DEFAULT_MODEL
    key = os.environ[rc.KEY_VAR]
    sections = write_sections(releases, lambda c: rc.ask_model(c, url, key, model, urlopen), rc.gh_graphql, repository)
    existing = args.changelog.read_text() if args.changelog.exists() else ""
    args.changelog.write_text(merge_changelog(existing, sections))
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
