---
name: agentihooks ledger
description: The operator's console for agentihooks swarms; ledger pages, HOME and BIN.
colors:
  canvas-ink: "#010104"
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
  surface-1: "rgba(255, 255, 255, .045)"
  surface-2: "rgba(255, 255, 255, .06)"
  hover-lift: "rgba(255, 255, 255, .08)"
  edge: "rgba(255, 255, 255, .14)"
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
---

# Design System: agentihooks ledger

## Overview

**Creative North Star: "The Operator's Console"**

A dark, dense control surface for one operator watching many agents. The page is a record first: hairline-divided rows of monospaced text on a near-black canvas, with colour reserved for state. Structure follows the operator's design system 2026-001: bare labels instead of chips, frameless controls that show a surface only on hover, depth from two translucent lifts over a single canvas, and every colour in one palette file.

Two operator decisions override 2026-001 here. First, **no glow**: no `text-shadow`, `drop-shadow` or halo `box-shadow` on text, icons or controls. Role colour, weight and size carry emphasis. Second, the canvas is near black (#010104) with no blue radial wash behind it.

Density is high and calm. A ledger page holds dozens of panels and hundreds of rows and must stay readable after hours open.

**Key Characteristics:**
- Monospace everywhere, five sizes (22 / 14 / 13 / 12 / 11 px), uppercase tracked labels for section and column heads.
- Near-black canvas, two white lifts (4.5% and 6%) for panels and wells, hairlines (6%) between rows.
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
- **Canvas Ink** (`--canvas` → `--ink-950`, #010104): the page ground, the only opaque surface.
- **Panel Navy** (`--panel`, `--overlay`): floating panels (chat, notifications) under a backdrop blur.
- **Text White** (`--text`): primary text, values.
- **Secondary Text** (`--text-2`): body copy in lists, proof values.
- **Muted Slate** (`--muted`, `--dim`): labels, column heads, timestamps, idle states.
- **Surface 1 / Surface 2 / Hover / Rule / Edge**: white lifts at 4.5%, 6%, 8%, 6% and 14%.

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

The ledger page is a three-column desktop layout at 1920 width: a fixed outline on the left (280px), the main column of folding sections (max 1802px overall), and a sidebar of Stats and the swarm panel that scrolls on its own. Floating action buttons (sync, top, bell, home, chat) sit in the corners at 16px from the edge. Breakpoints at 1599, 1279, 1023 and 600px drop the sidebar and outline into the flow. HOME is a full-width hairline table on a fixed column grid: ledger, kind, overview, open, done, swarm, activity, actions. Spacing steps are 4, 8, 12, 16 and 32px; panels pad 16–18px, rows 6–12px.

## Elevation & Depth

Flat. Depth is two translucent lifts over one canvas: `--surface-1` for panels and `--surface-2` for wells and inputs, never darker than what they sit on. Floating panels use `--overlay` with a 16px backdrop blur. Nothing casts a drop shadow and nothing glows. The only `box-shadow` allowed is an inset hairline used as an input underline or selected edge.

### Named Rules
**The Two Lifts Rule.** Every panel samples to one of two surface values. A third value is a component painting its own surface.

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
- **Checkbox:** 16px, 4px radius, surface-2; checked fills positive green with an ink tick.

### Panels / Sections
- **Corner Style:** 6px.
- **Background:** `--surface-1`.
- **Border:** none; hairlines divide items inside.
- **Head:** a fold summary in 12px uppercase tracked bold with a red 2px tick underline when open.

### Labels and states
- **Style:** bare uppercase text in its role colour, optionally led by a 6px status dot. No chip, no capsule, no glow.

### Tooltips
- **Delay (operator decision 2026-10-07, locked):** every tooltip on ledger pages, HOME and BIN opens after one second of hover, not two. `DELAY` in `tooltips.js` is 1000 ms, and a test fails on any other value. Native `title` attributes follow the browser's own delay, so new hover text goes through `tooltips.js`.
### Navigation
- **Outline:** a fixed left list of section links, muted at rest, white on hover with the hover lift, accent when current. Items lead with a red or green bullet for open or done.

## Do's and Don'ts

### Do:
- **Do** take every colour from a role token in `palette.css`.
- **Do** carry status with role colour and an optional status dot.
- **Do** keep controls frameless at rest and show the hover lift on hover and focus.
- **Do** keep every section foldable, remembering its state per viewer.
- **Do** keep a visible `focus-visible` ring on every control.

### Don't:
- **Don't** use `text-shadow`, `drop-shadow` or halo `box-shadow` on anything.
- **Don't** paint a blue or other tinted wash behind the canvas.
- **Don't** wrap labels in chips, badges or pills.
- **Don't** add a third surface level; reach for a hairline instead.
- **Don't** write a colour literal outside `palette.css`.
