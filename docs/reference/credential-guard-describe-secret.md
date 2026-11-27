---
title: Secret reads with kubectl describe
parent: Reference
nav_order: 83
---

{% raw %}

# Secret reads with kubectl describe

The credential guard refuses a `kubectl describe` that names Secrets or does
not show a literal resource type. For a `kubernetes.io/service-account-token`
Secret, describe prints the full `token` value. The byte counts shown for
other Secret types do not make describe a leak free way to inspect Secrets.

The guard reads the command, not the cluster. It refuses describe of every
Secret, including one previously observed to have type `Opaque`. There is
no exemption based on an earlier type check. To inspect a Secret, use the
default get table, `-o name` or the key listing below.

This check runs at `PreToolUse` on `Bash` commands for Claude Code, Codex and
Copilot CLI. [Secret reads with kubectl get](credential-guard-get-secret.md)
covers output formats and the accepted key listing templates.

## Inspect a Secret without printing its values

Replace `db` and `prod` with the Secret name and namespace:

```bash
kubectl -n prod get secret db
kubectl -n prod get secret db -o name
kubectl -n prod get secret db --template='{{range $k,$v:=.data}}{{$k}} {{end}}'
```

The default table shows the name, type, number of data keys and age. The
second command prints the resource name. The third lists only the data key
names; `$v` is bound by the template but never printed. For a service account
token Secret, the listing can include `ca.crt`, `namespace` and `token`
without printing their values.

Use the Go template from the refusal message. kubectl JSONPath has no
key-name operator: `{.data}`, `{range .data}{@}{end}` and `{.data.*}` print
values. The guard refuses JSONPath output for Secrets. See
[Listing key names](credential-guard-get-secret.md#listing-key-names) for
accepted template variants and their restrictions.

## Which describe calls are refused

| Form | Examples |
|---|---|
| A plain, plural, group qualified or `TYPE/NAME` Secret type | `kubectl describe secret db`, `kubectl describe secrets`, `kubectl describe Secret/db`, `kubectl describe secrets.v1. db` |
| A resource list or a mixed list that includes Secrets | `kubectl describe configmaps,secrets`, `kubectl describe pod/web secret/db` |
| A type supplied by a variable, command substitution or glob | `kubectl describe $RES db`, `kubectl describe $(echo secret) db`, `kubectl describe $RES/*` |
| No type operand, including file or kustomize input | `kubectl describe -f manifests/`, `kubectl describe --filename=deploy.yaml`, `kubectl describe -k overlays/prod`, `kubectl describe --kustomize overlays/prod` |
| A type supplied later by `xargs` or a loop | `kubectl get secrets -o name \| xargs kubectl describe`, `for s in $(kubectl get secrets -o name); do kubectl describe "$s"; done` |
| A mixed `TYPE/NAME` list with another operand left to the shell | `kubectl describe pod/web $NAME` |

Namespace and context flags do not exempt a Secret describe:
`kubectl -n prod describe secret db`, `kubectl describe -n prod secret db`
and `kubectl describe --context prod secret db` are all refused.

The check lowercases the command and removes quotes, escapes and parentheses
from the words after describe. It catches `"sec"ret` and `sec\ret` as well
as `secret`, joins backslash continuations and checks describe calls after
a pipe, through `sudo`, inside an `ssh` argument or in a command substitution.
Piping the output to `head` does not make the call pass.

### Literal types and flags

A known value flag written without `=` takes the following word as its
value. This includes `-n`, `--namespace`, `-l`, `--selector`, `--context`,
`--kubeconfig`, `-f` and `-k`. Boolean flags, including `--show-events`,
`--recursive`, `-A`, `-R` and clustered `-AR`, leave the next word as an
operand. An unknown flag also leaves the next word as an operand. Therefore
`kubectl describe --show-events $RES db` is refused, while
`kubectl describe --show-events pod "$POD"` passes this check.

When any operand contains `/`, the guard checks the part before `/` of
every operand for a hidden type. Otherwise it checks the first operand.
This allows a variable resource name after a literal type, while refusing
`pod/web $NAME`, because `$NAME` might expand to `secret/db`.

## What still passes

```bash
kubectl describe pod secret-store-0
kubectl describe externalsecret db
kubectl describe sa default
kubectl describe pod "$POD"
kubectl describe pod/$NAME
kubectl describe -n "$NS" pod web
kubectl describe --selector "$SEL" pods
kubectl describe --context "$CTX" pod web
kubectl describe --show-events pod "$POD"
kubectl describe -AR pods
kubectl -n secret describe pod web
```

These commands name literal resource types other than Secrets. Passing this
check does not certify that their output contains no credentials; other
guardrails and the credential rule still apply.

## Refusal and configuration

The refused tool call stops before execution with exit code 2. The hook emits
`agentihooks.guardrail.credential_read_blocked` with the session id and tool
name. The first line explains the describe refusal:

```text
BLOCKED: describing a Secret or a resource the command does not name (kubectl describe prints the token of a service account token Secret).
```

After the shared credential guidance, both this refusal and a refused Secret
get offer the same alternative:

```text
kubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}' lists the key names without the values.
```

`CREDENTIAL_GUARD_ENABLED` defaults to `true` and controls the whole
credential guard. It is read from the environment or the AgentiHooks dotenv
configuration. Keep it enabled:

```bash
# ~/.agentihooks/.env
CREDENTIAL_GUARD_ENABLED=true
```

Case-insensitive `true`, `1` and `yes` enable the guard; any other value
disables it. There is no describe-specific setting or per-Secret exception.
Session bypass mode (`disable controls`) does not lift this refusal.

The bundle's credential rule answers the Secret key question with the same
Go template. The hook enforces the refusal independently of the prose rule.

## Limits and troubleshooting

- **A pod or namespace named `secret` is refused.** Any word after describe
  that exactly names a Secret type counts, including names and flag values.
  So `kubectl describe pod secret` and `kubectl describe -n secret pod web`
  are refused. Put the namespace before the verb as in
  `kubectl -n secret describe pod web`. For a pod named `secret`, use
  `kubectl get pod secret`.
- **A describe with a variable type, `xargs`, `-f` or `-k` is refused.**
  Write the known non-Secret type literally: `kubectl describe pod "$POD"`.
  For a Secret, use the get table or key listing.
- **A Secret has already been checked as `Opaque`.** A separate type read
  does not change this check. Use the key listing instead of describe.
- **A command passed but printed a Secret value.** Check that the guard is
  enabled and record the exact command without its output. The check matches
  a client named `kubectl`; aliases such as `k`, other clients and commands
  hidden in script files are outside this parser. Heredoc bodies are masked
  before the guard scans the command. Follow the credential exposure rule if
  a value entered the transcript.

The parser judges command text without executing it or resolving shell
aliases. It may refuse a harmless command whose type is not visible, and
`kubectl describe --help` is also refused because it has no type operand.

## Source and regression coverage

Delivered by pull request
[#11875](https://github.com/the-cloud-clockwork/agentihooks/pull/11875) and
the credential rule update in
[agentihooks-bundle#436](https://github.com/the-cloud-clockwork/agentihooks-bundle/pull/436).
Pull request [#11917](https://github.com/the-cloud-clockwork/agentihooks/pull/11917)
subsequently made only known value flags consume a following word, so boolean
flags cannot hide a shell-supplied type.

The describe check is `describes_secrets`, called by `renderer_verdict` in
`hooks.context.credential_guard`. Regression tests cover refused describe
forms, passing non-Secret commands and the key listing in get refusals:
`test_kubectl_describe_of_a_secret_is_refused`,
`test_kubectl_commands_that_name_no_secret_type_pass` and
`test_get_secret_refusal_offers_the_key_listing_and_not_describe`.

Feature map entry `credential-guard-refuses-describe-secret` records the
behavior. Semgrep incident rule `credential-guard-offers-describe-secret`
refuses describe as a suggested alternative in guard refusal messages and
constants, with red and clean fixtures. See [Feature map](feature-map.md).

{% endraw %}
