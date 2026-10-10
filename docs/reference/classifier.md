---
title: Decision classifier
parent: Reference
nav_order: 8
---

# Decision classifier

`hooks.classifier` asks small decision models typed questions about a state and gets
calibrated probabilities back. Hooks and scripts call one function, `decide()`; any
part of agentihooks that needs a cheap yes/no, a pick from options or a score uses it
with its own `purpose`.

The models are OpenRouter decision models served by LiteLLM on `POST /v1/decisions`:
`liquid-d1` and `jev-1.13` (32k each) and `pplx-decider-v1-27b` (262k context). They
return no text, only probabilities from one forward pass. `pplx-decider-v1-27b` comes
last because OpenRouter lists it with no serving provider and answers it 404; it stays
in the order for inputs too large for the 32k models.

## Calling it

```python
from hooks.classifier import Choice, Score, YesNo, decide

result = decide(
    state={"task": title, "kind": kind, "territory": paths},
    questions={
        "tier": Choice("Which model tier fits this task?", {"small": "...", "medium": "...", "large": "..."}),
        "effort": Score("How much reasoning does it need?", ["low", "medium", "high", "max"]),
        "trivial": YesNo("Is this a one line change?", true="...", false="..."),
    },
    purpose="model-pick",
    harness="claude",
)
result.answers["tier"].choice, result.answers["tier"].probabilities, result.answers["tier"].confidence
result.answers["effort"].score, result.answers["effort"].legend
result.answers["trivial"].noul      # probability of yes
result.source                       # the model that answered
result.calibrated, result.latency_ms, result.cost
```

| Type | Criteria | Answer fields |
|---|---|---|
| `YesNo` (`noul`) | `true` and `false` descriptions | `noul`: probability of yes |
| `Choice` | `{option: description}`, 1 to 255 options | `choice`, `confidence`, `probabilities` |
| `Score` | `[level0, level1, ...]`, 1 to 10 levels | `score` (weighted level index), `confidence`, `legend`, `probabilities` |

Question names match `^[a-z][a-z0-9_]{0,63}$`, and a call asks 1 to 128 questions.
A violation raises `ClassifierInputError` before any network call.

## Failover

Models are tried in the order of `AGENTIHOOKS_CLASSIFIER_MODELS`. A model whose
context is smaller than the estimated input (4 characters per token) is skipped.

| Response | What happens |
|---|---|
| 429, 5xx, other 4xx, timeout, connection error, a non-JSON body, a missing answer | next model |
| 400 whose message names context or tokens | next model |
| any other 400 | `ClassifierRequestError`: the caller sent a bad request; no fallback |
| 401 or 403 | every API model is skipped (they share one key) |

When every configured API model fails, or the key is refused, a marker holds the API down for
`AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S` seconds, so hook callers pay no timeout on every
tool call. A large input that fails on the only model wide enough for it leaves the
API up for the next call. The marker retains the failed models and their reasons.

The default fallback uses Haiku for Claude and Luna for Codex. `harness` selects
it explicitly; otherwise `AGENTIHOOKS_TARGET` selects it, with Claude the default
for scripts. If a CLI fails, the other is tried. With neither answering,
`decide()` raises `ClassifierUnavailable`, and the caller keeps its own default.
An explicit `fallbacks` sequence replaces the built-in backends, including an empty
sequence to disable CLI calls.

Both CLIs receive a JSON schema requiring a probability for every question option
or score level. Distributions are validated and normalized; choice, confidence
and weighted score are derived from them. CLI results have `calibrated=False`.
Claude runs Haiku without tools or session persistence. Codex runs Luna at low
effort in read-only mode without the shared daemon, so its hooks inherit the child
environment. Its model slug is read from the Codex model catalog, defaulting to
`gpt-6-luna`. Both receive `AGENTIHOOKS_CLASSIFIER_CHILD=1`; the hook manager returns
before reading stdin, dispatching handlers or registering the child session.
If a routed Claude shell retains only its account token, the Haiku child receives
that same token as native OAuth. An existing native OAuth token takes precedence;
the fallback never selects another account or changes provider settings.

A fallback is any object with a `name` and a `decide(DecisionRequest) -> DecisionResult`
method (the `Backend` protocol) that raises `BackendFailure` when it cannot answer.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENTIHOOKS_CLASSIFIER_URL` | none | LiteLLM base address; `/v1/decisions` is appended. Unset means the API is not tried |
| `AGENTIHOOKS_CLASSIFIER_LITELLM_KEY` | none | The LiteLLM key, read at call time and sent only in the Authorization header. Unset means the API is not tried |
| `AGENTIHOOKS_CLASSIFIER_MODELS` | `liquid-d1,jev-1.13,pplx-decider-v1-27b` | Model order |
| `AGENTIHOOKS_CLASSIFIER_TIMEOUT_S` | `5` | Timeout per API call |
| `AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S` | `120` | How long a failed API stays marked down |
| `AGENTIHOOKS_CLASSIFIER_FALLBACK_TIMEOUT_S` | `60` | Timeout per CLI fallback |
| `AGENTIHOOKS_CLASSIFIER_LUNA_MODEL` | model catalog, then `gpt-6-luna` | Override the Codex fallback model |

The key never appears in a log line, an exception text or the decision log. It comes
from the shell (loaded from OpenBao), never from a bundle file.

## Decision log and stats

Every call appends one JSON line to `$AGENTIHOOKS_HOME/classifier/decisions.jsonl`:
`ts`, `purpose`, `source` (null when nothing answered), `calibrated`, `latency_ms`,
`cost`, `answers`, a 16 character `state_digest`, `failures` and `api_down_cached`.
Each failure holds `model` and `reason`: HTTP status, timeout, connection error,
parse error, missing CLI or CLI exit status. Raw child output is never logged.
When `api_down_cached` is true, `failures` includes the API failures retained by
the down marker, followed by any failed CLI calls. Caller request errors are logged
before they are raised. The down marker sits next to the log as `api-down`.

```bash
agentihooks classifier stats [--purpose model-pick]
```

prints the call count, unavailable count, calls per source, the fallback rate (share
of answered calls not answered by a calibrated API model), latency p50, p90 and p99,
and the total cost.

## Command line

```bash
agentihooks classify --state state.json --questions questions.json [--purpose P] [--harness claude|codex]
```

`--state` holds JSON, or plain text sent as a string. `--questions` holds the wire
form, one object per name:

```json
{
  "trivial": {"type": "noul", "instructions": "Is this a one line change?", "criteria": {"true": "one line", "false": "more"}},
  "tier": {"type": "choice", "instructions": "Which tier?", "criteria": {"small": "trivial edits", "large": "architecture"}},
  "effort": {"type": "score", "instructions": "How much reasoning?", "criteria": ["low", "medium", "high", "max"]}
}
```

It prints the result JSON and exits 0; 2 for an input or request error; 1 when no
backend answered.

## Corpus and eval

A corpus file `<name>.corpus.yaml` sits next to the definition file the loader selects
(package, bundle or `$AGENTIHOOKS_HOME/classifiers`). Each case holds the state, the
definition parameters, the expected verdict per question, a control flag and recorded
answer samples in the decisions wire shape:

```yaml
version: 1
cases:
  - name: one_line_typo
    state: {task: "Fix the spelling of 'recieve' in the README heading"}
    params: {instructions: "Is this task a change to a single line of text?", "true": "One line", "false": "More"}
    expected: {verdict: true}
    control: false
    samples:
      - source: liquid-d1
        latency_ms: 557
        answers:
          verdict: {type: noul, noul: 0.98}
```

A yes rule expects `true` or `false`, a choice rule an option key, and a score rule a
`[min, max]` range. A choice or score rule with a threshold may expect `null`, meaning
the answer falls below its confidence floor. A control case may expect only rejections:
`false` or `null`. Definitions with a code rule have no corpus.

```bash
agentihooks classifier eval NAME             # replay the recorded samples
agentihooks classifier eval NAME --live N    # ask every API model, haiku and luna N times per case
```

Replay calls no backend and runs in the CI tests. `--live` runs only on demand and is
refused whenever `CI` is set. Both print wrong cases, held controls (controls whose every
sample was rejected), and samples and latency per backend. They exit 1 on any wrong
sample or when no backend answered, and 2 for a malformed definition or corpus. With
`AGENTIHOOKS_METRICS_URL` and `AGENTIHOOKS_METRICS_USER` set, each sample is a row in
`swarm.classifier_evals`.

## Auto swarm lanes

When an eng or ci lane's effort is `auto`, the spawn runtime asks the classifier
how much reasoning the task needs, following harness routing, using its title,
description, kind and territory size. The answer only raises effort above the
launch default (`high` unless `AGENTIHOOKS_CLAUDE_EFFORT` or
`AGENTIHOOKS_CODEX_EFFORT` names another): Claude launches high or max, Codex high
or xhigh. The question offers two levels, the launch default and the harness's top
effort, because every answer at or below the default launches the default; a lower
default such as `low` therefore launches low or the top, never a level between. A
default at the top launches without asking, and a launch default outside those levels
is kept without asking. The
classifier never picks a model: a lane model of `auto` launches the harness
default, opus for Claude and gpt-6.1-sol for Codex unless
`AGENTIHOOKS_CLAUDE_MODEL` or `AGENTIHOOKS_CODEX_MODEL` names another. Explicit
model and effort values remain unchanged, and master and plan seats never consult
the classifier. A master always launches on the frontier model at high effort for
its harness, whatever its lane or the environment names, and records `frontier`.
Each decision it asks for uses purpose `model-pick` in the classifier log.

The `model-pick` definition's `confidence` threshold defaults to 0.6;
`AGENTIHOOKS_CLASSIFIER_MODEL_PICK_CONFIDENCE` overrides it, and the older
`AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE` still applies when that is unset. The
effort answer's confidence must meet it; otherwise the lane keeps its launch defaults. An
unavailable classifier also preserves those defaults.
Agent records, swarm status and the page carry `model_source` and
`model_confidence`; low confidence retains the attempted classifier's metadata,
and an explicit or unavailable pick records `lane-default` without confidence.
A running session that reports another model or effort replaces both on the
record with source `session` and no confidence.
