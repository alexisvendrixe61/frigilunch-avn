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
py create_plan.py --week 36 --year 2026

# Look without committing anything (needs saved state from earlier runs, see below)
py create_plan.py --week 36 --year 2026 --dry-run

# Quick test from a clean slate: ignore saved state, skip the warm-up, commit nothing
py create_plan.py --week 36 --year 2026 --reset --warmup 0 --dry-run

# Only re-render the dashboard from the last run of that week (no solve)
py create_plan.py --week 36 --year 2026 --render-only
```

A run takes minutes, not seconds: the solver works against a time budget, so
two runs on the same input can differ slightly.

**Warm-up.** With no saved state (or with `--reset`), the planner first runs
the 4 weeks before `--week`, each with a normal commit, so the target week
starts from a realistic frozen plan. That costs 4 extra runs.

- `--warmup 0` skips it: faster, but the target week looks fuller than it is.
- The warm-up has to commit, so `--dry-run` refuses to run while a warm-up is
  due. Either run the warm-up once without `--dry-run`, or add `--warmup 0`.

**Watch out:** a run without `--dry-run` writes to
`data/generated/plan_state.json`, and the next run treats those weeks as
frozen. Use `--dry-run` whenever you are just trying something.

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
spreadsheets ─▶ load orders + products          src/order_filter.py, src/product_setup_loader.py, src/loader.py
             ─▶ bring back frozen weeks         src/plan_state.py
             ─▶ pick a day + line per order     src/scheduler_ortools_v3.py   (OR-Tools CP-SAT)
             ─▶ build each day minute by minute src/scheduler.py, src/day_sequencer.py
                  └─ orders that don't fit go back to the solver, repeat
             ─▶ check, count, export            src/validator.py, src/totals.py, src/exporter.py, src/dashboard.py
             ─▶ freeze the next 4 weeks         src/plan_state.py
```

The solver can't know exact changeover times, because they depend on the
order products run in. So it proposes an assignment, the day builder works out
what really fits, and whatever gets dropped becomes a new constraint for the
next solve. That loop is the heart of the code.

| Folder / file | Holds |
|---|---|
| `create_plan.py` | The entry point: runs the whole pipeline above |
| `src/constraints/` | The planning rules, one module per domain (allergens, lines, days, equipment, changeover, …). `registry.py` lists every rule |
| `src/config.py` | Shift times and per-line capacity, read from `data/capacity.json` |
| `data/capacity.json` | Capacity, machine unit counts, production window, solver budget |
| `data/setup_times.json` | Changeover and cleaning times |
| `tests/` | The test suite |
| `docs/` | Generated rule list and design notes |

## Tests and generated docs

```bash
py -m pytest                    # run the tests
py -m src.constraints.registry  # regenerate docs/constraints.md after changing a rule
```
