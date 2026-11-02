### Fixed

- The fleet red notice step no longer waits about thirteen seconds on every tick pass. The open pull request listing asked GitHub for one hundred pull requests with their check rollups per page and GitHub answered with a gateway timeout after eleven seconds, so the step paid the wait and held nothing. The listing now pages twenty five pull requests at a time, still up to two hundred, and the step keeps each repository's listing, a failed one too, in Redis for ten minutes.
