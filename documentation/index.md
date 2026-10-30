# Frigilunch production planner — documentation

Frigilunch is a weekly rolling production planner for a food manufacturing facility. It reads incoming orders and product master data, assigns each order to a production day and line (L1 or L2), builds exact minute-by-minute daily timelines, exports an Excel plan and HTML capacity dashboard, and commits the next four weeks to a rolling freeze so confirmed production cannot be moved by a later run.

## Documents

| Document | Audience | Description |
|---|---|---|
| [getting-started.md](getting-started.md) | Anyone setting up or running the tool | Prerequisites, input files, CLI flags, output files, common mistakes |
| [how-it-works.md](how-it-works.md) | Planners and managers | Plain-language walkthrough of the nine pipeline stages and the feedback loop |
| [architecture.md](architecture.md) | Developers | Package layers, data flow, optimizer algorithm, day builder, rolling freeze |
| [constraints.md](constraints.md) | Developers and planners | All 44 planning rules — which are enforced, where, and which are not |
| [glossary.md](glossary.md) | Anyone | Dutch and domain-specific terms used throughout the code and documents |
| [decisions/001-benders-loop.md](decisions/001-benders-loop.md) | Developers | Why the optimizer uses a feedback loop instead of a single CP-SAT model |
| [decisions/002-rolling-freeze.md](decisions/002-rolling-freeze.md) | Developers | Why each run commits and locks the next four weeks |
| [decisions/003-package-layout.md](decisions/003-package-layout.md) | Developers | Why the package is named `frigilunch` and sits at the repo root |
| [reports/](reports/) | Anyone | Generated HTML reports (explainability, capacity analysis) |
