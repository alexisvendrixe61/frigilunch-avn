# ADR 001 — Logic-Based Benders decomposition for the optimizer

**Status:** Accepted

## Context

The production planner must assign each order to a (day, line) pair and build an exact minute-by-minute timeline for each day. The two sub-problems are coupled: the real time a day consumes depends on the sequence of orders, and the sequence changes the changeover costs between consecutive products.

The naive approach — a single CP-SAT model that decides both the assignment and the sequence — is intractable at the order volumes we plan (400–500 orders over an 8-week horizon). Encoding the sequence-dependent changeover function directly inside CP-SAT would require either:

- A time-indexed formulation: exponential in the number of orders per day.
- A big-M relaxation: loose bounds that cause the solver to believe days fit more than they do.

The deeper problem is that `schedule_day` — the real day builder — is a deterministic function of the SET of orders placed on a day, not a function the solver can represent internally. Calling it is exact; approximating it is not.

## Decision

Use logic-based Benders decomposition:

1. **Master problem (CP-SAT):** assigns each order to a (day, line), treating each day's capacity as `sum(order durations + PROXY_BUFFER_MIN) ≤ planning_cap`. The buffer is a flat per-order approximation of the real changeover cost.

2. **Sub-problem (oracle):** for each proposed day, call `schedule_day()` — the real greedy day builder — which applies actual sequence-dependent changeovers and drops any orders that would push past the shift end.

3. **Cut generation:** every day where the oracle drops at least one order generates a no-good cut in the master problem: the exact set of orders assigned to that day is forbidden from sharing it again. Dropped orders are free to migrate to other days in the next iteration.

4. **Best-so-far semantics:** each iteration is evaluated by how many orders the oracle actually placed. The best result is kept when the loop terminates — not necessarily the last iteration, since CP-SAT's solution can vary as cuts accumulate.

The loop terminates when no day drops any order (convergence), when the time budget is exhausted, or when a plateau of iterations shows no improvement.

This is implemented in `frigilunch/application/date_optimizer.py` (`assign_production_dates_feedback`). The generic loop skeleton lives in `frigilunch/domain/benders.py` (`run_benders`) with injected solver and oracle callables.

## Consequences

**Non-determinism.** CP-SAT runs multiple workers under a wall-clock budget. Re-running the same input can land a few orders differently. Never quote a single run's exact scheduled count as the definitive answer; the variance is typically ± 5–10 orders on a 450-order run.

**Runtime.** A typical run takes 3 iterations of 4–6 minutes each (total 15–20 minutes). The oracle call on all days in the horizon is the bottleneck — it is exact and cannot be parallelised across days without coordination overhead.

**Approximation residual.** The proxy buffer (`FRIGI_PROXY_BUFFER`, default 30 minutes) is an estimate. A larger buffer converges faster (fewer oracle drops per iteration) but leaves more end-of-shift time unbookable; a smaller buffer converges slower but fills days tighter. Measured at buffer=30 vs. buffer=60, 30 schedules roughly 10 more orders per run.

**Best-iteration semantics.** Because the best iteration is kept, the committed plan may not correspond to the solver's last assignment. This is intentional — a later iteration with fewer oracle drops is not always better than an earlier one that placed more orders.
