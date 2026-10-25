# Canonical project identity

SV2-IDN-01 adds `ProjectIdentity.project_id` to `resolve_project` and `attributes()`.
The previous `project`, `repo`, `cwd`, `worktree` and `remote` fields remain readable.
Paths and basenames describe local provenance. They grant no authority and are not global IDs.

`canonical_remote` reads no network service. The resolver observes the checkout's Git
`origin` configuration. HTTPS, SSH URLs with optional `git` user, and `git@host:owner/repo`
forms resolve to `host/owner/repo`. Host case is normalized; GitHub owner and repository
case is normalized too. Other forges retain owner and repository case. A trailing slash
and `.git` suffix are removed. Linked worktrees use the same Git common directory as
their primary checkout. Separate clones may use different local names.

Credential-bearing remotes and ambiguous forms resolve to `unknown` with an empty
exported `remote`. Paths, query strings, fragments, ports, nested forge groups, usernames
other than `git`, and hosts without a dot are unsupported. Diagnostics never include
the rejected remote. Existing Git configuration is read without rewriting it.

For non Git projects, the registration authority supplies `AGENTIHOOKS_PROJECT_ID=local:<id>`.
An ID starts with an alphanumeric character, then has at most 127 alphanumeric, dot,
underscore or dash characters. Registration survives a path change when the authority
supplies the same ID. Missing registration stays `unknown`; an unowned folder without
registration still returns `None`. Registration cannot override an observed Git project.
In a swarm, registration applies to the configured repository, not the incidental cwd.

Repository renames use a persisted explicit map provided by the caller:

```json
{"schema_version":"2.0","aliases":{"github.com/first/renamed":"github.com/first/common"}}
```

Pass that document as `resolve_project(cwd, env, aliases=document)`. The renamed remote
keeps the historical canonical ID; the legacy `remote` still describes the observed
remote. The resolver validates the entire map, rejects cycles and noncanonical keys,
follows chains, and never edits the map. The map's maintainer owns which repositories
are explicitly equated; no rename is inferred and no alias grants access.

The identity and map schema is `urn:swarm-v2:project`, version 2.0. Its wider forge
support is metadata only; transport contracts currently accept GitHub, local and unknown
IDs. Other forge IDs must wait for an explicit transport contract update before admission.

`project_identity_ambiguities_total` on each identity is one for `unknown`, otherwise
zero. The caller aggregates these per resolution with its own fixture or execution
dimensions. No global counter or network telemetry is created by the resolver.

Rollback retains the five preceding fields and reads historical records without
`project_id`. Reverting the change or omitting an alias map does not delete stored
evidence, registered IDs, or alias documents. The recovery test replays a persisted
map and projects the result onto those five fields to rehearse this compatibility path.
