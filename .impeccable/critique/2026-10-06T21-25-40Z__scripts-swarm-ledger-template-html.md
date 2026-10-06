---
target: ledger page, HOME and BIN
total_score: 19
max_score: 40
na_heuristics: 
p0_count: 3
p1_count: 6
target_identity: "file:/home/iamroot/dev/worktrees/agentihooks/session-ce5108a4/scripts/swarm_ledger/template.html"
target_fingerprint: "sha256:fd06c71b48a606b3e52d4f4d3011921732f8afed526d969ac6a96b11ddf874e8"
target_path: /home/iamroot/dev/worktrees/agentihooks/session-ce5108a4/scripts/swarm_ledger/template.html
timestamp: 2026-10-06T21-25-40Z
slug: scripts-swarm-ledger-template-html
---
Method: dual-agent (A: design review, B: detector + headless-browser evidence), plus a separate architecture audit. Pages: a copy of the 278-task swarm ledger, HOME and BIN, on a scratch ledger server, headless Chromium at 1920x1080. Run on 2026-10-06 before PR #983 (glow and blue wash removed); items fixed by #983 are marked.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Swarm tab says "unavailable" with no reason |
| 2 | Match System / Real World | 3 | Dense internal vocabulary fits the trained operator |
| 3 | User Control and Freedom | 2 | No undo on note or comment delete |
| 4 | Consistency and Standards | 2 | Shipped CSS contradicted the written design rules (glow, wash), fixed in #983 |
| 5 | Error Prevention | 2 | HOME delete fires on one click (soft delete to BIN, no confirm) |
| 6 | Recognition Rather Than Recall | 2 | About 280 truncated outline titles need recall of ids and abbreviations |
| 7 | Flexibility and Efficiency | 1 | No keyboard row navigation, no bulk actions, no task filter |
| 8 | Aesthetic and Minimalist Design | 2 | Priorities print the same sentence twice; sections all open at once |
| 9 | Error Recovery | 1 | HOME shows raw server text in a native `alert()` |
| 10 | Help and Documentation | 1 | Five red icon-only floating buttons with no legend beyond hover tips |
| **Total** | | **19/40** | **Poor** |

## Design Specificity Verdict

The token system is specific to this product (one palette file, five-step monospace scale, frameless hover controls, design system 2026-001). The shipped skin was generic dark-dashboard: glowing status words and a blue radial wash, both against the written rules (fixed in #983). The structure underneath (layout, density, interaction) is where the remaining work is.

Detector: source scan 5 findings (all-caps-body x3, clipped-overflow-container x2). Rendered pages through headless Chromium: ledger 8, HOME 10 (text-overflow x3, dark-glow x7), BIN 4. False positives: file-mode `low-contrast` and `design-system-font-size/radius` on HOME and BIN (the static scan does not apply the inline CSS, so it measures against white); `design-system-color` on the canvas `rgb(3,5,11)`, which is the declared canvas.

## What's Working

- Every text role passes WCAG AA against the canvas, including 11px muted text (8.45:1) and the weakest hue, signal red (5.4:1).
- No colour literal outside `palette.css`, no inline `style=` attributes, every button and link has an accessible name, all 211 form controls on the ledger page are labelled, heading order is correct.
- Tabular monospace keeps counts and times aligned; per-viewer fold state suits a page left open for hours.

## Priority Issues (anti-pattern backlog)

### Design and UX

1. **[P0] HOME table has no table semantics.** `home.html`: the column header row is `aria-hidden="true"` and rows are `<li>` grids, so a screen reader hears unlabeled numbers. Fix: a real `<table>` with `<th scope="col">`, or `role=grid` with visible column headers. Command: harden.
2. **[P1] The outline is an unfiltered wall.** `.outline` in `template.html` lists every task (about 280) truncated with ellipsis, with no filter or grouping beyond section heads. Fix: a type-ahead filter, grouping by phase, collapsed done items. Command: distill / layout.
3. **[P1] Native `alert()` for HOME errors.** The bin script in `home.html` ends `alert(await r.text())`. Fix: an inline notice in the page's own style with plain-language text. Command: harden / clarify.
4. **[P1] Everything opens at once.** Overview, priorities, notes, 11 open questions, phases and hundreds of tasks render expanded; 5 of 8 cognitive-load checks fail, and decision points with more than 4 options include open questions (11) and the outline (about 300). Fix: lead with "needs you now", collapse answered questions and done phases by default. Command: distill.
5. **[P2] Priorities repeat themselves.** `renderPriorities` prints `.prio-title` and `.prio-text` at nearly the same weight, often the same sentence twice. Fix: drop the second line when it repeats the first, or subordinate it. Command: clarify.
6. **[P2] Red means six things.** `--signal` and `--destructive` share one red for destructive actions, the offline banner, section ticks, unread dots, task ids and all five floating buttons (sync, top, bell, home, chat). Fix: neutral treatment for navigation buttons; red only for alert and destructive. Command: colorize.
7. **[P2] One-click delete on HOME.** `.act.del` moves a ledger to BIN with no confirmation (BIN keeps it 30 days). Fix: an undo notice after the move. Command: harden.
8. **[P2] Silent swarm tab.** The Swarm tab shows "unavailable" without saying why or what to do. Command: clarify.
9. **[P3] No keyboard list navigation or bulk actions.** Only menus handle keys. Fix: row focus with j/k, multi-select with bulk approve or clear. Command: harden.
10. **[P3] Terse empty states.** "The bin is empty.", "Nothing waits on you." offer no next action. Command: onboard.

### Measured (detector and DOM)

11. **[P2] Uppercase on long runs.** `all-caps-body`: uppercase applied to 32–40 character labels in `template.html`. Keep uppercase for short labels. Command: typeset.
12. **[P2] 11px body text.** `tiny-text` on the ledger page meta lines. Command: typeset.
13. **[P2] HOME overviews are cut off.** `text-overflow`: overview cells overflow by up to 1169px and titles by 99px, hidden by ellipsis. Command: layout.
14. **[P2] Clipping containers.** `html` and `body` clip positioned children (`clipped-overflow-container`), a risk for popovers and menus. Command: harden.
15. **[P3] Spacing and radius off-token.** 59 literal margin, padding and gap values bypass the three `--space-*` tokens; 5 literal radii and 6 z-index values with no tokens. Command: polish.
16. **[P3] HOME has no reduced-motion rule.** Its 3 transitions are not gated by `prefers-reduced-motion` (the ledger page is). Command: harden.
17. **[P3] HOME and BIN have no `nav` landmark.** Command: harden.

### Architecture (separate audit)

18. **[P0] Every write rewrites the whole document.** `ledger_core.sync` re-reads and re-writes the full JSON (up to 6 MB) and HTML for one checkbox, under one global lock.
19. **[P0] Whole-document polling.** The page fetches the full document every 2 s and the swarm panel every 2 s (about 60 fetches a minute per tab); `watch_ledger` re-reads the file every 3 s per agent.
20. **[P1] Page weight.** The ledger page is 951 KB, 82% of it the embedded JSON seed, with about 17,300 DOM elements.
21. **[P1] One inline script** of about 180 functions in `template.html`, with no modules.
22. **[P1] No versioned API or schema.** Routes are string matches and bodies are checked by hand.
23. **[P2] No Content-Security-Policy on page responses.** Only media and artifacts carry one.

Fixed by #983: glow on 39 + 9 text-shadow declarations, 17 + 4 drop-shadow and halo box-shadows, and the blue radial wash.

## Persona Red Flags

- **Alex (power user):** no row-level keyboard navigation; each verdict is a separate click across 11 open questions and 280 tasks; no filter.
- **Sam (keyboard and screen reader):** HOME column headers hidden from assistive tech; five floating buttons distinguished only by icon and the same red.
- **The operator mid-swarm:** "what needs me now" is buried under fully expanded sections and a 280-row outline.

## Minor Observations

- `DESIGN.md` documents the canvas as #010104; the code matches only after #983.
- HOME kind labels are correctly bare text, no capsule.

## Questions to Consider

- If the page's job is "what needs me now", why does the Priorities section cost two reads per item?
- Is the 280-row outline a sidebar, or the task list the main pane is missing a filter for?
- Should a gate diff the shipped CSS against `DESIGN.md` rules, so drift like the glow cannot ship again?
