---
name: agentihooks ledger
description: The operator's console for agentihooks swarms; ledger pages, HOME and BIN.
colors:
  canvas-ink: "#050812"
  panel-navy: "#0d1424"
  accent-blue: "#3b82f6"
  link-blue: "#60a5fa"
  accent-blue-soft: "#93c5fd"
  selection-blue: "#1d4ed8"
  signal-red: "#ef4444"
  warn-yellow: "#facc15"
  warn-yellow-soft: "#fde68a"
  positive-green: "#4ade80"
  text-white: "#f8fafc"
  text-secondary: "#cbd5e1"
  muted-slate: "#9aa8bd"
  surface-1: "rgba(147, 197, 253, .05)"
  surface-2: "rgba(147, 197, 253, .075)"
  hover-lift: "#151c29"
  rule: "#151c29"
  edge: "#222f44"
  grip: "#60a5fa"
typography:
  title:
    fontFamily: "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-.01em"
  headline:
    fontFamily: "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 700
    letterSpacing: ".16em"
  item:
    fontFamily: "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 700
  body:
    fontFamily: "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "ui-monospace, 'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    letterSpacing: ".08em"
rounded:
  sharp: "4px"
  default: "6px"
  soft: "8px"
  fab: "12px"
spacing:
  scroll-gutter: "max(.5%, 12px)"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "32px"
components:
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.accent-blue}"
    rounded: "{rounded.default}"
    padding: "4px 10px"
    typography: "{typography.label}"
  button-ghost-hover:
    backgroundColor: "{colors.hover-lift}"
    textColor: "{colors.accent-blue}"
  button-destructive:
    backgroundColor: "transparent"
    textColor: "{colors.signal-red}"
    rounded: "{rounded.default}"
    padding: "4px 10px"
  link-action:
    backgroundColor: "transparent"
    textColor: "{colors.muted-slate}"
    typography: "{typography.label}"
  link-action-hover:
    textColor: "{colors.accent-blue}"
  fab:
    backgroundColor: "transparent"
    textColor: "{colors.signal-red}"
    rounded: "{rounded.fab}"
    size: "44px"
  input-inset:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text-white}"
    rounded: "{rounded.default}"
    padding: "10px 12px"
  panel:
    backgroundColor: "{colors.surface-1}"
    rounded: "{rounded.default}"
    padding: "16px 18px"
  table-row:
    textColor: "{colors.text-white}"
    typography: "{typography.body}"
    height: "34px"
  column-grip:
    backgroundColor: "{colors.grip}"
    width: "8px"
  overlay-row:
    height: "36px"
---

# Design System: agentihooks ledger

## Overview

**Creative North Star: "The Operator's Console"**

A dark, dense control surface for one operator watching many agents. The page is a record first: hairline-divided rows of monospaced text on a near-black canvas, with colour reserved for state. Structure follows the operator's design system 2026-001: bare labels instead of chips, frameless controls that show a surface only on hover, depth from two translucent lifts over a single canvas, and every colour in one palette file.

Two operator decisions override 2026-001 here. First, **no glow**: no `text-shadow`, `drop-shadow` or halo `box-shadow` on text, icons or controls. Role colour, weight and size carry emphasis. Second, the canvas is a deep blue black (#050812, operator decision 2026-10-12, replacing #010104) with no blue radial wash behind it.

Density is high and calm. A ledger page holds dozens of panels and hundreds of rows and must stay readable after hours open.

**Key Characteristics:**
- Monospace everywhere, five sizes (22 / 14 / 13 / 12 / 11 px), uppercase tracked labels for section and column heads.
- Deep blue-black canvas (#050812), two light-blue lifts (5% and 7.5%) for panels and wells, steel hairlines between rows.
- Colour is state: blue for action and links, green done or running, yellow paused or warning, red signal, destructive and open items.
- Frameless controls; the hover lift is the only frame.
- Flat: no shadows and no glow.

## Colors

One blue accent family over a near-black canvas, with red as a structural signal hue. Every value lives in `scripts/swarm_ledger/palette.css`: a ramp layer (`--ink-950`, `--blue-500`, …) and a role layer (`--canvas`, `--accent`, `--signal`, …). Components use role tokens only.

### Primary
- **Accent Blue** (`--accent` → `--blue-500`): primary actions, active outline items, claimed-by names, focus rings, progress bars.
- **Link Blue** (`--link` → `--blue-400`): ledger titles on HOME, in-text links, open counts.
- **Soft Blue** (`--accent-2` → `--blue-300`): kind and phase-state labels, the operator's own chat name.

### Secondary
- **Signal Red** (`--signal`, `--destructive` → `--red-500`): the section tick and header rule, task ids, priority links, unread dots, delete and stop actions, the floating buttons' icons.

### Tertiary
- **Positive Green** (`--positive` → `--green-400`): done counts, running swarms, checked checkboxes, approve.
- **Warn Yellow** (`--warn`, `--warn-2`): paused swarms, out-of-scope items, unsaved state.

### Neutral
- **Canvas Ink** (`--canvas` → `--ink-950`, #050812): the page ground, the only opaque surface.
- **Panel Navy** (`--panel`, `--overlay`): floating panels (chat, notifications) under a backdrop blur.
- **Text White** (`--text`): primary text, values.
- **Secondary Text** (`--text-2`): body copy in lists, proof values.
- **Muted Slate** (`--muted`, `--dim`): labels, column heads, timestamps, idle states.
- **Surface 1 / Surface 2** (`--bay-050`, `--bay-075`): light-blue lifts at 5% and 7.5% on ledger pages; HOME keeps surface 1 transparent.
- **Hover / Rule / Edge** (`--steel-100`, `--steel-100`, `--steel-200`): solid steel steps for the hover lift, row hairlines and edges.
- **Grip** (`--grip` → `--blue-400`): the hairline shown on a resize grip under hover, drag or focus.

### Named Rules
**The One File Rule.** A colour value appears only in `palette.css`. Changing a hue is one edit there; a change that needs a component file opened means the palette layer is broken.

**The State Colour Rule.** Hue means state. A colour used for decoration takes that meaning away from the state it stands for.

## Typography

**Body Font:** ui-monospace (with JetBrains Mono, SFMono-Regular, Menlo, Consolas, monospace)

**Character:** A single monospaced family at every size. Tabular figures keep counts and times aligned in columns; uppercase tracked labels separate structure from content without boxes.

### Hierarchy
- **Title** (700, 22px, 1.25): the ledger title in the page header.
- **Headline** (700, 12px, .16em tracking, uppercase): section and panel heads, the HOME header.
- **Item** (700, 14px): phase and task titles, stat values, HOME ledger titles.
- **Body** (400, 13px, 1.6): overview, comments, chat, descriptions.
- **Label** (400–700, 11px, .06–.14em tracking, uppercase): column heads, meta lines, action links, states.

### Named Rules
**The Flat Type Rule.** Text never glows. Emphasis comes from weight, size, case and role colour.

## Layout

The ledger page is a three-column desktop layout at 1920 width: a fixed outline on the left (280px), the main column of folding sections (max 1802px overall), and a sidebar of Stats and the swarm panel that scrolls on its own. Floating action buttons (sync, top, bell, home, chat) sit in the corners at 16px from the edge. Below 1280px the outline gives way to a Jump to menu. Below 1024px the icon rail becomes a bottom bar of seven equal slots with the chat bubble last, side panels open above it, and Stats becomes a drawer from the right: an inward arrow in the icon colour or a left swipe opens it, a right swipe, a tap outside or Escape closes it. Below 768px body and label text step up to 14px and 12px and row actions wrap under their text; HOME keeps its compact list up to 1100px. Touch screens get 44px controls, tabs, steppers and fold heads, 32px small controls and 16px text in inputs so the browser never zooms on focus, and every scrollbar is thin over a transparent track. HOME is a full-width hairline table on a fixed column grid: ledger, kind, overview, open, done, swarm, activity, actions. Spacing steps are 4, 8, 12, 16 and 32px; panels pad 16–18px, rows 6–12px.

**Scroll gutter (operator decision 2026-10-12).** Content never touches a scrollbar. The main column keeps `max(.5%, 12px)` and the Stats column `max(.5%, 8px)` of padding between its content and its scrollbar, on the Ledger and Swarm tabs alike.

### Swarm tab
- **Control bar.** Left: swarm name, state and tick. Right: the controls row (start, pause, stop, stop now, close ledger, autonomy modes, reset layout), and under it, right aligned, the Doctor row in this order: DOCTOR, last check, findings, start doctor or stop doctor (red), the on or off dot, the Doctor state (a link to its ledger when running). The Doctor row is always visible; with no Doctor it reads `last check —`, `findings —`, `not running`. There is no Doctor side panel.
- **Status line.** The line under the control bar shows failures only ("Could not … Try again or ask the master."). Progress notes such as "sending", "pending, waiting for the hive tick" or "acknowledged" never show, and there is no Commands log.
- **Capacity.** Two strips: the caps (eng, ci, plan, masters step, compact limit, effort min, effort max), then focus weight, Claude model, Codex model, master affinity and apply together on their own strip. Roles follow: every role beside its lane with its live agent count (engineer, frontend, qa, documentation under eng; cicd under ci; planner under plan; dispatcher and master under seats), plus any other profile a live agent of that lane runs. Gates follow. Strips and roles appear only once a status arrives. When swarm status cannot be read, one muted notice replaces them, or sits under the strips when they keep the last observed state; no empty input shows.
- **Panels.** Below Capacity, full width, in this order: Quota, Hives, Overlays, Agents, Handoff outcomes, Recovery, Freezes, Architecture. Each folds on its header and keeps a drag height grip; a folded panel shrinks to its header. Reset layout restores sizes; the order is fixed. The Quota panel has no capacity line under its table and lists only accounts read within 15 minutes of the newest reading; its header count names the accounts and the probe age, never a hive. The Hives panel lists only hives that beat within the last day, by name, with no per account column. The Architecture panel lists each design inventory proposal, marked approved or awaiting approval, with a reason field and approve while it awaits, and each earlier record revision with roll back; only the page credential's operator may use them. The Recovery panel holds one row per seat waiting on a recovery decision: its failure, state, checkpoint and watermark, diagnosis, wait and the native resume, context continuation and fresh start choices. Each checkpoint and diagnosis line ends in an ellipsis with its full text on hover, and a choice the seat cannot take stays visible and disabled with its reason as its title.

### Stats
Default order (operator choice 2026-10-12): Started, Elapsed, Time left, Phases, Tasks, Follow-ups, Frozen, Unfrozen, Open, Claimed, Blocked, In PR, Done today, Next phase, Out of scope, Inbox pending, Agents · 2 h, Last activity, then Top bottleneck last: its title carries the measured age (`Top bottleneck · 3m ago`) over a bullet list of stage, trend and last action, then seat wait and queue wait in task hours, each with its meaning, then, while any launch waits, one Admission refusals line counting the waiting launches by reason (cpu, ram, quota, placement, node cap, missing telemetry). Each row is a key and value on a fixed label track (104px), labels muted and values right aligned on one column, hairlines between rows. A value stays on one line; one that does not fit ends in an ellipsis and never covers its label. Every row, Top bottleneck included, carries a dim `≡` grip 16px in from the panel's left edge, the same margin the values keep on the right, 8px before its label: drag it, or focus it and press the up or down arrow, to reorder. The order is saved in the shared page layout (`/api/layout`, `stats`), so every browser shows it, and Reset layout clears it.

## Elevation & Depth

Flat. Depth is two translucent lifts over one canvas: `--surface-1` for panels and `--surface-2` for wells and inputs, never darker than what they sit on. Floating panels use `--overlay` with a 16px backdrop blur. Nothing casts a drop shadow and nothing glows. The only `box-shadow` allowed is an inset hairline used as an input underline or selected edge.

### Named Rules
**The Two Lifts Rule.** Every panel samples to one of two surface values. A third value is a component painting its own surface.

**The Hairline Row Rule (operator rule 2026-10-12, cannot be broken).** A record row (a priority, an item, a stat, a table or list row) is transparent over its panel and divided from its neighbour by a 1px --rule hairline only. It never takes a background, a border-radius, a border, a box-shadow or a sibling gap that turns it into a tile, card or bubble; emphasis comes from role colour and type. Only hover and focus may wash a row that is a link. tests/swarm_ledger/test_design_system.py fails on any painted row and proves itself against planted tiles.

## Shapes

Small, quiet radii: 4px for inline controls, checkboxes and code, 6px for panels, buttons and inputs, 8px for floating panels and HOME actions, 12px for the floating action buttons, 50% for status dots. Hairlines of 1px divide rows; a 2px red tick marks section heads.

## Components

### Buttons
- **Shape:** gently rounded (6px).
- **Ghost (default):** transparent, role-coloured label in 11px uppercase bold; no border.
- **Hover / Focus:** the hover lift (`--hover`) appears behind the label; `focus-visible` draws a 1px ring in the label's colour, offset 2px.
- **Destructive:** same shape with the destructive red label.
- **Disabled:** 35% opacity, `not-allowed` cursor.

### Action links
- **Style:** bare 11px uppercase muted text (`.link`, `.add`); hover turns it accent blue. Approve and deny carry green and red at rest.

### Floating action buttons
- **Style:** 44px square, 12px radius, transparent, red icon; the hover lift appears on hover. A red bare-number counter sits at the top right.

### Inputs / Fields
- **Inset:** `--surface-2` well, 6px radius, no border. Focus draws an accent inset underline.
- **Line:** transparent with an edge underline that turns accent on focus.
- **Checkbox:** 16px (24px on touch screens), 4px radius, surface-2; checked fills positive green with an ink tick.

### Panels / Sections
- **Corner Style:** 6px.
- **Background:** `--surface-1`.
- **Border:** none; hairlines divide items inside.
- **Head:** a fold summary in 12px uppercase tracked bold with a red 2px tick underline when open.

### Folding (operator rule 2026-10-12)
- Only four things fold, each remembering its choice per viewer: the Ledger tab's sections below Overview, the Swarm tab panels below Capacity, the outline categories (with Expand all / Collapse all) and comment dropdowns (with Show all / Hide all comments).
- Nothing else folds: Overview, Stats, the chat, artifacts, alerts and notifications windows, the Swarm control bar and Capacity, HOME and BIN, and inside a section the plan groups, phase rows, done groups and task contract and proof blocks. A fold anywhere else fails its test.
- A button inside a fold header (quota refresh, overlays apply) acts without folding its panel.

### Overview
- A muted 11px uppercase OVERVIEW label with a pen beside it. The pen opens an inset textarea with Save and Cancel; Ctrl+Enter saves, Esc cancels. Saving sends `overview_set`, an operator-only operation recorded as "overview changed".
- **The Overview Cap Rule.** The page shows at most the first 200 words of the overview, cut at a word boundary and ending with an ellipsis; nothing folds or expands, and the stored overview and the editor keep the full text.

### Tables
- **Columns resize (operator rule 2026-10-12, cannot be broken).** Every column in the front end resizes by dragging its header's right edge: every table on the ledger page and the HOME and BIN columns. An 8px grip shows a 1px `--grip` hairline on hover, drag or keyboard focus; arrow keys move it 16px (Shift ×4). Only columns resize this way. Widths are saved in the shared page layout (`/api/layout`, `columns` per table) beside the panel sizes, apply to every ledger page, and clear with Reset layout. A table the grips cannot reach fails its test.
- **Distribution.** Columns share the width in proportion (fixed layout on desktop) instead of one column taking everything and the rest crowding the right edge. Values sit left aligned under their heads.
- **Overflow.** A cell that does not fit ends in an ellipsis and shows its full text on hover; ids (`.sw-id`) ellipsize inside a narrowed column. Tables never scroll sideways at 1920.
- **Row actions** (end focus, unfreeze) use the row's own type: 13px, weight 400, on the row's baseline, never the 11px uppercase button style.

### Overlays
- One row per role, all 36px high, role names in a 12ch column and overlay names or "no overlay in the bundle wears this role" in one text column.
- Apply sits in the panel header after the count and appears only when an overlay change is pending.

### Freeze marks
- A snowflake marks a frozen or held item. On a plan row it sits inside the header before the title; it never floats outside its panel.

### Outline marks
- An outline entry with a pending notification carries a `+` placed against its own link, section headers included.

### Labels and states
- **Style:** bare uppercase text in its role colour, optionally led by a 6px status dot. No chip, no capsule, no glow.

### Tooltips
- **Delay (operator decision 2026-10-07, locked):** every tooltip on ledger pages, HOME and BIN opens after one second of hover, not two. `DELAY` in `tooltips.js` is 1000 ms, and a test fails on any other value. Native `title` attributes follow the browser's own delay, so new hover text goes through `tooltips.js`.

### Navigation
- **Outline:** a fixed left list of section links, muted at rest, white on hover with the hover lift, accent when current. Items lead with a red or green bullet for open or done.
- **Outline filter:** a line input and a state select above the list filter it by title, id and state; done items not yet loaded are searched on the server. Swarm tasks sit under muted uppercase phase labels that never fold.
- **Fresh defaults:** an outline category or Ledger section whose every item is done or answered starts closed for a viewer with no saved choice.
- **Keyboard:** ArrowUp and ArrowDown, or j and k outside form controls, move focus through the visible outline links or task ids; Enter reveals the target.

## Do's and Don'ts

### Do:
- **Do** take every colour from a role token in `palette.css`.
- **Do** carry status with role colour and an optional status dot.
- **Do** keep controls frameless at rest and show the hover lift on hover and focus.
- **Do** fold only Ledger sections, Swarm panels below Capacity, outline categories and comments, remembering each choice per viewer.
- **Do** give every table column a resize grip whose width is saved in the shared layout.
- **Do** keep a gutter between scrolling content and its scrollbar.
- **Do** keep a visible `focus-visible` ring on every control.

### Don't:
- **Don't** use `text-shadow`, `drop-shadow` or halo `box-shadow` on anything.
- **Don't** paint a blue or other tinted wash behind the canvas.
- **Don't** wrap labels in chips, badges or pills.
- **Don't** add a third surface level; reach for a hairline instead.
- **Don't** paint a record row as a tile, card or bubble: no row background, radius, border, shadow or gap between rows. Hairlines only.
- **Don't** write a colour literal outside `palette.css`.
- **Don't** show progress or acknowledgement chatter under the Swarm control bar; failures only.
- **Don't** let one table column take the width while the rest crowd the right edge.
- **Don't** add a fold to Overview, Stats, side windows, the control bar, Capacity, HOME or BIN.
