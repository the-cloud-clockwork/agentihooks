import argparse
import subprocess
from collections.abc import Iterator

from scripts.swarm import merge_queue, queue_record
from scripts.swarm.store import SwarmError

OPEN = """query($owner: String!, $name: String!, $after: String) { repository(owner: $owner, name: $name) {
  pullRequests(states: OPEN, baseRefName: "dev", first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
    pageInfo { hasNextPage endCursor }
    nodes {
      number url isDraft createdAt mergeQueueEntry { id }
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    }
  }
} }"""


def green(pull: dict) -> bool:
    return any(
        (node["commit"]["statusCheckRollup"] or {}).get("state") == "SUCCESS" for node in pull["commits"]["nodes"]
    )


def candidates(repo: str, run) -> list[dict]:
    owner, name = repo.split("/")
    found, after = [], None
    while True:
        variables = {"owner": owner, "name": name} | ({"after": after} if after else {})
        page = merge_queue.graphql(OPEN, variables, run)["repository"]["pullRequests"]
        found += [
            pull for pull in page["nodes"] if not pull["isDraft"] and pull["mergeQueueEntry"] is None and green(pull)
        ]
        if not page["pageInfo"]["hasNextPage"]:
            return sorted(found, key=lambda pull: pull["createdAt"])
        after = page["pageInfo"]["endCursor"]


def sweep(repo: str, run=subprocess.run, journal=queue_record.Journal) -> Iterator[dict]:
    for pull in candidates(repo, run):
        try:
            result = merge_queue.operate("queue", pull["url"], run, journal=journal(pull["url"], run))
            outcome = "waiting" if result.get("waiting") else "queued" if result["queued"] else result["state"].lower()
        except SwarmError as exc:
            outcome = f"refused: {exc}"
        yield {"number": pull["number"], "created": pull["createdAt"], "outcome": outcome, "url": pull["url"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue every green open pull request into dev, oldest first")
    parser.add_argument("--repo", default="The-Cloud-Clockwork/agentihooks")
    count = 0
    for row in sweep(parser.parse_args(argv).repo):
        print(f"{row['number']} {row['created']} {row['outcome']} {row['url']}", flush=True)
        count += 1
    print(f"{count} pull requests swept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
