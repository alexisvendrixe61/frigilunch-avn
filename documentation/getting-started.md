# Getting started

## Prerequisites

- Python 3.11 or later
- Dependencies installed from `requirements.txt`:

```bash
pip install -r requirements.txt
```

The key dependencies are `ortools` (CP-SAT solver), `openpyxl` (Excel I/O), and `python-dotenv` (optional environment variable loading from `.env`).

## Input files

Two source spreadsheets go in `data/source_data/`:

| File | Contents | Sheet used |
|---|---|---|
| `orderverwerking en planning.xlsm` | The order book | `orderverwerking` |
| `Planning info.xlsx` | Product master database | *(first sheet)* |

Both are re-read on every run. Never edit the generated JSON files in `data/generated/` by hand — they are overwritten each run.

## Running the planner

```bash
python -m frigilunch.cli --week 40 --year 2026
```

The alternative entry point `create_plan.py` at the repo root is identical:

```bash
python create_plan.py --week 40 --year 2026
```

### All flags

| Flag | Default | Effect |
|---|---|---|
| `--week N` | required | ISO week number to plan |
| `--year Y` | current year | ISO year |
| `--reset` | off | Ignore existing `plan_state.json` and start from scratch |
| `--dry-run` | off | Solve and export outputs but do **not** write `plan_state.json` |
| `--warmup N` | auto | Run the N preceding weeks first so the rolling lock is realistic. Default: 4 on a cold start (empty state or `--reset`), 0 otherwise |
| `--suffix LABEL` | none | Append a label to output filenames: `planning_wk40_test.xlsx` |
| `--render-only` | off | Re-render the HTML dashboard from the previous run's `chart_data_*.json`. No solve, no Excel write |

`--dry-run` and `--warmup` cannot be combined: warmup weeks must commit to build the rolling lock.

## What comes out

All outputs go to `output/`:

| File | Contents |
|---|---|
| `planning_wkNN.xlsx` | The production plan: one overview tab, one continuous tab, one tab per production day |
| `capacity_dashboard_wkNN.html` | Interactive capacity dashboard — open in a browser |
| `actie_vereist_wkNN.csv` | Orders that no future run will place automatically; each needs a human decision |

`data/generated/plan_state.json` is also updated (unless `--dry-run`) — it records the committed weeks that the next run must leave frozen.

## Common first-run mistakes

**The solver runs but produces no output.**
Check that both spreadsheets exist in `data/source_data/` with the exact filenames above. The loader errors on a missing file.

**All orders are rejected as "unknown product".**
The product master sheet was not found or the `art_nr` column does not match between the two spreadsheets. Run with `--dry-run` and read the `[WARN]` lines.

**Every run moves already-confirmed production.**
You are running with `--reset` each time, which clears the freeze. Use `--reset` only when you genuinely want a clean slate. Remove it for the normal weekly run.

**A run is much faster than expected and places far fewer orders.**
`plan_state.json` records a `frozen_until` date in the future, so the solver has almost no open days to assign into. Use `--reset` to clear the freeze, or `--dry-run` to inspect what the solver is being given.

**Outputs from different weeks overwrite each other.**
Use `--suffix` to differentiate test runs: `--suffix test` appends `_test` to all filenames.
