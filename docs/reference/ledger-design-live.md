---
title: Ledger design with Impeccable live
parent: Reference
nav_order: 11
---

# Ledger design with Impeccable live

The ledger front end (ledger pages, HOME and BIN) is set up for [Impeccable](https://github.com/pbakaus/impeccable) live mode: you pick an element in the browser, ask for variants, and the accepted one is written back to source.

| File | Role |
|---|---|
| `PRODUCT.md` | Who uses the pages and why, product constraints |
| `DESIGN.md` | The visual system: tokens, components, rules |
| `.impeccable/design.json` | Sidecar for the Impeccable live panel |
| `.impeccable/live/config.json` | Live mode targets `scripts/swarm_ledger/template.html` and `home.html` |

## Rules

- **Never run live mode in the primary checkout.** The shared ledger server on port 8765 runs from `~/dev/tcc-ecosystem/agentihooks`. Live mode writes a script tag into `template.html` and `home.html`, so every open ledger would reload with it. Work in a worktree with a scratch server.
- The scratch server reads copies of ledgers, never `~/development-ledger` itself, so a click on the page cannot steer a live swarm.
- Colours change only in `palette.css`. `DESIGN.md` lists the other rules.

## Start a live session

In a terminal, one command per line:

```bash
cd ~/dev/worktrees/agentihooks/<your-worktree>
export LEDGER_DIR=$HOME/scratchpad/agentihooks/ledger-design
export LEDGER_PORT=8790
mkdir -p "$LEDGER_DIR"
cp ~/development-ledger/<slug>.json "$LEDGER_DIR/design-copy.json"
cp ~/development-ledger/<slug>.html "$LEDGER_DIR/design-copy.html"
~/dev/tcc-ecosystem/.venv/bin/python scripts/swarm_ledger/ledger_server.py --serve
```

`LEDGER_PORT` takes effect only because `LEDGER_DIR` is not `~/development-ledger`. The server reloads itself when a `.py` or `.html` file under `scripts/swarm_ledger` changes.

In a second terminal, start Claude Code in the same worktree and type:

```text
/impeccable live
```

Then open the page the agent names, for example:

- Ledger page: `http://127.0.0.1:8790/design-copy`
- HOME: `http://127.0.0.1:8790/`
- BIN: `http://127.0.0.1:8790/?view=bin`

The Impeccable bar appears at the bottom of the page. Select an element, pick an action, and accept a variant. The agent writes the accepted variant into `template.html` or `home.html` and moves its colours into `palette.css`.

## End a live session

Close the tab or say "exit live" to the agent. It runs `impeccable live-server stop`, which removes the injected script. Before you commit, `git status` must not show the live script in `template.html` or `home.html`. Stop the scratch server with Ctrl+C.
