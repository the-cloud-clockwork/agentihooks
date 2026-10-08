# Tests GitHub App setup

The master and operator provision a dedicated App for Tests. Store its credentials in OpenBao and publish them as organization Actions secrets `TESTS_APP_ID` and `TESTS_APP_PRIVATE_KEY`, available to the public agentihooks repository. No credential values belong in issues, logs, code or chat.

## Permissions

| Repository permission | Access | Consumer |
| --- | --- | --- |
| Actions | Read only | Workflow runs, jobs, artifact lists and artifact downloads |
| Contents | Read only | Checkout, commits and tested tree lookup |
| Pull requests | Read only | Resolve the queued pull request branch |
| Metadata | Read only | Automatically granted by GitHub |

Grant no organization or account permissions. The current Tests gates make no repository API writes: uploads and caches use the Actions runtime service, and GitHub reports job checks itself. These do not require App write permissions. Keep branch protection and merge privileges outside this App.

## Creation and installation

1. An organization owner or GitHub App manager opens [organization settings](https://github.com/organizations/The-Cloud-Clockwork/settings/apps), then Developer settings, GitHub Apps, New GitHub App.
2. Choose a unique App name for Tests. Set the homepage to the agentihooks repository. Leave user authorization, device flow, callback and setup URLs unused. Disable webhook Active; the App only authenticates CI.
3. Select the repository permissions above. Choose installation only on this account, then Create GitHub App.
4. In the App settings, record the numeric App ID. Generate a private key under Credentials, Key pairs, New key (older UI: Private keys, Generate a private key). Transfer the downloaded PEM directly to the approved OpenBao secret record, preserving newlines. Do not paste it into an agent session.
5. Select Install App, install on The-Cloud-Clockwork, choose Only select repositories and select agentihooks. Confirm the read permissions.
6. Through the existing OpenBao to GitHub organization secret provisioning process, publish the App ID as `TESTS_APP_ID` and the complete PEM as `TESTS_APP_PRIVATE_KEY`. Set selected repository access to agentihooks; private repositories only would exclude this public repository. The OpenBao record and its GitHub sync mapping are operator owned; their current names are not established here.
7. Confirm only the secret names and access policy. Report provisioning complete to the master without returning either value.

These UI instructions follow [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) and [private key management](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps).

## Workflow contract and acceptance

Use the official [create GitHub App token action](https://github.com/actions/create-github-app-token/tree/v3.2.0), pinned to release tag `v3.2.0`. Its `app-id` input accepts the requested numeric App ID; this release marks that input deprecated in favor of `client-id`, but still supports it. Its `private-key` input reads the org secret. Scope each token to the current repository with explicit read permission inputs. Mint separately in consuming jobs so parallel jobs do not depend on a token job or exchange secret outputs. Keep the default job-end token revocation; tokens expire after one hour.

Route explicit GitHub API calls, REST artifact downloads and repository API checkout fallbacks through the installation token. Keep artifact upload and cache service credentials owned by Actions. Never add a workflow token fallback or change to privileged execution of pull request code to obtain secrets. Fork and Dependabot secret availability require separate trust handling; acceptance must use an authorized same-repository pull request.

On that pull request, capture the mint action result, successful `GET /installation/repositories` scoped to agentihooks, and `GET /rate_limit` resource limits, remaining quota and reset. Compare with a workflow-token rate-limit response in the same job as a diagnostic only; consumers continue using the App token. The installation-only endpoint establishes token identity even when quota limits happen to match. [GitHub documents installation rate limits separately](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Do not claim acceptance until the API consumers pass, the required checks and both reviews close, and the pull request merges.
