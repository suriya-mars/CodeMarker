---
name: RepoDigger
description: Every answer drilled through the code and read as a core sample, beside the graph it came from.
colors:
  bg: "#050a12"
  s1: "#08121f"
  s2: "#0a1626"
  raised: "#0e1f33"
  line: "#1a3550"
  text: "#d7e6f2"
  muted: "#8fb0c8"
  accent: "#1069e0"
  accent-hover: "#0d5cc7"
  on-accent: "#ffffff"
  live: "#35d6e6"
  via: "#7fa8cc"
  code: "#5fe3f0"
  code-bg: "#12283f"
  pre-bg: "#030710"
  pre-text: "#d7e6f2"
  danger-bg: "#3a1216"
  danger: "#ffb4b4"
  warn: "#ffd27a"
  warn-bg: "#3a2a0c"
  edge: "#27486a"
  edge-soft: "#16293f"
  dot: "#10233a"
  k-file: "#6f9bff"
  k-class: "#d8f1ff"
  k-function: "#9db7d6"
  k-method: "#2f66ad"
  k-page: "#b9c8ff"
  k-interface: "#4fa8c9"
typography:
  title:
    fontFamily: "Hanken Grotesk Variable, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.5
  section:
    fontFamily: "Hanken Grotesk Variable, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.5
  body:
    fontFamily: "Hanken Grotesk Variable, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  summary:
    fontFamily: "Hanken Grotesk Variable, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "Hanken Grotesk Variable, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  symbol:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.5
  code:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.5
  numeral:
    fontFamily: "JetBrains Mono Variable, ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.5
    fontFeature: "tnum"
rounded:
  strip: "2px"
  band: "3px"
  pre: "5px"
  control: "6px"
  popover: "8px"
  dot: "50%"
spacing:
  strata-gap: "3px"
  band-gap: "6px"
  band-pad: "10px"
  form-pad: "12px"
  panel-pad: "16px"
  log-pad-block: "18px"
  graph-dot-grid: "28px"
  column-log: "360px"
  column-chat: "400px"
components:
  button-ask:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-ask-hover:
    backgroundColor: "{colors.accent-hover}"
  control:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "5px 9px"
  input-question:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  link-action:
    textColor: "{colors.code}"
    typography: "{typography.summary}"
  depth-numeral:
    backgroundColor: "{colors.s1}"
    textColor: "{colors.live}"
    typography: "{typography.numeral}"
    rounded: "{rounded.band}"
  core-band:
    textColor: "{colors.text}"
    rounded: "{rounded.band}"
    padding: "10px 10px 10px 8px"
  flag:
    backgroundColor: "{colors.warn-bg}"
    textColor: "{colors.warn}"
    rounded: "{rounded.band}"
    padding: "0 5px"
  message-answer:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "12px"
  message-error:
    backgroundColor: "{colors.danger-bg}"
    textColor: "{colors.danger}"
    rounded: "{rounded.control}"
    padding: "12px"
---

# Design System: RepoDigger

## Overview

**Creative North Star: "The Core Sample"**

The workspace reads like a drilling log. A repository is a core of its symbol kinds; a question drills it; the answer comes back as strata stacked by depth, with the same route lit in the graph beside it. Every band can be checked against the graph, and the log states what the answer spent.

It is a dense working tool, not a presentation: three columns held in the viewport, a type ramp that stops at 15px, flat tonal surfaces, and standard web controls. The identity lives in the hatched strata, the depth numerals and the drill line, and it survives a complete change of palette because a theme is only colours.

Motion carries state and nothing else. Bands land, the drill line grows between them, and the camera moves to what was picked. While a trace runs, the line bores on a loop beside an elapsed count, because its length is unknown and the interface does not pretend otherwise.

**Key Characteristics:**
- Six switchable palettes over one structure; Deep Core is the default.
- Hatch patterns encode symbol kind, alongside the kind written in words.
- Depth numerals, counts and spend in tabular figures.
- One workhorse sans, with mono for code and numbers.
- Flat tonal layering, one soft shadow, small corners.
- Answers rendered as untrusted content.

## Colors

A near-black blue ground with blue structure and one bright signal colour for the live path, re-cast by each theme through the same token names.

The frontmatter records Deep Core, the default. All six themes are complete blocks of the same tokens in `web/src/styles.css`, mirrored in `.impeccable/design.json` under `extensions.themes`.

| Theme | Scheme | Character | Ground | Accent | Live |
|---|---|---|---|---|---|
| Deep Core | dark | Black, blue, cyan | #050a12 | #1069e0 | #35d6e6 |
| Blossom | light | Blue, white, light pink | #ffffff | #1747c8 | #1747c8 |
| Cobalt | dark | Saturated blue, white, ice | #0a2472 | #ffffff | #7df3ff |
| Aurora | dark | Black, emerald, violet | #06110f | #0b7f69 | #c58bff |
| Ember | dark | Charcoal, rust, amber | #100b09 | #c2410c | #ffb454 |
| Daylight | light | White, blue, teal | #f6f9fc | #0b62d6 | #007a8a |

### Primary
- **Bore Blue** (accent, accent-hover, on-accent): the Ask button and text selection. It is the one filled action on screen.

### Secondary
- **Live Cyan** (live): the drill line, depth numerals, lit graph nodes and edges, the picked overlay, the selected band's border, the log heading, the current theme's ring and the focus ring.
- **Route Steel** (via): the border of a graph node on the route but not cited, so a cited symbol and a connecting one stay distinct.
- **Code Cyan** (code, code-bg): inline code, the full symbol id and text-link actions. It is a close neighbour of Live in the dark themes and identical to it in Blossom, where Live is told apart by what it draws (lines, borders, numerals) rather than by hue.

### Tertiary
- **Kind colours** (k-file, k-class, k-function, k-method, k-page, k-interface): one per symbol kind, filling graph nodes and legend dots and generating the hatch tones. Hatch and tint tones are mixed from them against the column surface (55%, 12% and 7%; 26% and 9% in the quieter repository core), never set by hand.

### Neutral
- **Core Black** (bg): the graph ground.
- **Log and Panel Surfaces** (s1, s2): the core log column and the questions column.
- **Raised Slate** (raised): answers, inputs, controls, symbol detail, the theme popover.
- **Strata Line** (line): every column divider, control border and table rule.
- **Ice Text and Muted Steel** (text, muted): primary text and all secondary text, placeholders included.
- **Graph Quiet Tones** (edge, edge-soft, dot): dependency edges, the fainter CONTAINS edges, and the 28px dot grid.
- **Code Block** (pre-bg, pre-text): fenced code in answers. It stays dark in the light themes.
- **Status** (warn, warn-bg, danger, danger-bg): the Flagged label and error messages. Used only for those states.

### Named Rules
**The One Block Rule.** A theme is one block of tokens and nothing else. No theme may change layout, type, shape or motion, and no component may branch on which theme is active. A new theme defines every token; its swatches go in the picker list in `web/src/themes.js`, the one place colour literals repeat, because the picker shows all themes at once.

**The Live Path Rule.** Live marks the answer's traced path and is never assigned to a symbol kind: no `k-*` token equals `live` in any theme. In the graph only lit nodes, lit edges and the picked overlay wear it. Its other uses (the log heading, selected states, the focus ring) are the complete list; give it no new meaning.

**The Canvas Rule.** The graph is canvas and cannot read CSS variables, so `readTokens()` hands it the active values on every theme change. Never hard-code a colour into a graph style.

Known gap, not a target: some kind colours sit under 3:1 against the ground (Blossom function and class; Daylight method, interface and page), and those nodes rely on their labels. Do not treat these values as the floor when writing a new theme.

## Typography

**Body Font:** Hanken Grotesk Variable (with system-ui, -apple-system, Segoe UI, sans-serif)
**Label/Mono Font:** JetBrains Mono Variable (with ui-monospace, SFMono-Regular, Menlo, monospace)

**Character:** A plain, slightly warm grotesque does all the reading work, and a mono marks anything that came from the code or was counted. There is no display face, because nothing here is display.

### Hierarchy
- **Title** (700, 15px, 1.5): the product name in the questions header, the largest type on screen.
- **Section** (700, 13px, 1.5): log headings, in Live.
- **Body** (400, 14px, 1.5): questions, answers, the base size. User questions step up to 600 rather than up in size.
- **Summary** (400, 12.5px, 1.4): the LLM's one-line symbol summaries, and text-link actions, in Muted.
- **Label** (400, 12px, 1.5): relations, kind tags, paths, facts and provenance lines. Kind beside a band title drops to 11.5px; the Flagged label is 11px at 600.
- **Symbol** (mono, 600, 13px): symbol names in band titles and the full id in the detail view.
- **Code** (mono, 12.5px): inline code in answers; 12px in summaries and fenced blocks.
- **Numeral** (mono, 600, 13px, tabular): depth numerals. Repository counts and spend figures use the same mono tabular setting.

### Named Rules
**The Tight Ramp Rule.** Everything sits between 11px and 15px; rank comes from weight and colour inside that band. Headings inside an answer are held to 14px, so an answer never outranks the workspace around it.

**The Tabular Rule.** Any number a reader compares (counts, depth, tokens, cost, time) uses tabular figures, and spend sits right-aligned in its column.

**The Self-Hosted Face Rule.** Both families ship through `@fontsource-variable` and load from the app's own origin. A hosted font is a third-party request, and the product promises none. Never add a font CDN link, even for a preview.

## Layout

Three working columns fill the viewport at desktop: the graph (fluid), the core log (360px) and questions (400px), with no page scroll. Each column scrolls itself, and the question input stays docked at the foot of its column. Repository and theme controls live in the questions header; symbol and edge counts float over the top of the graph with the kind legend.

The graph leads. Everything else takes a fixed width and gives the graph the remainder.

Density is tight and the rhythm is small: 3px between repository strata, 6px between answer bands (plus 12px between separate samples), 16px panel gutters.

At 1180px and below, the graph holds the left column and the core log moves under questions at 40% of the height. At 760px and below everything stacks in reading order: graph at 50vh with its counts in a static bar above it, questions, then the log; the page scrolls and the question form sticks to the bottom of the viewport.

## Elevation & Depth

Flat by default. Depth is tonal, stepping from the ground through the two column surfaces to raised, with a 1px line at each column edge. Geological depth is told by the depth column and the numerals, not by material.

### Shadow Vocabulary
- **Popover** (`box-shadow: 0 10px 28px rgba(0, 0, 0, 0.28)`): the theme list, the only thing that floats over content.

### Named Rules
**The One Lift Rule.** Only an overlay that covers content gets a shadow. Bands, answers, panels and buttons stay flat at rest and on hover; their states change border or fill.

## Shapes

Small, nearly square corners keep the strata reading as cut samples: 2px on repository strips, 3px on bands, numerals and inline code, 5px on code blocks, 6px on controls, inputs and messages, 8px on the popover. Circles are reserved for what is a point: graph nodes, legend dots and theme swatches.

Borders are 1px. A band's border is transparent at rest so a state change never shifts the layout.

## Components

### Hatch Patterns
The system's signature material. Each symbol kind owns one tile, and the tile plus its colour say the kind at a glance.

- **file:** horizontal stripes, 6px tile, even.
- **page:** horizontal stripes, 8px tile, heavy.
- **class:** vertical stripes, 8px tile, even.
- **interface:** vertical stripes, 8px tile, thin.
- **function:** dot grid, 8px tile.
- **method:** line grid, 6px tile.

A new kind needs a `k-*` token in all six themes, a tile, a legend entry and a line in `readTokens()`. `proc` and `table` symbols exist in the graph model and have none of these yet.

**The Pixel Tile Rule.** Hatches use pixel-aligned tiles only: stripes, dot grids and line grids whose stops land on whole pixels, because these stay seamless at any band height. Diagonal gradients showed seams and were dropped.

**The Beside The Text Rule.** A pattern behind text fails contrast, so the pattern sits in a column beside the text, as on a real log. In an answer band that is a 42px column, with the text on a calm tint (7% of the kind) that holds at least 9.4:1 for primary and 5.1:1 for muted text in every theme. Anything set on a pattern itself, such as a depth numeral or a repository label, carries its own solid surface patch.

### Core Band
One cited or traversed symbol, drawn as a full-width button.
- **Structure:** a 42px hatch column holding the depth numeral, then the symbol name, its kind at the right, how the path reached it, and the summary.
- **Honest labels:** a band says "On the route, not cited" when it was not cited, and a symbol with no route to the others becomes its own sample and says so, rather than being bridged.
- **States:** transparent border at rest, Strata Line on hover, Live when selected, and the global focus ring.
- **Provenance:** the log states that levels and routes come from parsed code and that summaries are written by the LLM, so the two stay distinguishable.

### The Drill
**The Drill Rule.** One 2px Live line grows down the depth column as bands land, stopping at each sample's end, and the same route lights in the graph. Bands land over 260ms with a 70ms stagger, and the line segment follows 60ms after its band. During a trace the line bores down the repository core on a 2.4s loop beside an elapsed count, and it must never read as a progress bar.

### Repository Core
The idle state of the log: one strip per symbol kind, its thickness in proportion to the kind's count, a label and a tabular count at the foot of each on solid patches. It uses quieter hatch tones than answer bands because it fills the column and the graph must still lead.

### Graph
- **Nodes:** filled by kind, sized by degree (12px to 40px). Minor symbols only label once zoomed in.
- **Edges:** 1px in the edge tone with small arrows; CONTAINS is 0.5px, softer, with no arrow, because it is structure rather than dependency.
- **Lit path:** the rest dims to 14%. Cited nodes take a 3px Live border, route nodes a 2px Via border, and lit edges go 3px Live. Labels on the path use a halo in the ground colour rather than a solid box, which would cut the line where it crosses.

### Buttons
- **Ask:** the one filled button, Bore Blue, semibold, 6px corners. Darkens on hover in 150ms; 55% opacity when disabled.
- **Control:** raised fill with a 1px line, 13px type, for the repository select and theme button. The border moves to Muted on hover.
- **Text action:** underlined with a 3px offset in Code Cyan, for Whole repo, Close and Show this path.

### Inputs / Fields
- **Style:** raised fill, 1px line, 6px corners, 10px by 12px padding, Muted placeholder at full opacity.
- **Focus:** the global 2px Live ring at a 2px offset.
- **Disabled:** the input and Ask lock together while a question runs.

### Navigation
There is no navigation; the workspace is one view. The picker is the only chrome menu.

### Theme Picker
A control showing the current theme's three overlapping swatches opens a 248px popover of native radio inputs, each with swatches, name and a plain colour note. The current option has a 1px inset Live ring; hovering fills with the panel surface. Escape or an outside click closes it. The choice is remembered when storage allows; if it is blocked, Deep Core applies and nothing breaks.

### Answers and Messages
**The Untrusted Answer Rule.** Answers quote repository text, so an answer is untrusted content. Images are dropped so nothing fetches a URL by being displayed, links render as plain text showing where they point, and raw HTML stays inert. Summaries in the log get even less: plain text with backticks read as code.

- **Question:** body at 600, no container.
- **Answer:** raised panel, 12px padding, 6px corners. Fenced code scrolls within its own block.
- **Error:** the same panel in the danger pair.
- **Flagged:** a small warn label ahead of the summary, a label and never a block.

### Spend Readout
Tokens, estimated cost and time under each answer, as a ruled list with labels at the left and mono tabular figures at the right. Cost is labelled as an estimate.

## Do's and Don'ts

### Do:
- **Do** define every token when adding a theme, and add its swatches to the picker list.
- **Do** keep hatch stops on whole pixels, using stripes, dot grids or line grids.
- **Do** set text beside a pattern, or on a solid surface patch when it must sit over one.
- **Do** mix hatch and tint tones from the kind token with `color-mix`, so every theme inherits them.
- **Do** write the kind in words next to its pattern and colour.
- **Do** use tabular figures for any number a reader compares.
- **Do** stop all CSS motion under `prefers-reduced-motion`.
- **Do** load fonts through `@fontsource-variable` from the app's own origin.

### Don't:
- **Don't** use a diagonal gradient for a hatch; diagonal tiles showed seams.
- **Don't** set text directly on a hatch; a pattern behind text fails contrast.
- **Don't** give a symbol kind the Live colour, in any theme.
- **Don't** let a theme change anything except token values.
- **Don't** render images or live links from an answer; show a link as plain text with its target.
- **Don't** load a font, or anything else, from a third-party host.
- **Don't** add a shadow to anything that does not float over content.
- **Don't** show the trace as a progress bar or percentage; its length is unknown.
- **Don't** add motion that does not report a change of state.
