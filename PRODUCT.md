# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One operator who runs agent swarms. He keeps a ledger page open for hours on a 1920x1080 screen while Claude and Codex agents work a plan. From it he follows progress, answers agent questions, checks phases, writes notes and comments, gives verdicts on health findings, steers the swarm (start, pause, stop, caps, gates, autonomy) and chats with the swarm master. Agents never read the page visually: they write to the same ledger through the `agentihooks ledger` and `agentihooks swarm` CLIs and the ledger server API.

## Product Purpose

The ledger front end is the presentation layer of agentihooks. Since 2026-10-06 the operator treats it as a product in its own right rather than a skill's by-product. It shows one plan's record (overview, sources, phases, tasks with proof contracts, questions, follow-ups, notes, artifacts, chat, notifications) and the live swarm working it (agents, lanes, quota, gates, health, Doctor). HOME lists every ledger and BIN holds deleted ones for 30 days. Success means the operator sees what needs him at a glance and acts in one click, without opening a terminal.

## Positioning

It is the only window onto an agentihooks swarm, and its records are the swarm's own: every operator write on the page becomes an inbox item for the agent that owns the task, and every agent write shows on the page without a refresh. Generic project boards show tickets; this page shows the agents doing the work, their quota and their proof.

## Operating Context

- Served on localhost by the ledger server (`agentihooks ledger serve --ensure`, default `http://127.0.0.1:8765`), Python stdlib only, no build step. Viewed in desktop Chrome on Windows from WSL.
- Ledger state is a JSON document per ledger under `~/development-ledger`; the page embeds a seed and syncs through the server API.
- The swarm tick, inbox and health data come from Redis through the same server.
- Agents and the operator act on the same records concurrently, often dozens of writes a minute in a busy swarm.

## Capabilities and Constraints

- **Stack (operator decision):** plain HTML, CSS and vanilla JavaScript only. No frameworks, bundlers or build step (no React, Next or similar).
- **Direction (operator, not yet designed):** the presentation layer gets its own architecture, a proper API, and possibly SQLite as the store in place of per-ledger JSON files. Undecided: schema, migration path, API shape.
- Front-end sources: `scripts/swarm_ledger/template.html` (ledger page), `scripts/swarm_ledger/home.html` (HOME and BIN), `palette.css` (every colour value, nowhere else), `tooltips.js`. The server fills `__LEDGER_*__` / `__HOME_*__` placeholders at render time.
- Every page section folds on a click of its header and remembers that per viewer; a new section ships foldable or its test fails.
- A page edit changes the page version (hash of template, palette and tooltips), and open ledger pages re-render from the template on their next load.
- Vocabulary: ledger, phase, task, proof, follow-up, question, note, seat, lane (eng, ci, plan), master, swarm tick, gate, health finding, verdict, Doctor, inbox, BIN.

## Brand Commitments

- Name: agentihooks, set in lowercase in the brand mark and uppercase in the HOME header.
- Logo: `media/agentihooks-logo.png`, used as a mask in the header and as the HOME watermark.
- Design system 2026-001 (operator's design-system base) governs structure. Operator overrides for this product: no text glow, and a near-black canvas without the blue wash.

## Evidence on Hand

- Live ledgers in `~/development-ledger` (for example `rig-grade-swarm`, 287 tasks) are the real content to design against.
- No user research, analytics or external users exist. Do not invent them.

## Product Principles

1. The operator steers and the agents write. Every screen answers "what needs me now?" before anything else.
2. State over decoration. Colour and type carry status; nothing decorative competes with a state change.
3. One source of truth. The page renders the ledger record; it never holds state the server does not.
4. Minimal stack. HTML, CSS and vanilla JS served by the ledger server; a new dependency needs the operator's decision.
5. Fit a working day. The page stays readable and calm after hours open, with dense data at 1920x1080.

## Accessibility & Inclusion

Keyboard reachable controls with a visible `focus-visible` ring, and text contrast held against the canvas for every role colour. No further standard has been set.
