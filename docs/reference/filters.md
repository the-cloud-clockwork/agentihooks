---
title: Filters
parent: Reference
nav_order: 32
---

# Filters
{: .no_toc }

A filter catches text nobody asked for before it lands. A deterministic finder
flags spans of the text, the [decision classifier](classifier.md) judges each
finding against the filter's intent, and the filter sends the text back, strips
the spans or flags them. A filter is a [condition](../hooks/conditions.md) file
written in YAML: it uses the same layers, filename grammar, operator gate and
fail open behaviour, so when a filter runs is decided by its filename.

1. TOC
{:toc}

---

## Worked example: the capacity line

The swarm capacity reason in `scripts/swarm/capacity.py` carries this tail:

```python
reason += f"; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
```

The ledger page label already names the state; the tail explains how it was
computed. The agentihooks repository carries a project layer filter that sends
such an edit back:

`.agentihooks/conditions/pre-edit+write+multiedit-ledger_ui.filter.yaml`

```yaml
paths:
  - 'scripts/swarm/capacity.py'
  - '**/scripts/swarm/capacity.py'
  - 'scripts/swarm_ledger/**'
  - '**/scripts/swarm_ledger/**'
finders:
  - script: string_literals
  - script: explanation_tail
mode: both
action: send-back
intent: >-
  User-facing text states the value or state its label names. It never explains
  why, how it was computed, or adds detail nobody asked for.
```

What happens on an Edit whose `new_string` appends that line:

1. The filename makes it run before every Edit, Write and MultiEdit; `paths`
   narrows it to the ledger UI text producers.
2. `string_literals` returns the f-string as a finding; `explanation_tail`
   returns the span from `; Claude has` to the end of the literal, reason
   `explanation tail`.
3. The classifier is asked one yes or no question per finding, carrying the
   intent, the span, its reason and the enclosing lines.
4. A yes on the tail denies the Edit. The agent sees:

   ```
   filter sent the text back:
   - "; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats": explanation tail
   ```

An Edit that writes only `changed 9m ago` yields no explanation tail finding and
passes. With the classifier down both edits pass.

The package layer ships the same intent and finders for front end files as
`pre-edit+write+multiedit-ui_slop.filter.yaml`, with `paths` covering `*.html`,
`*.htm`, `*.jsx`, `*.tsx`, `*.vue`, `*.svelte` and `.js` or `.ts` files under
`components`, `pages` and `ui` folders.

## Filename grammar

```
<step>-<matcher>-<name>.filter.yaml
```

The `step`, `matcher` and `name` parts follow the
[condition filename grammar](../hooks/conditions.md#filename-grammar). A filter
cannot be `.async`: it runs in the hook process and its answer is needed before
the tool runs. Examples:

| File | Runs |
|---|---|
| `pre-edit+write+multiedit-ui_slop.filter.yaml` | before every Edit, Write and MultiEdit |
| `pre-ledger_write-plain_words.filter.yaml` | before every ledger comment, follow up or question write |
| `pre-inbox_send-no_ids.filter.yaml` | before every `agentihooks msg send` |
| `pre-judge-bias.filter.yaml` | on every judge case |

A filter lives in any condition layer: the package, the linked bundle, a profile,
the runtime folder or a repository's `.agentihooks/conditions/`.

## Schema

Every key is optional. An unknown key or an invalid value fails the filter as a
condition error: the call is allowed and `agentihooks conditions list` reports
the file with the reason.

| Key | Default | Meaning |
|---|---|---|
| `paths` | every path | globs matched against the tool's `file_path`, `notebook_path` or `path`, and against its file name; with `paths` set, a call without a path is skipped |
| `fields` | every field | limit the text to these input keys: `text`, `evidence`, `case`, `content`, `new_string`, `new_source`, `edits` |
| `finders` | none | a list of `regex:` entries (with an optional `reason:`) and `script:` entries naming a finder |
| `mode` | `both` | `finders`: findings stand without the classifier; `classifier`: each whole text unit is one finding, no finders; `both`: finders flag, the classifier confirms |
| `intent` | empty | what the text must be, in plain words; every classifier question carries it |
| `intent_from` | none | a dotted `tool_input` key whose text replaces `intent` when present, such as `case.intent` |
| `question` | `Does this finding go against the filter intent?` | the yes or no wording asked per finding |
| `action` | `send-back` | `send-back`, `strip` or `flag` |
| `max_rounds` | `3` | send backs per target before the text passes flagged; a whole number of at least 1 |

## Text each tool offers

| Tool | Text |
|---|---|
| Write | `content` |
| Edit | `new_string` |
| MultiEdit | each `edits[].new_string` |
| NotebookEdit | `new_source` |
| `ledger_write`, `inbox_send` | `tool_input.text` |
| `judge` | `text`, each `evidence[]` item, `case.intent`, each `case.points[]` and each evidence item's `claim` and `excerpt` |

## Finders

An inline finder is a regular expression; each non-empty match is a finding with
its `reason` (default `matched a finder`).

A named finder is a script in `<layer>/conditions/_finders/<name>.py` or `.sh`.
The `_finders` folder is never scanned as conditions, and a later layer's finder
overrides an earlier finder of the same name. Contract:

- stdin: `{"text": ..., "path": ..., "tool": ...}`
- stdout: a JSON list of `{"start", "end", "text", "reason"}`, where `text` equals
  the source between `start` and `end`
- a finder that exits non zero, times out after `CONDITIONS_TIMEOUT_SEC`, or
  prints spans that do not match the source yields no findings and is logged

Built-in finders, shipped in the package:

| Name | Finds |
|---|---|
| `string_literals` | Python string and f-string literals through `tokenize`; string literals and text nodes in `.js`, `.ts`, `.jsx`, `.tsx`, `.vue`, `.svelte`, `.html` and `.htm`; reason `user-facing literal` |
| `explanation_tail` | inside a literal, the span from `because`, `since`, `which means`, `so that` or `; ` to the end of the literal; reason `explanation tail` |
| `judge_cues` | a package `_finders` script used by the judge blind filter |

## Synthetic tools

Three choke points that are not agent tools run filters under a tool name of
their own, so a filename such as `pre-ledger_write-x.filter.yaml` parses as an
ordinary condition:

| Name | Call site |
|---|---|
| `ledger_write` | the ledger server's comment, follow up and question writes |
| `inbox_send` | `agentihooks msg send` |
| `judge` | the judge's case and evidence; the package ships its filters, and the judge's call site lands with the judge |

The call site runs `hooks.filters.check(tool, tool_input)`. A deny is returned to
the writer as a refusal carrying the findings; a strip rewrites the text, and a
strip that leaves nothing refuses the write. Any error inside `check` allows the
write. `condition_list` names the three tools and reports a filename whose
matcher is a near miss of one of them.

## Actions

| Action | Effect |
|---|---|
| `send-back` | denies the call; the reason lists each confirmed finding not yet at the [round cap](#round-cap) as `- "<span>": <reason>` |
| `flag` | allows the call and adds the findings as context, `filter flagged:` |
| `strip` | removes the confirmed spans through an input rewrite where the harness applies rewrites at `pre`; elsewhere it sends back. Outside `bypassPermissions` the rewrite asks for permission |

Several `pre` filters on one call run in a chain: each sees the input as rewritten
by the filter before it.

## Round cap

Send backs are counted per session, filter and target under
`<runtime>/filters/rounds/`. The target is the resolved file path, or for a
synthetic tool the tool name and the text's place in the input. Sending the same
text again counts as a round.

When the count reaches `max_rounds`, the next run allows the text, adds the
findings as context marked `passed after N send-backs`, and writes
`filter flagged: round cap reached` to the hook log. In a swarm session
(`AGENTIHOOKS_SWARM` and `AGENTIHOOKS_SWARM_TASK` set) it also comments on that
ledger task, so the operator sees a filter or an agent failing. A clean pass
resets the target's count.

## Fail open

A filter never blocks work because it broke:

- an invalid filter body is a condition error and the call is allowed
- a crashing filter, or one that runs past `CONDITIONS_TIMEOUT_SEC` (default 10
  seconds), is a condition error and the call is allowed
- a failing finder yields no findings
- with the classifier unavailable or timing out, the text passes and the hook log
  records `filter passed: classifier unavailable`

## Answer cache

Classifier answers are cached in `~/.agentihooks/classifier/answers.json`, keyed
by a hash of the intent, question, finding text, reason, enclosing lines and the
configured model set (`AGENTIHOOKS_CLASSIFIER_MODELS`). Entries expire after
`AGENTIHOOKS_FILTER_CACHE_TTL_S` seconds (default seven days). A run asks only
the uncached findings and skips the classifier when every finding is cached, so
changing the intent or question asks again.

## Setting a filter from a session

Filters are created only when the operator asks, under the same
[gate](../hooks/conditions.md#creating-conditions-from-a-session) as conditions.
The gate also opens on filter wording: *set*, *add*, *create*, *update* or
*remove* a filter or classifier, as in *set a ui slop filter*.

```
condition_set(step="pre", matcher="edit+write+multiedit", name="ui_slop",
              language="filter", script="<the YAML body>", scope="directory")
```

The body is checked against the schema before the file is written; an invalid
body is refused with the schema error. Writes into any layer's
`conditions/_finders/` are gated like condition files.

| Tool | For a filter |
|---|---|
| `condition_list` | lists it with its `mode`, `action` and `max_rounds` |
| `condition_show` | prints the YAML |
| `condition_clear` | removes it |
