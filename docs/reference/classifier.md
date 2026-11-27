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
| `AGENTIHOOKS_CLASSIFIER_TIMEOUT_S` | `5` | Timeout per API call for an input up to 35,000 estimated tokens; a larger input gets one second per 7,000 tokens, capped at 30 seconds unless this value is higher |
| `AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S` | `120` | How long a failed API stays marked down |
| `AGENTIHOOKS_CLASSIFIER_FALLBACK_TIMEOUT_S` | `60` | Timeout per CLI fallback |
| `AGENTIHOOKS_CLASSIFIER_LUNA_MODEL` | model catalog, then `gpt-6-luna` | Override the Codex fallback model |

The key never appears in a log line, an exception text or the decision log. It comes
from the shell (loaded from OpenBao), never from a bundle file.

The swarm tick and the controller run outside an agent session, so at start they add
each `AGENTIHOOKS_CLASSIFIER_*` name they lack from the installed Claude settings env,
then, while the address or the key is still missing, from the operator's login shell,
as the hooks of an agent session see them.

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

When an eng or ci lane's effort is `auto`, the spawn runtime picks the model and
effort from a band that code builds from the task's size (`S`, `M` or `L`; unsized
counts as `M`) and rank. The Claude ladder is Sonnet at xhigh, Opus at medium, Opus at
high and Opus at xhigh. Size `S` offers Sonnet xhigh or Opus medium, `M` Opus medium
or Opus high, `L` Opus high or Opus xhigh. Rank `urgent` and `high` move the band up
one step, `low` moves it down one, `normal` leaves it, and the band stays inside the
ladder. Codex uses the same effort column on gpt-6.1-sol with the Sonnet step removed,
so a Codex band can hold one option, which launches without asking.

The classifier answers one choice question, `lower` or `higher`, carrying the task's
title, description, kind, territory size, size and rank, with a plain description of
each band option. Code maps the answer to model and effort. The band floor is its
first Opus or Sol option, so Sonnet launches only on a confident answer, and only at
xhigh: the Sonnet step is offered only when the swarm effort range reaches xhigh and the
lane model is `auto`. A lane with an explicit model keeps that model, never takes the
Sonnet step and takes the picked effort; otherwise the Opus or Sol steps use the
harness default model, which `AGENTIHOOKS_CLAUDE_MODEL` or `AGENTIHOOKS_CODEX_MODEL`
can name.
Explicit lane efforts never consult the classifier, planners launch the medium
default, and a master always launches on the frontier model at xhigh effort for its
harness, whatever its lane or the environment names, and records `frontier`.
Each decision it asks for uses purpose `model-pick` in the classifier log.

The `model-pick` definition's `confidence` threshold defaults to 0.6;
`AGENTIHOOKS_CLASSIFIER_MODEL_PICK_CONFIDENCE` overrides it, and the older
`AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE` still applies when that is unset. The
answer's confidence must meet it; otherwise the launch takes the band floor. An
unavailable classifier also launches the band floor, recorded as source `band`.
Agent records, swarm status and the page carry `model_source` and
`model_confidence`; low confidence retains the attempted classifier's metadata,
an explicit lane records `lane-default` and an unavailable classifier `band`, both without confidence.
A running session that reports another model or effort replaces both on the
record with source `session` and no confidence.

### Lane models on the other harness

An eng or ci lane's `model` is matched to the harness of the agent being launched
before its effort is read. A Codex agent on a lane fixed to a Claude model, one whose
name starts with `claude`, `opus`, `sonnet`, `haiku` or `fable`, launches on the
swarm's Codex model instead, and the lane's effort is kept. A lane with `agent` set to
`auto` can therefore fix `opus` for its Claude agents and still launch Codex agents:

```bash
agentihooks swarm <id> set eng-agent=auto eng-model=opus eng-effort=xhigh
agentihooks swarm <id> set codex-model=gpt-6.1-sol
```

| Harness | Lane model | Lane effort | Launch |
|---|---|---|---|
| codex | `opus` | `xhigh` | the swarm's Codex model at `xhigh`, with no classifier call |
| codex | `opus` | `auto` | the Codex band on the swarm's Codex model, with no Sonnet step |
| claude | `opus` | `xhigh` | `opus` at `xhigh` |
| codex | `gpt-6.1-sol` | `xhigh` | `gpt-6.1-sol` at `xhigh` |

The swarm's Codex model is its `codex-model` setting, `gpt-6.1-sol` unless `swarm set`
names another model from the Codex catalog. A caller of the pick that passes no swarm
model gets the Codex launch default: `AGENTIHOOKS_CODEX_MODEL`, else `gpt-6.1-sol`.
After the pick the spawn runtime makes the same check in both directions: a Codex
model (`gpt`, `codex`, `o1`, `o3` or `o4`) on a Claude agent, or a Claude model on a
Codex agent, is replaced with the swarm's model for that harness, including a model
saved in a handoff. A launch never asks one harness for the other harness's model.

Before this check, the pick returned an explicit lane effort together with the lane's
model before it looked at the harness. Every Codex engineer on the eng lane, fixed to
`opus` at `xhigh`, asked Codex for `opus`, failed with model not found before its
first tool call and never reported its profile canary, while ci lane agents on `auto`
validated ([#11477](https://github.com/the-cloud-clockwork/agentihooks/issues/11477)).
Three guards hold the fix: the parametrized
`test_lane_model_matches_harness_before_explicit_effort_returns` and
`test_a_claude_lane_model_with_no_configured_codex_model_takes_the_codex_launch_model`
in `tests/swarm/test_model_pick.py`, the `feature-map.json` entry
`lane-model-harness-compatibility`, and the Semgrep incident rule
`lane-model-before-harness`, which fails a `ModelPick` built from the lane in
`scripts/swarm/model_pick.py` that returns on an explicit effort with no Codex harness
or `foreign` model check replacing its model before it.

Troubleshooting:

- **A Codex agent exits with model not found before its canary.** Read the agent's
  model in `agentihooks swarm <id> status`; until the session reports its own, the row
  shows the launch model. A Claude model there means the host that spawned it runs a
  release without this check, so roll that host onto the current release. The rendered
  Codex `config.toml` can name the right model while the launch asks for another, so
  it does not prove which model the agent requested.
- **`swarm set codex-model=M` is refused** with `codex_model must be a model listed in
  the codex catalog`: name a model the Codex catalog lists. A lane model is refused
  only when no harness catalog lists it (`eng-model must be auto or a model listed in a
  harness catalog, not <model>`), so a Claude model on a lane that also launches
  Codex agents is accepted.
