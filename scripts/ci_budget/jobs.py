import argparse
import json
import re
import subprocess
import sys
import time

from scripts.ci_budget.rate_limit import gh_api

PAGE_SIZE = 10
ATTEMPTS = 4
TIMEOUT = 10
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
SERVER_MIN = 500
SERVER_END = 600
TIMEOUT_MESSAGES = ("timeout", "timed out")


def read_page(endpoint: str, offset: int, size: int) -> tuple[list[dict], int]:
    for attempt in range(ATTEMPTS):
        page = offset // size + 1
        try:
            result = gh_api([f"{endpoint}?per_page={size}&page={page}"], timeout=TIMEOUT, text=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if isinstance(error, subprocess.CalledProcessError):
                status = HTTP_STATUS.search(error.stderr)
                retryable = (
                    SERVER_MIN <= int(status[1]) < SERVER_END
                    if status
                    else any(message in error.stderr.lower() for message in TIMEOUT_MESSAGES)
                )
                if not retryable:
                    raise
            if attempt == ATTEMPTS - 1:
                raise
            size = max(1, size // 2)
            time.sleep(attempt + 1)
        else:
            return json.loads(result.stdout)["jobs"], size


def read_jobs(endpoint: str, size: int = PAGE_SIZE) -> list[dict]:
    jobs = []
    while True:
        rows, size = read_page(endpoint, len(jobs), size)
        jobs.extend(rows[len(jobs) % size :])
        if len(rows) < size:
            return jobs


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint")
    parser.add_argument("--page-size", type=int, choices=range(1, 101), default=PAGE_SIZE)
    args = parser.parse_args()
    try:
        for job in read_jobs(args.endpoint, args.page_size):
            print(json.dumps(job))
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr)
