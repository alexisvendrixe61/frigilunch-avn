# Getting started

Frigilunch turns two planning spreadsheets into a production plan: a day, a
line (L1 or L2) and a time slot for every order. You run it once a week; each
run freezes the next 4 weeks and leaves everything after that provisional.

This page is the short version. The details live in:

| Doc | For |
|---|---|
| [HOW_IT_WORKS.md](HOW_IT_WORKS.md) | A plain-language walkthrough of the whole flow, high level then detailed — no code required to follow it |
| [README.md](README.md) | Every flag, the inputs and outputs, what each rejection cause means |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the optimizer and the day builder work together |
| [scheduler.md](scheduler.md) | A run step by step, the solver phases, the warm-up |
| [docs/constraints.md](docs/constraints.md) | Every planning rule and where it is enforced (generated, don't edit) |

## Setup

On Windows, use `py` instead of `python`.

```bash
py -m pip install -r requirements.txt
```

Put the two source spreadsheets in `data/source_data/`:

- `orderverwerking en planning.xlsm`: the orders (`orderverwerking` tab)
- `Planning info.xlsx`: the product database

Both are re-read on every run. You never edit the JSON in `data/generated/`.
Solver tuning (`FRIGI_*` variables) can go in `.env`; it's optional.

## Running it

```bash
# Normal weekly run: plan from the Monday of week 36 and commit the next 4 weeks
py -m frigilunch.cli --week 36 --year 2026

# Start from a clean slate (ignore existing plan_state.json)
py -m frigilunch.cli --week 36 --year 2026 --reset
```

A run takes minutes, not seconds: the solver works against a time budget, so
two runs on the same input can differ slightly.

**Watch out:** every run writes to `data/generated/plan_state.json` and the
next run treats those weeks as frozen. Use `--reset` to start fresh when
testing.

The legacy entry point `create_plan.py` accepts additional flags
(`--dry-run`, `--warmup`, `--render-only`, `--suffix`) that the new CLI does
not yet expose — use it when you need those.

### What comes out

| File | What it is |
|---|---|
| `output/planning_wkNN.xlsx` | The plan: an overview tab, one continuous tab, one tab per day |
| `output/capacity_dashboard_wkNN.html` | Capacity and rejections, open in a browser |
| `output/actie_vereist_wkNN.csv` | Orders a person has to decide on (no future run will place them) |
| `data/generated/plan_state.json` | What this run froze, read by the next run |

Everything in `output/` is generated. Change the code or
`dashboard_template.html`, then re-run; don't edit the files by hand.

## How the code fits together

```
spreadsheets ─▶ load orders + products          frigilunch/adapters/master_data.py, excel_orders.py
             ─▶ bring back frozen weeks         frigilunch/domain/state.py
             ─▶ pick a day + line per order     frigilunch/domain/benders.py   (OR-Tools CP-SAT)
             ─▶ build each day minute by minute frigilunch/domain/oracle.py
                  └─ orders that don't fit go back to the solver, repeat
             ─▶ export                          frigilunch/adapters/excel_export.py, dashboard.py
             ─▶ freeze the next 4 weeks         frigilunch/domain/state.py
```

The solver can't know exact changeover times, because they depend on the
order products run in. So it proposes an assignment, the day builder works out
what really fits, and whatever gets dropped becomes a new constraint for the
next solve. That loop is the heart of the code.

| Folder / file | Holds |
|---|---|
| `frigilunch/cli.py` | The entry point |
| `frigilunch/application/pipeline.py` | `STAGES` tuple and `run_pipeline()` |
| `frigilunch/application/run_week.py` | Assembles adapters and calls the pipeline |
| `frigilunch/domain/` | Pure domain logic — no I/O, no OR-Tools imports |
| `frigilunch/domain/constraints/` | The planning rules (allergens, lines, devices, changeover, sequencing) |
| `frigilunch/adapters/` | I/O — reads spreadsheets, writes Excel and HTML |
| `data/capacity.json` | Capacity, machine unit counts, production window, solver budget |
| `data/setup_times.json` | Changeover and cleaning times |
| `tests/` | The test suite |
| `docs/` | Generated rule list and design notes |
| `src/` | Legacy implementation (reference only) |

## Tests and generated docs

```bash
py -m pytest                    # run the tests
```
