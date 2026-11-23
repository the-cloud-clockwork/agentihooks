---
title: Reviewer bundle overlay
parent: Reference
nav_order: 98
permalink: /docs/reference/reviewer-bundle-overlay/
---

# Reviewer bundle overlay

The fleet's `reviewer` profile adds its review rules and platform connections
to the package reviewer role. It was delivered in
[bundle pull request #504](https://github.com/the-cloud-clockwork/agentihooks-bundle/pull/504).
Use it when a reviewer needs the same testing policy, feature map, manifestos
and service access as the engineers whose changes it reviews.

The package role supplies the `swarm-reviewer` skill and
[read only guards](read-only-roles.md). It reads a GitHub pull request or
GitLab merge request, its whole diff, feature map and affected callers and
tests. It reviews independently of red, green or pending checks, posts
evidence to the change request and sends findings to the author. It requires
at least one finding; a weakest point that breaks no rule must say so plainly.
The author makes the fix. The reviewer never edits code, commits or merges.

## Profile configuration

The bundle keeps a thin `profiles/reviewer/profile.yml`. This example replaces
the deployment's private base profile name with `<fleet-base>`; substitute the
base already used by the bundle's other role profiles:

```yaml
name: reviewer
extends: ["<fleet-base>", package:reviewer]
description: "Swarm reviewer: independent strict review of every pull request, merge group and release"
allowedOverlays:
  - swarm-brain
```

The order combines the fleet persona and operations rules, the package role's
skill and guards, and the bundle reviewer doctrine. `package:reviewer` selects
the installed package role explicitly. An unprefixed self reference would
create an inheritance cycle. Keep the role skill and conditions in the package;
the bundle adds fleet policy and connections rather than copies of those assets.

The bundle's `profiles/reviewer/CLAUDE.md` points to the Development and core CI
manifestos. Both manifestos include `reviewer` in their `roles` lists. The CI
Guardrails manifesto targets CI and master roles and is not selected for the
reviewer by this change. Role filtering determines which manifestos render;
receiving a fleet base does not mean every role receives every manifesto.

## What the fleet reviewer checks

| Area | Review requirement |
|---|---|
| Findings | Cite the documented rule that is broken or quote the specification line the change misses. Back the finding with code or observed output. |
| Tests | Run tests and mutation only in CI and the lab. A local run requires the user's explicit `AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true` opt in. |
| Size and code style | Treat a changed unit beyond the Development manifesto's size limits, a prohibited comment or a missing signature annotation as a finding. |
| Feature map | Read `feature-map.json` first. Report touched behavior without a test and mapped tests that were deleted, skipped or lost their `@pytest.mark.feature` marker. |
| Recurring defects | An upheld finding about a recurring pattern closes with a lint rule, a Semgrep rule with red and clean fixtures, or a test in the same pull request. Semgrep rules belong in `.semgrep-incidents` or `.semgrep/features`. |
| Platform changes | Require code and CI to carry the change to running systems using the fleet's floating image tags. Read service evidence through the configured tools. |

The delivered Development manifesto sets these limits for new or changed
units. Existing allowlisted offenders may not grow:

| Measure | Limit |
|---|---|
| Function or method | 300 lines |
| File | 3,000 lines |
| Parameters | 7 |
| Cyclomatic complexity | 12 |
| Branches | 12 |

Comments are prohibited; docstrings and one file header of at most 20 lines
are allowed. Tool directives may remain without prose. Annotate every
parameter except `self` and `cls`, including `*args` and `**kwargs`, and the
return type. The injected manifestos remain the authority if the fleet changes
these rules. See [Feature map](feature-map.md),
[Semgrep feature tests](semgrep-features.md) and
[Local test guard](local-test-guard.md) for the underlying contracts.

Gate evidence follows the [delivery tiers](delivery-tiers.md):

| Tier | Evidence the reviewer reads |
|---|---|
| Pull request | Checks on the current head: lint, format, size, static analysis, changed line ratchets and the touched tests. For gates named by `delivery.yaml` under `lab.authoritative`, read the lab check verdict. |
| Queue | The unit suite on the merge group. A green pull request head does not prove the combined group. |
| Promotion | Slow checks on the integration branch before release promotion. A red promotion holds the release while integration can keep accepting changes. |

A check result supports the review; it does not replace reading the code or
finding what the delivery breaks. See [CI lab](ci-lab.md) for verdicts and reuse.

## MCP connections

The reviewer adds `serena` and `gateway-tools` to the fleet base connections.
Serena reaches the worktree router. The gateway selects the `engineer` toolset
for reading GitHub, SonarQube, LiteLLM and Grafana. Review permission does not
authorize live changes to those systems.

This is the shape of the bundle's `profiles/reviewer/.claude/.mcp.json`, with
documentation addresses in place of private routing values. Replace the URL
and Host with the deployment's configured gateway values:

```json
{
  "mcpServers": {
    "serena": {
      "type": "http",
      "url": "http://127.0.0.1:8643/mcp"
    },
    "gateway-tools": {
      "type": "http",
      "url": "https://gateway.example.com/toolset/engineer/mcp",
      "headers": {
        "Host": "gateway.example.com",
        "Authorization": "Bearer ${MCP_KEY_GATEWAY}"
      },
      "default_tools_approval_mode": "approve"
    }
  }
}
```

Provide `MCP_KEY_GATEWAY` in the launch environment through the fleet's secret
configuration. Keep the reference in the bundle; never put its value in JSON
or a review. `default_tools_approval_mode: approve` lets Codex use the declared
gateway server when the rendered session cannot ask for tool approval. It does
not remove the reviewer's write guards.

## Optional swarm brain

`allowedOverlays` permits `swarm-brain`; the overlay also lists `reviewer` in
its `wears` list. Both declarations allow selecting it for this role. A new
reviewer launch can request it with `--overlay swarm-brain`.

For a swarm bound launch, the overlay connects `agentibrain` to the shared swarm
brain, with `X-API-Key` referencing `${SWARM_BRAIN_MCP_KEY}`. The swarm brain
replaces the ordinary brain connection rather than adding a second brain.
Outside a swarm the normal kernel connection remains. Serena, gateway tools,
the reviewer skill and inherited doctrine remain present in either case.
Selecting the overlay alone does not create a swarm or give the session a
review task.

## Render and launch

Use an installed package version that includes `package:reviewer` and a bundle
checkout that includes the delivered overlay. The source bundle workflow used
`v2.17.1127`; that is delivery evidence, not a required permanent version pin.

Render into an owned scratch folder to inspect both targets without changing
the live installation. Replace the example bundle directory with its checkout:

```bash
REVIEW_BUNDLE_DIR="$HOME/dev/my-fleet-bundle"
REVIEW_SCRATCH_DIR="$(agentihooks scratch new)"

agentihooks profile render reviewer \
  --target claude \
  --overlay swarm-brain \
  --out "$REVIEW_SCRATCH_DIR/claude" \
  --bundle "$REVIEW_BUNDLE_DIR"

agentihooks profile render reviewer \
  --target codex \
  --overlay swarm-brain \
  --out "$REVIEW_SCRATCH_DIR/codex" \
  --bundle "$REVIEW_BUNDLE_DIR"
```

`scratch new` builds the task directory name from the session. `--out` selects
an isolated home; `--bundle` selects the bundle for that render.
Omit `--overlay swarm-brain` to inspect the base reviewer. Inspect the rendered
instructions for the package role, fleet reviewer doctrine and selected
manifestos, and the target's MCP configuration for Serena and gateway tools.
A scratch render without a swarm binding does not prove shared brain access.

For an individual review, prepare a brief that names the repository, change
request and author, then launch with the linked bundle's reviewer profile:

```bash
REVIEW_REPO_DIR="$HOME/dev/my-project"
REVIEW_BRIEF="$REVIEW_SCRATCH_DIR/review-brief.md"
cat > "$REVIEW_BRIEF" <<'EOF'
Review https://github.com/example/project/pull/123.
Read the body, whole diff, feature map, affected callers and tests.
Post evidence for each finding and send the review link to the author.
Change no code and run no local tests.
EOF

agentihooks init-agent \
  --dir "$REVIEW_REPO_DIR" \
  --agent codex \
  --profile reviewer \
  --prompt-file "$REVIEW_BRIEF"
```

Use `--agent claude` for Claude Code. For a launch that already has a swarm
binding, add `--overlay swarm-brain`. The prepared home is recorded for the
launch and the binding canary validates the mounted profile. For reviewer
tasks, seats and template configuration, use the [Review lane](swarm-review-lane.md)
reference. The bundle overlay supplies context and connections; it does not
add forge event triggers, create tasks or reserve reviewer capacity.

## Verification and troubleshooting

The delivered bundle's role, manifesto, profile budget and gateway tests cover
the thin reviewer overlay. Its swarm brain tests render the reviewer for both
Claude and Codex, check the bound brain and authentication references, and
preserve the ordinary brain outside a swarm. The package feature
`reviewer-role-renders-read-only-for-claude-and-codex` covers the role and guards.
[The source CI run](https://github.com/the-cloud-clockwork/agentihooks-bundle/actions/runs/44906187513)
passed the bundle test and type jobs. Rendering proves configuration assembly;
service connectivity still needs evidence from the launched target.

| Symptom | Check or action |
|---|---|
| `package:reviewer` cannot resolve | Use a package release containing the reviewer role and a bundle checkout containing its overlay. |
| The rendered instructions lack fleet review rules | Confirm the selected profile is the bundle's `reviewer`, the base and `package:reviewer` are in its chain, and the manifestos include `reviewer` in their role lists. |
| `swarm-brain` is rejected | Check both reviewer `allowedOverlays` and the overlay's `wears` list. |
| Shared brain is absent after a scratch render | A scratch render without swarm identity retains the ordinary brain. Check the binding of the actual swarm launch before judging its connection. |
| Gateway or brain calls are refused | Check the selected toolset, routing and presence of the referenced environment variables without printing values. Preserve the configured approval mode for Codex. |
| An edit, commit or local test is refused | Send the finding to the author and read CI or lab evidence. The reviewer guards and local test policy apply with or without the brain overlay. |
| A configured reviewer never launches | Follow the review lane's placement and trigger limits. Rendering a profile does not prove a reviewer has been scheduled. |
