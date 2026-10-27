---
name: diagram-design
description: "Generates editorial-quality diagrams as self-contained HTML+SVG files. Covers architecture, flow/process, data/ER, and all other diagram types. Follows the cathrynlavery/diagram-design framework: deliberate visual restraint, 4px grid, one accent color, WCAG AA contrast, zero external dependencies. Trigger phrases: 'create diagram', 'draw diagram', 'generate diagram', 'architecture diagram', 'flow diagram', 'ER diagram', 'system diagram', 'visualize', 'diagram this'."
lifecycle: experimental
---

# Diagram Design

Produces a single self-contained HTML file containing an inline SVG diagram. No build steps, no JavaScript dependencies, no external assets (Google Fonts via `@import` is the only allowed exception).

**Core principle:** *The highest-quality move is usually deletion.* Every element must earn its place. Target density: 4/10.

---

## Design System

### Color

- **One accent color** used on 1–2 focal elements only. Default: `#2563eb` (blue). Match the user's brand if they provide a URL or hex.
- Background: `#ffffff`. Stroke/text: `#111827`. Muted surfaces: `#f9fafb`. Muted text: `#6b7280`.
- WCAG AA contrast required on all text: verify mentally before writing.

### Typography

| Role | Font | Size | Weight |
|------|------|------|--------|
| Diagram title / emphasis | Instrument Serif | 18–22px | 400 |
| Node labels, body text | Geist (sans) | 12–14px | 400–500 |
| Code values, IDs, technical | Geist Mono | 11–13px | 400 |

Load via single `@import` in `<style>`:
```css
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500&family=Geist+Mono&display=swap');
```

### Geometry

- **4px grid**: all x/y coordinates must be multiples of 4.
- Stroke width: 1px hairlines only. No drop shadows (`filter: drop-shadow` is banned).
- Border radius: max 10px (`rx`/`ry` on SVG `<rect>`).
- Arrow markers: simple open arrowhead, 1px stroke, no fill.
- Padding inside nodes: 12px horizontal, 8px vertical (multiples of 4).

---

## Output Format

Write a single `.html` file with this structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{Diagram Title}</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500&family=Geist+Mono&display=swap');
    body { margin: 0; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #f3f4f6; }
    figure { margin: 0; background: #ffffff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    figcaption { font-family: 'Geist', sans-serif; font-size: 12px; color: #6b7280; margin-top: 12px; text-align: center; }
    @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <figure>
    <svg role="img" aria-labelledby="diag-title diag-desc"
         width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
      <title id="diag-title">{Diagram Title}</title>
      <desc id="diag-desc">{One-sentence description}</desc>
      <defs>
        <!-- arrowhead marker -->
        <marker id="arrow" markerWidth="8" markerHeight="6" refX="6" refY="3" orient="auto">
          <path d="M0,0 L8,3 L0,6" fill="none" stroke="#111827" stroke-width="1"/>
        </marker>
      </defs>

      <!-- diagram content here -->

    </svg>
    <figcaption>{Caption or empty}</figcaption>
  </figure>
</body>
</html>
```

---

## Workflow

**Step 1 — Understand the request.**
- What is being shown? (system, process, data model, hierarchy, sequence, …)
- Who is the audience? (engineers, executives, clients)
- Does the user have a brand color or URL? Extract dominant color if yes.

**Step 2 — Sketch the layout mentally.**
- Choose the fewest nodes/elements that convey the full idea.
- Delete anything decorative. Delete anything the viewer can infer.
- Pick a reading direction: left→right for flows/sequences, top→down for hierarchies.

**Step 3 — Compute the SVG canvas.**
- Size to content, not the other way around. Start with a rough node count:
  - Simple (1–6 nodes): ~600×400
  - Medium (7–15 nodes): ~900×600
  - Complex (16+ nodes): ~1200×800
- All coordinates snap to 4px grid.

**Step 4 — Write the file.**
- One `.html` file, output path chosen to match the user's project (ask if unclear).
- Use `<g>` groups with `id` attributes for logical sections.
- Nodes: `<rect>` + `<text>`, never `<foreignObject>`.
- Text: `dominant-baseline="middle"` + `text-anchor="middle"` for centered labels.
- Lines/edges: `<line>` or `<path>` with `marker-end="url(#arrow)"`.

**Step 5 — Self-check before delivering.**
- [ ] All coordinates divisible by 4?
- [ ] Accent color used on ≤ 2 elements?
- [ ] No shadows, no fills on arrows?
- [ ] `<title>` and `<desc>` present on `<svg>`?
- [ ] File is self-contained (no `src=`, `href=` pointing to local paths)?
- [ ] WCAG AA contrast on all text?

---

## Diagram Type Patterns

### Flow / Process
Reading direction: left→right or top→down. Nodes: rounded rectangles (`rx="8"`). Decision: diamond (`<polygon>`). Use swimlanes (horizontal `<rect>` bands with 10% opacity fill) only when actors are essential to understanding.

### Architecture / System
Group components into bounded contexts with a light-fill `<rect>` background and a small label in the top-left corner. Use dashed borders (`stroke-dasharray="4 4"`) for external systems. Solid borders for owned services.

### Data / ER
Each table: `<rect>` header block (accent fill, white text) + rows below (alternating `#f9fafb` / `#ffffff`). Relationship lines: crow's foot notation using `<path>` endpoints. PK fields in `Geist Mono`, bold.

### Sequence
Lifelines: vertical `<line>` with participant box at top. Messages: horizontal `<line>` with arrow, label above in `Geist` 12px. Activation boxes: narrow `<rect>` on the lifeline.

### Hierarchy / Org
Tree layout, top→down. Parent→child: straight vertical lines connecting node centers. Use consistent row heights (multiples of 4) for clean alignment.

---

## Importing Existing Diagrams

If the user provides a draw.io XML, Mermaid definition, or Excalidraw JSON, redraw it in the HTML+SVG format above. Apply the design system on top — do not copy the original styling.

Adjustable parameters the user can request:
- **detail**: minimal | standard | full (controls how many labels/annotations)
- **audience**: technical | executive | client
- **size**: compact | standard | poster
- **accent**: any hex color

---

## Notes

- Never output PNG or SVG as a bare file — always wrap in the HTML shell so it previews in a browser.
- If the diagram would exceed ~25 nodes, offer to split it into sub-diagrams rather than cramming everything in.
- When unsure of layout, ask one clarifying question before writing code.
