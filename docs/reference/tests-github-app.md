# Tests GitHub App token

The Tests workflow authenticates GitHub API calls as the org GitHub App `tcc-main-ci`, which has its own installation rate limit bucket instead of the shared workflow token's.

## Secrets

Organization Actions secrets, available to all repositories and sourced from the OpenBao record for the `tcc-main-ci` App:

| Secret | Use |
| --- | --- |
| `TCC_CI_CLIENT_ID` | `client-id` input of the mint action |
| `TCC_CI_APP_PRIVATE_KEY` | `private-key` input of the mint action |
| `TCC_CI_APP_ID` | Numeric App id, kept for tools that need it |
| `TCC_CI_INSTALLATION_ID` | Installation id, kept for tools that need it |

## Workflow contract

- Mint with the official [create GitHub App token action](https://github.com/actions/create-github-app-token/tree/v3.2.0), pinned to `v3.2.0`, scoped to the current repository with `permission-actions`, `permission-contents` and `permission-pull-requests` set to `read`.
- Mint in every job that calls the API: the action revokes its token when its job ends, and secret job outputs cannot feed parallel jobs.
- Every API step reads `GH_TOKEN` from the minted token. No step falls back to the workflow token.
- The Sonar job proves the token on every run: `GET /installation/repositories` answers only for an installation token, and `GET /rate_limit` reports the App installation bucket.

`tests/test_ci_app_token.py` holds this contract.
