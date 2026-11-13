---
title: Docs sanity check
parent: Reference
nav_order: 78
---

# Docs sanity check

Public docs and evidence use placeholders and documentation values only. The
docs sanity check enforces that on every pull request and in the merge queue.
It is project agnostic: one standard library Python script and one reusable
workflow that any repository can call.

## What it flags

| Rule | Flags | Passes |
|------|-------|--------|
| `private-ipv4` | Addresses in the RFC 1918 private ranges, the RFC 6598 shared address space and the link local range | The RFC 5737 ranges `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, loopback, `0.0.0.0` and public addresses |
| `email` | Any address outside reserved domains | `example.com`, `example.org`, `example.net`, `.example`, `.test`, `.invalid`, `.localhost`, no reply mailboxes, `git@` remotes and URL user names |
| `private-host` | Names ending in `.lan`, `.local`, `.internal`, `.intranet`, `.corp`, `.localdomain`, `.home.arpa` or `.ts.net` | Names with an `example` label and Kubernetes `cluster.local` names |
| `private-ipv6` | Addresses in the RFC 4193 unique local and the link local ranges | The documentation range `2001:db8::/32`, loopback `::1` and public addresses |
| `cloud-host` | Host names that encode a private address in the `ip-a-b-c-d` form, and names carrying an 8 or 17 digit compute instance id | Names that encode a documentation or public address |
| `lab-name` | The home lab by name, its repository, its domain and host names under it, matched without case | Words that only contain the name, such as `antonym` |
| `secret-value` | Password or token like values after a password, secret, token or key name, after `Bearer`, or as the password in a URL | Placeholders such as `<...>`, `$VAR`, `${{ ... }}` or a value containing `example`, `your`, `redacted` or `changeme`, keys ending in `name`, `ref`, `file`, `path` or `id`, environment variable names, paths, package versions and values under 8 characters or without both a letter and a digit |
| Token shapes | GitHub, AWS, Slack, Anthropic, OpenAI and Google keys, JSON web tokens and private key headers | Vendor sample keys that contain `EXAMPLE` |
| `keyword` | Any private keyword the caller supplies, matched without case | Everything else |

Each hit prints as `file:line: rule`. The matched text is never printed, so a
private keyword does not leak into a public CI log. Any hit exits 1; a missing
path exits 2.

## Private keywords

Names, domains and other identifiers specific to one project are not built in.
Store them one per line, or comma separated, in a repository secret, by default
`DOCS_SANITY_KEYWORDS`, or in a file outside the repository. Lines starting with
`#` are ignored. Without keywords only the built in rules run, and the workflow
prints a notice.

## Baseline

A baseline lets the check go required before every existing hit is fixed. It
holds, per file, how many `lab-name` and `keyword` hits that file already
carries; a file fails only when it carries more. Address, host and secret hits
are never held, so they fail at once.

```bash
python scripts/docs_sanity.py --write-baseline tests/DOCS_SANITY_BASELINE.json docs evidence changelog.d README.md
python scripts/docs_sanity.py --baseline tests/DOCS_SANITY_BASELINE.json docs evidence changelog.d README.md
```

The baseline only shrinks. A pull request that removes hits rewrites it with
`--write-baseline`; the unit suite requires it to match the tree exactly.

## Run it locally

```bash
python scripts/docs_sanity.py docs README.md
python scripts/docs_sanity.py --keywords-file ~/private-keywords.txt --exclude '*/fixtures/*' docs
python scripts/docs_sanity.py --keywords-env DOCS_SANITY_KEYWORDS docs
```

## Call it from another repository

```yaml
name: Docs sanity check

on:
  pull_request:
  merge_group:
    types: [checks_requested]

permissions:
  contents: read

jobs:
  docs-sanity:
    uses: the-cloud-clockwork/agentihooks/.github/workflows/docs-sanity.yml@main
    with:
      paths: docs README.md
    secrets: inherit
```

| Input | Default | Meaning |
|-------|---------|---------|
| `paths` | required | Space separated files and directories to scan |
| `keywords_from` | `DOCS_SANITY_KEYWORDS` | Name of the caller secret holding private keywords |
| `exclude` | empty | Space separated globs of files to skip |
| `tool_repository` | `the-cloud-clockwork/agentihooks` | Repository that carries the scanner |
| `tool_ref` | `main` | Revision of the scanner to run |
| `bootstrap_ref` | empty | Revision to take the scanner from only when `tool_ref` does not carry it yet |

Make the check required by adding `docs-sanity / scan` to the branch ruleset's
required status checks once the workflow has run on one pull request.

## In this repository

The lint job of the Tests workflow scans `docs`, `evidence`, `changelog.d` and
`README.md` on every pull request into dev, with the private keywords from the
`DOCS_SANITY_KEYWORDS` secret and the baseline in `tests/DOCS_SANITY_BASELINE.json`.
It runs the scanner and reads the baseline from the protected grader checkout
of the base branch, so a pull request can neither weaken the rules that grade
it nor raise its own allowance; only while the base lacks the scanner does it run the pull request's
own copy. A red scan turns the lint job red, which `Gate — Required`, the
check the dev ruleset requires, refuses. Fork pull requests get no secrets, so
only the built in rules run for them. Workflow, chart and deployment files
that must carry real addresses are not scanned.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `private-ipv4` on a sample address | Use `192.0.2.x`, `198.51.100.x` or `203.0.113.x` |
| `email` on a sample address | Use `user@example.com` |
| `private-host` on a sample host | Use a name under `example.com` or `example.internal` |
| `lab-name` on a page | Describe the deployment generically, such as "the cluster" or "the deployment repository" |
| `cloud-host` on a node name | Use a placeholder such as `redacted-node-1` |
| `secret-value` on a sample | Use a placeholder such as `<token>` or `$API_TOKEN` |
| `keyword` with no visible cause | Compare the line against the private keyword list where it is stored; the log never shows the keyword |
| The check never reports on a pull request | The calling workflow must run on `pull_request` and `merge_group` |
