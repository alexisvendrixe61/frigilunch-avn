# Frigilunch production planner

Weekly rolling scheduler for Frigilunch: assigns orders to production days and
lines, builds exact per-day timelines, exports Excel + HTML dashboard + CSV action list,
and commits a 4-week rolling freeze to `plan_state.json`.

## How to run

```bash
python -m frigilunch.cli --week 40 --year 2026
```

| Flag                  | Effect                                                   |
| --------------------- | -------------------------------------------------------- |
| `--week N --year Y` | Plan week N of year Y (ISO week)                         |
| `--reset`           | Ignore existing `plan_state.json`, replan from scratch |
| `--dry-run`         | Solve and export but do not write `plan_state.json`    |
| `--suffix LABEL`    | Append label to output filenames                         |

## How to test

```bash
pytest tests/unit/ -q                                # unit tests only (fast)
pytest tests/unit/ --cov=frigilunch --cov-report=term-missing   # with coverage
pytest tests/                                         # all tests including integration
```

## Layout

```
frigilunch/          new top-level package
  domain/            pure domain logic — no I/O, no OR-Tools imports
    models.py        Order, Product, DaySchedule, PlanScore, SolveOutcome
    constraints/     changeover, device, sequencing, allergen rules
    oracle.py        schedule_day() — deterministic, pure
    benders.py       feedback loop (oracle + cp_solver injected)
    state.py         rolling freeze logic
  application/
    pipeline.py      STAGES tuple, run_pipeline()
    run_week.py      assembles adapters and calls pipeline
  adapters/          I/O adapters — call inward only
  cli.py             argparse entry point
tests/
  unit/              fast, no I/O
  integration/       smoke tests with synthetic fixtures
src/                 legacy implementation (reference only)
```
