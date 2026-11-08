---
title: Workflow runs
parent: Reference
nav_order: 75
---

# Workflow runs
{: .no_toc }

A workflow is a YAML file of stages, each stage one or more
[headless profile runs](headless-run.md). `agentihooks workflow run` walks the
stages from the head to the exit, saves the run state after every stage and
prints that state as one JSON object. `agentihooks workflow status` reads the
saved state without launching anything, and `agentihooks workflow resume`
continues a saved run from the stage it stopped at.

Use it to chain profiles unattended, for example scouts, a judge, a planner and
an engineer, with retry loops between them. `list`, `show` and `validate` read
and check workflow files without running them.

1. TOC
{:toc}

## Usage

```bash
agentihooks workflow list
agentihooks workflow show NAME
agentihooks workflow validate FILE|NAME
agentihooks workflow run NAME --case FILE [--run-id ID] [--harness auto|claude|codex]
agentihooks workflow status RUN
agentihooks workflow resume RUN
```

| Command | What it does |
|---|---|
| `list` | Prints every loaded workflow as `name`, layer and file, tab separated; invalid files are reported on stderr and set exit 1 |
| `show NAME` | Prints one workflow with every default filled in, headed by its layer and file |
| `validate FILE\|NAME` | Checks a file path, or a loaded name; prints `valid: <name> (<path>)` or each error |
| `run NAME` | Starts a new run of a loaded workflow and drives it to `passed` or `exhausted` |
| `status RUN` | Prints the saved state of a run; never launches a profile |
| `resume RUN` | Continues a saved run with its saved definition and harness |

`run` flags:

| Flag | Default | Meaning |
|---|---|---|
| `NAME` | required | Workflow name, as `list` prints it |
| `--case FILE` | required | Text file holding the case: the request every stage receives |
| `--run-id ID` | 32 hex characters | Name of the run folder; one path component, never empty, `.`, `..` or containing `/` |
| `--harness` | `auto` | `claude` or `codex` for every stage; `auto` reads `AGENTIHOOKS_TARGET` and falls back to `claude` |

## Examples

List what is loaded, check a draft and run it:

```bash
agentihooks workflow list
agentihooks workflow validate ./review.yaml
printf 'Add a --dry-run flag to the cleanup command.\n' > case.txt
agentihooks workflow run simple-workflow --case case.txt --run-id cleanup-dry-run
```

Read the result in a script:

```bash
if report=$(agentihooks workflow run simple-workflow --case case.txt --harness codex); then
  jq -r '.run, .runs' <<<"$report"
else
  jq -r '.error // "\(.status) \(.action)"' <<<"$report"
fi
```

Check on a run from another shell, and continue it after the process was
killed:

```bash
agentihooks workflow status cleanup-dry-run | jq '{status, stage, runs}'
agentihooks workflow resume cleanup-dry-run
```

A live two stage run on Codex, recorded on the delivering pull request
([#7785](https://github.com/the-cloud-clockwork/agentihooks/pull/7785)),
`definition` and `case` shortened:

```json
{"stage": null, "status": "passed", "iterations": 0, "visit": 1,
 "runs": {"first": 1, "second": 1},
 "outputs": {"first": ["{\"verdict\":\"pass\",\"proof\":\"workflow commands\"}"],
             "second": ["{\"verdict\":\"pass\",\"proof\":\"workflow commands\"}"]},
 "notes": null, "action": null, "workflow": "proofdc5d10ab5b624a8a9a17b7e7af084cd0",
 "run": "e3818628535048e79083fecce176d5d8", "case": "Return exactly ...",
 "definition": {"workflow": {"name": "...", "head": "first", "exit": "second.pass", "...": "..."},
                "stages": {"first": {"...": "..."}, "second": {"...": "..."}}},
 "harness": "codex"}
```

`status` and `resume` on that run both exited 0 and printed the same JSON;
`resume` launched nothing because the run had already passed.

## Workflow files

A workflow file is block style YAML with two keys, `workflow` and `stages`.
Flow style (`{...}` or a non-empty `[...]`) and unknown keys are refused. The
package ships `simple-workflow`:

```yaml
workflow:
  name: simple-workflow
  intent: Build the change the case asks for, backed by scouted evidence and a blind verdict.
  head: scout
  max_iterations: 3
  exit: build-verdict.pass
  on_exhausted: block

stages:
  scout:
    profile: scouter
    count: 3
    input:
      - case
    iterations: 2
    exit: every point of the case has cited evidence
    next:
      pass: evidence-verdict
  evidence-verdict:
    profile: judge
    input:
      - case
      - scout
    next:
      pass: plan
      retry: scout
      enhance: scout
```

(The shipped file continues with `plan`, `build` and `build-verdict`, and
writes every default out.)

`workflow` keys:

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Name used by `show`, `validate` and `run` |
| `intent` | required | One line saying what the workflow is for |
| `head` | required | First stage; every stage must be reachable from it |
| `exit` | required | When the workflow passes: `<stage>.pass`, or a condition in plain words (see [Exit conditions](#exit-conditions)) |
| `max_iterations` | `3` | `retry` and `enhance` edges allowed before the run ends `exhausted` |
| `on_exhausted` | `block` | `block`, `escalate` or `keep-best`; recorded as `action` in an exhausted run |

Stage keys:

| Key | Default | Meaning |
|---|---|---|
| `profile` | required | Profile each run of the stage uses |
| `tier` | `high` | Model tier, `high` or `low` ([Model tiers](headless-run.md#model-tiers)) |
| `effort` | `medium` | Effort passed to the profile run |
| `count` | `1` | Profile runs started in parallel for this stage |
| `input` | `[]` | Earlier stages whose outputs this stage receives; `case` may be listed and is always sent |
| `filters` | `[]` | Filter names carried in the definition; the command line runner does not apply them |
| `iterations` | `1` | Times the stage may run in a row while its own `exit` is not met |
| `exit` | `null` | Stage condition; `null` takes the launch outcome as is |
| `next` | all `null` | Target stage, `exit` or `null` for each outcome `pass`, `retry` and `enhance` |

`validate` refuses a head or `next` target that is not a stage, an `input` that
is neither `case` nor a stage, a `<stage>.pass` exit naming no stage, and a stage
the head cannot reach.

### Layers

Workflows load from these folders, in order; a later file with the same `name`
wins:

| Layer | Folder |
|---|---|
| `package` | `workflows/` in the installed package profiles (`profiles/package/workflows`) |
| `bundle`, `shared-bundle`, `user-bundle` | `workflows/` in each linked bundle |
| `profile:<name>` | `workflows/` in each profile of the active chain |
| `project` | `.agentihooks/workflows/` at the root of the git repository holding the current directory |

Files end in `.yaml` or `.yml`. `list`, `show` and `validate` read all six
kinds of layer. `run` reads every layer except `project`, and refuses to start
when any loaded file is invalid.

## How a run works

1. `run` creates `<AGENTIHOOKS_HOME>/workflows/runs/<run>/` with an empty
   `outputs/`, and saves `workflow.json` holding the start state, the case
   text, the full definition and the harness.
2. The current stage receives one JSON object: `{"case": <case text>}` plus,
   for each stage named in its `input` that has run, that stage's outputs under
   its id. The object is the `--input` prompt of `count` parallel headless
   profile runs with the stage's profile, tier and effort, a 900 second
   timeout and no schema.
3. The stage outcome is `pass` when every profile run reports `ok`, and `retry`
   otherwise. Its outputs are the list of each run's `output`.
4. The stage's own `exit`, when set, can turn the outcome into `pass`. When it
   is not met and the stage has iterations left, the same stage runs again.
5. When the workflow `exit` is met the run ends `passed`. Otherwise the
   outcome's `next` edge picks the next stage; an `exit` target ends the run
   `passed`. Each `retry` or `enhance` edge counts one iteration and stores the
   outcome and outputs in `notes`; at `max_iterations` the run ends
   `exhausted` with `action` set to `on_exhausted`.
6. `workflow.json` is written after every stage through a temporary file,
   fsync and rename, so a crash never leaves a partial state file.

`resume` loads `workflow.json` and repeats steps 2 to 6 from the saved stage. It
uses the definition and harness saved at `run`, so later edits to the workflow
file or a deleted case file do not affect it. A stage that was interrupted
before it finished runs again from the start. A finished run is printed as
saved and launches nothing.

### Exit conditions

| Expression | Met when |
|---|---|
| `<stage>.pass` as the workflow `exit` | That stage's outcome is `pass` |
| Plain words as the workflow `exit` | A stage outcome is `pass` and a low tier headless classifier judges the condition true over every stage's outputs so far |
| Plain words as a stage `exit` | The classifier judges the condition true over that stage's outputs |

The classifier is one extra sealed headless run per check at low tier and low
effort. It ignores `--harness`: it runs on `AGENTIHOOKS_TARGET`, else Claude,
and falls back to Claude when Codex refuses the route.

## Run folder

```text
<AGENTIHOOKS_HOME>/workflows/runs/<run>/
├── workflow.json
└── outputs/
    └── <stage>/
        └── <random>/          one folder per time the stage ran
            ├── 0/             headless run folder of the first parallel run
            │   └── transcript.jsonl
            └── 1/
```

`AGENTIHOOKS_HOME` defaults to `~/.agentihooks`. Each numbered folder is a
[headless run folder](headless-run.md#run-folder) with its transcript.

## Output

Every command except `list`, `show` and `validate` prints one JSON object on
stdout. `run`, `resume` and `status` print the saved state:

| Field | Meaning |
|---|---|
| `status` | `running`, `passed` or `exhausted` |
| `stage` | Stage that runs next; `null` once the run ends |
| `runs` | Times each stage ran |
| `outputs` | Latest outputs of each stage that ran |
| `iterations` | `retry` and `enhance` edges taken |
| `visit` | Runs of the current stage in a row |
| `notes` | The last loop edge: `stage`, `outcome` and `outputs`; `null` before one |
| `action` | `on_exhausted` value when the run is `exhausted`, else `null` |
| `workflow`, `run`, `case` | Workflow name, run identifier and case text |
| `definition`, `harness` | The workflow as loaded at `run`, and the harness every stage uses |

A refusal prints `{"error": "<reason>"}` instead.

### Exit codes

| Code | When |
|---|---|
| `0` | `run` or `resume` ended `passed`; `status` read the state, whatever its status |
| `1` | `run` or `resume` ended `exhausted`, or any `{"error": ...}` refusal |
| `2` | Missing `--case` or `NAME`, or a `--harness` outside `auto`, `claude` and `codex` (usage on stderr) |

## Limitations

- `run` does not load the project layer, `.agentihooks/workflows/` in the
  repository; `list` shows those workflows but `run` answers
  `no workflow named <name>`. Put a workflow you run in a bundle or profile
  `workflows/` folder.
- `filters` and `on_exhausted` are recorded and not acted on by the command
  line runner.
- Runs take no lock: two `resume` commands on the same run launch the same
  stage twice and the later save wins.
- Every profile run of a stage has a fixed 900 second timeout.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `{"error": "no workflow named X"}` | Not loaded, or only in the project layer; check `agentihooks workflow list` and move the file to a bundle or profile `workflows/` folder |
| `{"error": "<file>: <reason>"}` lines on `run` | A file in some layer is invalid and blocks every run; fix or remove it, then `agentihooks workflow validate <file>` |
| `{"error": "run identifier must be a single name"}` | `--run-id`, `status` or `resume` got an empty value, `.`, `..` or a path; pass a bare name |
| `{"error": "[Errno 17] File exists: ..."}` | `--run-id` names an existing run, which is never overwritten; `resume` it or pick another id |
| `{"error": "[Errno 2] No such file or directory: .../workflow.json"}` | No run by that name under `<AGENTIHOOKS_HOME>/workflows/runs`; check `AGENTIHOOKS_HOME` and the id |
| `{"error": "<stage> has no retry edge"}` | A stage failed and its `next` has no target for that outcome; the run stays `running` at that stage. Read the transcripts under `outputs/<stage>/`, fix the cause, then `resume` |
| `status: exhausted`, exit 1 | The run took `max_iterations` loop edges; `notes` holds the last stage outcome and outputs |
| A stage keeps retrying | One of its profile runs did not report `ok`; read `transcript.jsonl` in `outputs/<stage>/` and the [headless troubleshooting](headless-run.md#troubleshooting) |
