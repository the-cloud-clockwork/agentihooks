# Swarm v2 brain context quality

Package SV2-PRF-04. The harness in `tests/evaluation/brain_context/evaluate.py` packs held out engineering
questions from `tests/evaluation/brain_context/corpus.json` under each ranking and packing policy and grades the
packs against expected supporting sources and reviewer labels. It measures relevance and avoided repeated mistakes,
never the number of brain tool calls, arcs or tokens. The case records are `evidence/SV2-PRF-04/a-result.json`,
`b-result.json` and `c-result.json`.

## Corpus

Two independent fixtures, `alpha` and `beta`, each grant one project to one engineer and hold four held out tasks:

| Case | What it checks |
|---|---|
| Held out incident | The query matches the incident arc; the lesson that resolved it shares no words with the query and is reachable only over a `resolved_by` edge. |
| Prior design decision | The query matches the arc that led to a decision; the decision is reachable only over a `decided_by` edge. |
| Unrelated hot arc | The hottest arc in the project shares nothing with the query and must stay out of the pack. |
| Superseded guidance | Old and new guidance both match the query; the old one carries `superseded_by` and must yield to the new one. |

Each fixture also holds a lexically matching arc and lesson in a project outside the grant, an accepted edge into
that project, a proposed edge to an unrelated arc and an accepted inferred edge to an unrelated second hop.

## Policies

| Policy | Rule |
|---|---|
| `feed_only` | Today's fleet feed: hottest items, no scope, no query. |
| `scoped` | Accepted baseline: lexical matches inside the grant, superseded guidance omitted when its successor is visible. |
| `graph` | `scoped` plus one hop from source to target over accepted `resolved_by`, `decided_by` and `related` edges inside the grant. |
| `graph_wide` | Two hops, proposed edges, `supersedes` followed as a similarity edge, no superseded omission. |
| `scoped_padded` | `scoped`, then filled with the hottest remaining items up to a larger budget. |

Every policy packs at most 6 items and 900 tokens unless it names its own limit; the two rejection policies allow 12
items and 1800 tokens and break the kernel contract on purpose. Every pack names the superseded and over budget
items it left out, without naming anything outside the grant.

The questions are authored from incidents, decisions and superseded guidance recorded in this fleet's seat lessons
and swarm culture. The implementing agent wrote the review labels; the corpus `labels` block records that human
review is pending.

## Results

| Fixture | Policy | Recall | Usefulness | Mistakes avoided | Leakage | Irrelevant | Stale | Arcs | Tokens |
|---|---|---|---|---|---|---|---|---|---|
| alpha | feed_only | 0.2 | 0.083 | 0.0 | 8 | 13 | 1 | 24 | 3560 |
| alpha | scoped | 0.6 | 0.8 | 0.5 | 0 | 1 | 0 | 5 | 520 |
| alpha | graph | 1.0 | 0.857 | 1.0 | 0 | 1 | 0 | 7 | 720 |
| beta | feed_only | 0.2 | 0.083 | 0.0 | 8 | 13 | 1 | 24 | 3440 |
| beta | scoped | 0.6 | 0.8 | 0.5 | 0 | 1 | 0 | 5 | 760 |
| beta | graph | 1.0 | 0.857 | 1.0 | 0 | 1 | 0 | 7 | 960 |

Graph expansion finds the connected incident lesson and design decision in both fixtures with no leakage, no
more irrelevant items and no stale guidance, so it qualifies against `scoped`. It spends 200 more tokens per fixture,
reported as volume and never counted as the gain.

## Qualification rule

`evaluate.qualify` qualifies a candidate only when `context_source_recall`, `reviewed_usefulness` or
`repeated_mistakes_avoided` rises and none of them falls, `scope_leakage`, `irrelevant_injection` and
`stale_injection` do not rise, and leakage is zero. Case B shows `graph_wide` and `scoped_padded` reach full recall
with more arcs and tokens and are refused for irrelevant and stale injection, `feed_only` is refused for leakage, and
a pack that only adds arcs and tokens answers `no_gain`. Refusals leave the accepted record byte for byte unchanged
and name no out of scope item.

## Rollback

The accepted policy lives in `evaluate.Registry`, keyed by corpus revision and sequence and changed only under a
file lock. A request carries the whole report: the registry digests it, qualifies a promote against the accepted
totals itself and ignores any verdict the caller sends. Rollback is a revert to an earlier accepted policy, refused
when the report hash differs from the hash that policy was accepted with, so a revert proves the comparison
reproduces on the same corpus revision.
Case C interrupts a revert after staging, restarts, discards the staged write, applies the revert by the same
operation id, replays it without a second effect and refuses a stale sequence, another corpus revision, a reused
operation id and a drifted report.

## Measure

```bash
python -m tests.evaluation.brain_context.evaluate --fixture alpha
python -m tests.evaluation.brain_context.evaluate --fixture beta --policy scoped --policy graph
```

## Limits

The packers mirror the kernel ranking contract instead of calling brain-api, and semantic and heat sources are not
modelled. Usefulness is reviewer labels, not task outcomes. A live comparison with agents consuming packs belongs to
the G10 rollout gate.
