---
title: Secret reads with kubectl get
parent: Reference
nav_order: 82
---

{% raw %}

# Secret reads with kubectl get

The credential guard refuses a `kubectl get` that may fetch a Secret whenever
any output or template value it gives can print a `data` or `stringData`
value. A Secret's values are only base64 encoded, so a value printed by
`-o yaml`, `-o jsonpath={.data.password}` or a go template is the credential
itself. It enters the transcript, and an exposed credential has to be rotated.

For a Secret, the default table, `-o name` and `-o wide` still pass, because
they print no value, and so does one go template that prints only the key
names. A get of a non Secret type, such as a pod, passes with any output.

The guard runs at `PreToolUse` on every install target (Claude Code, Codex and
Copilot CLI) and checks `Bash` commands. Related pages:
[Process environment reads](credential-guard-proc-environ.md) and
[Credential file names in commands](credential-guard-file-names.md).

## When a get may fetch a Secret

The guard reads the words after the `get` verb, the way the shell passes them.
The verb is the first `get` that no value flag takes as its value, so in
`kubectl -n get get secret db` the namespace `get` is skipped. A get may fetch
a Secret when its resource type operand does any of these:

| Type operand | Examples |
|---|---|
| Names the Secret type, plain or plural | `kubectl get secret db -o yaml`, `kubectl get secrets -o json` |
| Names it in any letter case, group qualified or as `TYPE/NAME` | `kubectl get Secret/db --output=json`, `kubectl get secrets.v1. db --output=yaml` |
| Names it in a resource list | `kubectl get configmap,secret -o yaml` |
| Leaves the type to the shell: a variable, a substitution, `$'…'` quoting or a glob | `kubectl get $R db -o yaml`, `kubectl get $(echo secret) db -o yaml`, `kubectl get $'secret' db -o yaml` |
| Is missing, so the type comes from a file, a kustomization or `xargs` | `kubectl get -f manifests/ -o json`, `echo secret db \| xargs kubectl get -o yaml` |

Flags may come before `get` or between the operands:
`kubectl -o yaml get secret db`, `kubectl get -o yaml secret db` and
`kubectl get -n prod secret db -o yaml` are all refused.

Only the operands that may hold the type are judged:

- When any operand has the `TYPE/NAME` form, every operand may hold the type:
  `kubectl get deploy/app $X -o yaml` is refused, because `$X` could be
  `secret/db`.
- Otherwise the type is the first operand that no flag before it could take as
  its value. A flag the guard knows to take a value, such as `-n`, `-l`, `-f`,
  `--context` or `--sort-by`, takes the next word. An unknown flag written
  without `=`, such as `-w` or `--show-labels`, might take one, so the word
  after it is judged as a possible type too: `kubectl get -w $R db -o yaml` is
  refused.
- A resource name after a literal type is never the type. `kubectl get pod
  $POD -o yaml`, `kubectl get pod secret -o yaml` and
  `kubectl get pods -n secret -o yaml` pass.

The type match is exact. `secretstores` is another type, so
`kubectl get secretstores -o yaml` passes.

## Which output values are refused

Every output and template value on the command is judged, in every spelling
the kubectl flag parser accepts:

| Spelling | Examples |
|---|---|
| Short flag, separate, attached or after `=` | `-o yaml`, `-oyaml`, `-o=json`, `-o 'yaml'`, `-o==yaml` |
| Short flag at the end of a cluster | `kubectl get secrets -Ao yaml` |
| Long flag, separate or after `=` | `--output yaml`, `--output=jsonpath={.data.password}` |
| Template flag, with or without an output flag | `--template='{{.data.password}}'`, `-o go-template --template '{{.data.password}}'` |

A value is refused unless it is empty, `name` or `wide`, or a go template that
lists key names only:

| Output value | Why it is refused |
|---|---|
| `yaml`, `json`, `kyaml` | prints the whole object, `data` included |
| `jsonpath=…`, `jsonpath-as-json=…`, `jsonpath-file=…` | any path can reach `.data`; the guard does not read the path |
| `custom-columns=…`, `custom-columns-file=…` | a column can select `.data.<key>` |
| `go-template=…`, `template=…` and `--template` values other than the key listing | the template can print a value |
| `go-template` or `template` with no template body | kubectl needs a template from somewhere the command does not show |
| `go-template-file=…`, `templatefile=…` | the template is in a file the guard does not read |
| a value left to the shell, such as `-o $FORMAT` | the shell can fill in any format |
| any other value | an unknown format is not trusted |

A `--template` with no output flag counts as a go template, because kubectl
renders it as one. Because every value is judged, a second output or template
flag cannot carry a value past a harmless one:
`kubectl get secret db -o name -o yaml` is refused, and so is a key listing
`-o go-template=…` followed by `--template='{{.data.password}}'`.

## Raw API paths

`kubectl get --raw` reads an API path and prints the response as is. It is
refused, whatever the output flags, when its value names a `secrets` path
segment, or holds a `$` the shell fills in:

```bash
kubectl get --raw /api/v1/namespaces/prod/secrets/db
kubectl get --raw=/api/v1/secrets
kubectl get --raw $P
```

`kubectl get --raw /healthz` passes.

## How the guard reads the command

- The command is lowercased first, so `KUBECTL`, `Secret` and `SECRETS` match.
- The `Bash` command is split at `&&`, `||`, `;` and newlines outside quotes,
  and each part is checked. A line continued with a trailing backslash is
  joined first, as the shell joins it, so `kubectl get secret db \` followed by
  `--output yaml` on the next line is refused.
- Every `kubectl` word starts its own scan: one after `sudo`, one called by an
  absolute path, one inside an `ssh` or `bash -c` argument and one run through
  `kubectl exec POD --` are all judged.
- Each scan runs to the first `|`, `<`, `>`, `;` or `&` outside quotes. A pipe
  does not hide the get, so `kubectl get secret db -o=json | jq .data` is
  refused, and a quoted jsonpath that holds a `|` stays one word.
- Parentheses and backticks count as word breaks, so a get inside
  `echo "$(kubectl get secret db -o=json)"` is judged.

## What still passes

```bash
kubectl get secret db
kubectl get secrets -n prod
kubectl get secrets -o name
kubectl get secrets -oname
kubectl get secrets -o wide
kubectl get secret db -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}'
kubectl get secret db --template='{{range $k,$v:=.data}}{{$k}} {{end}}'
kubectl get secret db -o template --template '{{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}'
kubectl get pods -o yaml
kubectl get pod $POD -o yaml
kubectl get pod/$POD -o yaml
kubectl get configmap app -o jsonpath={.data}
kubectl get pod secret-store-0 -o yaml
kubectl get pods -n secret -o yaml
kubectl get secretstores -o yaml
kubectl get --raw /healthz
kubectl get secrets -o name | grep -o yaml
```

The default table shows each Secret's type, how many keys it holds and its
age, but no value.

## Listing key names

The one template that passes for a Secret ranges over `.data` with a key and a
value variable and prints only the key:

```bash
kubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}'
```

The guard accepts the template only when all of these hold:

- It starts with `{{range $KEY, $VALUE := .data}}` and ends with `{{end}}`,
  with nothing before or after. Spaces inside the actions and the `{{-` and
  `-}}` trim markers are allowed.
- `$KEY` and `$VALUE` are two different names; `$_` works as the value name.
- Between them there is only plain text, `{{$KEY}}` and quoted string
  literals such as `{{"\n"}}`.

These templates are refused:

| Template | Why |
|---|---|
| `{{range $k,$v := .data}}{{$v}} {{end}}` | prints each value |
| `{{range $k,$v := .data}}{{$k}}={{$v}} {{end}}` | prints each value after its key |
| `{{range $k := .data}}{{$k}} {{end}}` | with one variable, range binds it to the value |
| `{{range $k,$k := .data}}{{$k}} {{end}}` | the same name for key and value holds the value |
| `{{.metadata.annotations}}` | the `kubectl.kubernetes.io/last-applied-configuration` annotation of an applied Secret holds its data values |
| `{{range $k,$v := .metadata.annotations}}{{$k}}{{end}}` | only a range over `.data` is the key listing |

## The refusal and the leak free alternatives

A refused call stops with exit code 2 and emits the
`agentihooks.guardrail.credential_read_blocked` telemetry event with the
session id and the tool name. The message is the same for every refused form;
its first line names `-o yaml/json` even when a jsonpath, a template or a raw
path was refused:

```text
BLOCKED: printing Secret data (kubectl get secret -o yaml/json).
A value read here enters the transcript, which is indexed and permanent — an exposed credential has to be rotated, not deleted.

A variable name that is not a valid identifier can hold a secret, and a value that spans several lines prints its later lines as if they were names, even through an identifier filter. Only two listings pass. Valid names and only a count of malformed ones:
  awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; print "malformed names: " n+0}'
A count (wc, or grep -c):  env | wc -l
To test one variable:  test -n "${VAR:-}" && echo set || echo unset
To use a value, reference the variable ("$VAR") so the shell expands it at execution instead of printing it.
kubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}' lists the key names without the values.
```

| Question | Command |
|---|---|
| Does the Secret exist? | `kubectl get secret db -o name` |
| What type is it, how many keys does it hold, how old is it? | `kubectl get secret db` |
| Which keys does it hold? | `kubectl get secret db --template='{{range $k,$v:=.data}}{{$k}} {{end}}'` |
| Which Secrets are in a namespace? | `kubectl get secrets -n prod -o name` |

The guard allows no form that prints a Secret value.
[Secret reads with kubectl describe](credential-guard-describe-secret.md)
explains the separate describe refusal: service account token exposure,
literal type requirements, command forms and the same key listing alternative.

## Configuration

`CREDENTIAL_GUARD_ENABLED` is the only switch, read from the environment or
`~/.agentihooks/.env`. It defaults to `true`. The hook keeps the guard on only
for `true`, `1` or `yes`, ignoring case; any other value turns the whole
credential guard off, this check included. The check has no setting of its
own:

```bash
# ~/.agentihooks/.env
CREDENTIAL_GUARD_ENABLED=true
```

Bypass mode (`disable controls`) does not lift the refusal; credential values
in the transcript are a hard floor.

## Limits

- Only a client named `kubectl` is read. `oc`, a shell alias such as `k` and a
  script run from a file are not.
- Heredoc bodies are masked before the check, so a `kubectl` line inside a
  heredoc fed to a shell is not read.
- The guard judges the words, not the cluster: it does not know whether a
  name is a Secret, so a type it cannot see is refused, and a literal non
  Secret type passes with any output.

## Troubleshooting

- **A get of a pod or deployment is refused.** The type operand is not
  literal, or an unknown flag sits before a variable: `kubectl get $KIND db -o
  yaml`, `kubectl get -w $R db -o yaml`. Write the type literally right after
  `get` and its listed flags, as in `kubectl get pod "$POD" -o yaml` or
  `kubectl get pod/$POD -o yaml`.
- **A get with `-f`, `-k` or under `xargs` is refused.** The command names no
  type, so it may fetch a Secret. Name the type and the resource on the
  command line instead.
- **A key listing template is refused.** Compare it with the rules under
  [Listing key names](#listing-key-names): two different variables, a range
  over `.data`, only `{{$KEY}}` and quoted strings inside, nothing outside
  `{{range …}}` and `{{end}}`. Copy the template from the refusal message.
- **A namespace or resource named `secret` does not make a get a Secret
  get.** Only the type operand is judged, so
  `kubectl get pods -n secret -o yaml` and `kubectl get pod secret -o yaml`
  pass. When such a get is refused, its type operand is not a literal non
  Secret type.
- **A Secret value was printed.** Check that `CREDENTIAL_GUARD_ENABLED` is
  unset or `true`, and that the command calls `kubectl` by that name outside a
  heredoc. If both hold, report the exact command so a test can cover it.

## Source

Delivered by pull request
[#11891](https://github.com/the-cloud-clockwork/agentihooks/pull/11891), which
closed [#11887](https://github.com/the-cloud-clockwork/agentihooks/issues/11887).
The earlier check matched the command text: it refused only `get` directly
before `secret` with `-o yaml` or `-o json`, so the long output flag, an equals
sign, a jsonpath and a go template printed Secret values. Pull request
[#12044](https://github.com/the-cloud-clockwork/agentihooks/pull/12044) then
narrowed the check to the type operand, so a pod name in a variable after a
literal type, such as `kubectl get pod $POD -o yaml`, passes again.

The check is `gets_secret_values` in `hooks.context.credential_guard`, called
from `renderer_verdict` in the guard's `Bash` decision. Tests, feature map
entries and the Semgrep rule that hold the behavior:

- Tests `test_kubectl_get_secret_forms_that_print_a_value_are_refused`,
  `test_kubectl_get_secret_forms_that_print_no_value_pass`,
  `test_kubectl_get_whose_type_operand_may_be_a_secret_is_refused` and
  `test_kubectl_get_of_a_named_non_secret_type_passes_with_any_output` in
  `tests/test_credential_guard.py`.
- Feature map entries `credential-guard-refuses-get-secret-values` and
  `credential-guard-get-reads-the-type-operand`; see
  [Feature map](feature-map.md).
- Semgrep incident rule `credential-guard-get-secret-output-regex`, which
  refuses a regex in `hooks/` that matches `get` before `secret` or `-o` before
  `yaml` or `json` in the command text.

{% endraw %}
