---
version: 1
slug: "web-src-app-jsx"
primary_target: "web/src/App.jsx"
related_targets: ["web/src/styles.css"]
---

# Web view: graph + answer workspace

Mode: Operate. Audience: engineers asking how unfamiliar code works and checking the answer against the graph.
Task: choose a repo, ask, read the answer, verify it against the traced symbols. States: loading, nothing indexed, tracing, answered, nothing cited, unconnected symbol, API error, quota refused.
Must stay: the repo scope, safe answer rendering (no images, inert links), the graph as the main view. Themes are a user requirement: Black/Blue/Cyan, Blue/White/Pink, and more, switchable live.

## Direction contract

THESIS: every answer is drilled through the code and read as a core sample, symbols stacked by depth, beside the graph they came from. Refuses the category default of a grey graph plus a chat box, with citations as chips.

OWN-WORLD: tokens only, six switchable palettes; default near-black ground, blue structure, cyan for the live path. Hatched strata bands whose pattern encodes symbol kind, a depth numeral per band, tabular numerics, one workhorse sans with mono for code. Standard web controls throughout.

STORY: the visitor sees the whole repo as a core of its symbol kinds, asks, watches the core drilled, then checks each band against the graph, and sees what the answer spent.

FIRST VIEWPORT: three working columns at 1440: graph (fluid) with symbol and edge counts, core log (360px), chat (400px) with repo and theme controls in its header and the input docked at its foot.

FORM: Core Log, candidate 6 of the grounded list, seed:0ee881f0cc7bbb6c. Raises: rank comes from one system only (from Acetate Manual); numbers in tight tabular columns (from Data Field); each answer states what it spent, right-aligned (from Mixtape J-Card). Signature move: the drill, a line growing down the depth column as bands land, mirrored as the lit path in the graph. Motion only conveys that state.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
