# Frigilunch production planner

Weekly rolling scheduler: assigns orders to production days and lines,
builds exact per-day timelines, exports Excel + HTML dashboard + CSV action list,
and commits a 4-week rolling freeze.

## Quick start

```bash
pip install -r requirements.txt
python -m frigilunch.cli --week 40 --year 2026
```

## Documentation

Full documentation: [documentation/index.md](documentation/index.md)

| Doc | What it covers |
|---|---|
| [Getting started](documentation/getting-started.md) | Setup, input files, all CLI flags, output files |
| [How it works](documentation/how-it-works.md) | Plain-language walkthrough for planners and managers |
| [Architecture](documentation/architecture.md) | Package layers, optimizer algorithm, rolling freeze |
| [Constraints](documentation/constraints.md) | All 44 planning rules and where they are enforced |
| [Glossary](documentation/glossary.md) | Dutch and domain terms used in the code |
