# Example values

`examples/` holds a values file for each way to install the chart, each rendered and checked by the chart proof:

| File | Installs |
|---|---|
| `hub.yaml` | the ledger, Redis and controller with the event hub on the bundled NATS |
| `hive-only.yaml` | one worker hive that joins a swarm installed elsewhere, without NATS |
| `external-nats.yaml` | the hub on a NATS outside the release |

# Bundled NATS chart

The pinned NATS chart archive is committed under `charts/` and checked against `deploy/helm/agentihooks-swarm-deps.sha256`, so releases and proofs never fetch the upstream chart index. To move the pin, update the version in `Chart.yaml`, then from the repository root:

```bash
git rm deploy/helm/agentihooks-swarm/charts/nats-*.tgz
helm pull nats --repo https://nats-io.github.io/k8s/helm/charts/ --version <version> --destination deploy/helm/agentihooks-swarm/charts
sha256sum deploy/helm/agentihooks-swarm/charts/nats-<version>.tgz
```

Write the digest file only after the printed digest equals the `digest` of that version in the upstream `index.yaml`:

```bash
sha256sum deploy/helm/agentihooks-swarm/charts/nats-<version>.tgz > deploy/helm/agentihooks-swarm-deps.sha256
git add deploy/helm/agentihooks-swarm/charts deploy/helm/agentihooks-swarm-deps.sha256
```

# Worker GitHub authentication

Choose `workers.githubAuth.mode` and an existing Secret. GitHub credentials are selected by key reference; never put their values in Helm values.

For a GitHub token with access to the configured repositories and private bundle:

```yaml
workers:
  githubAuth:
    mode: token
    secretName: hive-github
    tokenKey: token
```

For a GitHub App installed on those repositories, with Contents, Pull requests and Workflows write permissions:

```yaml
workers:
  githubAuth:
    mode: app
    secretName: hive-github
    appIdKey: app-id
    installationIdKey: installation-id
    privateKeyKey: private-key
```

The three App keys contain the App id, installation id and PEM private key. An ExternalSecret can select them from an existing secret manager record. The chart selects only the keys for the chosen mode. Existing installations using `workers.envFromSecrets` can leave `secretName` empty and provide the corresponding standard environment names: `GITHUB_TOKEN`, or `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY`.

The shared Python API `scripts.github_app.GitHubAppAuth().get_token()` mints installation credentials and caches them in memory until five minutes before expiry. Its optional repository argument retains the worker's single repository Contents write restriction. Unscoped hive tokens inherit the App installation's grants.

`agentihooks github-app token` prints a current token for command substitution. `agentihooks github-app exec COMMAND ...` exports `GH_TOKEN`, `GITHUB_TOKEN` and git credential helper settings into that process tree. `agentihooks github-app credential get` implements Git's credential protocol for GitHub HTTPS only; store and erase persist nothing. These commands default to App mode unless `HIVE_GITHUB_AUTH_MODE=token` explicitly selects the supplied token.

Hive startup and native agent launch mint App credentials before starting their child processes. Git credential requests and the worker image's `gh` wrapper request current credentials again, so agents running beyond an hour continue to authenticate. Minted credentials stay in process memory and environment; generated launchers and git configuration hold only helper commands. An App failure refuses authentication and never selects token mode automatically.
