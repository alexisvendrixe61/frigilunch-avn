# A device-caused drop is not a capacity drop

Status: **fixed 2026-09-04** (`FRIGI_DEVICE_DROP_CUTS`, on by default). Kept as
the record of what was wrong, because the shape of the bug is worth
recognising again and the risk it carries is permanent.

## The symptom

`toestelconflicten` in the feedback log read 0 in every iteration, while
single-unit machines (`gernal` above all) were named in most of the remaining
rejections. The scheduler was bottlenecked on equipment and the mechanism meant
to learn about equipment never fired once.

## Why it read 0

M2 turns an observed device clash into a solver cut: "these two orders may not
share a day". `_run_oracle_day` looked for one by comparing every planned L1
order against every planned L2 order and asking whether they overlap in time
*and* both need the same scarce machine.

That comparison can essentially never succeed, because M1 already made it
impossible. `stagger_around` pushes the second line's order forward until the
machine is free, so two orders holding one machine at the same moment is
exactly the state the day builder exists to prevent. **The detector was looking
for a condition its own upstream guarantees away.**

## Where the evidence was being destroyed

The order was pushed past the shift end and dropped — and at that moment the
cause was discarded:

- `_schedule_lijn` knew why. It even built a `skipped` list with a reason per
  dropped order. That list was never read by anything: local, appended to, and
  thrown away on return.
- `schedule_day` then recovered the drops by set difference — "which candidates
  are not in the result" — and labelled every one of them `CAPACITY_DROP`.

So an order that died because the Gernal was held by the other line became
indistinguishable from one that died because the day was simply full.

The loop could then only emit a generic no-good cut: *of these N orders on this
(day, line), at most N-1*. That says **this set is too big**. The truth was
**these two cannot share a day**. Only the second is useful — the solver drops
one order, tries a different combination next iteration, and never learns the
pair, so the same impossible pairing gets re-proposed until the budget runs
out. Same shape as the S2 bug fixed alongside it: answering "this set is
MISSING something" with "this set is too big" makes the next iteration *more*
likely to fail the same way.

## What it does now

1. `scheduler._device_caused_drops` decides which of a line's drops the shared
   machine is responsible for, and for which machine.
2. `DaySchedule.device_dropped` carries `(order, line, devices)` out of the day
   builder, next to `dropped` and `positie_dropped`. The day warning is
   `TOESTEL_DROP`, not `CAPACITY_DROP`.
3. `_run_oracle_day` pairs each such drop with the orders on the other line
   that actually held one of those machines, and returns them; the loop adds
   each pair to `global_conflict_pairs`.

### The test is a counterfactual, not the product's device list

`_device_caused_drops` does not ask "did this order want a blocked machine".
It rebuilds the same line with the other line's claim **removed** and asks who
survives that but not the real build. An order that dies either way died of
capacity.

This is the part not to simplify away. An order pushed by a device block **may
simply not have fit**. Recording that as a device conflict forbids a pairing
that is in fact perfectly schedulable, and then the change costs orders instead
of winning them. `test_an_order_that_would_not_have_fitted_anyway_is_not_blamed_on_the_machine`
pins it.

### The cuts are always day-scoped

Regardless of what `FRIGI_DEVICE_CUT_DAY_SCOPED` says about the (near-dead)
overlap-derived cuts. The evidence is that **today's** sequence could not fit
the two around one machine; on another day, with other day-mates and other
changeovers, they often can. These cuts are never retracted, so they have to
stay narrow.

A blocking order belonging to an earlier phase yields no pair: it is real, but
it is not this model's to move.

## Measured

Week 36, full warm-up chain, `--reset`, 2 runs per arm, whole-horizon totals,
on top of everything else already shipped:

| | off | on |
|---|---|---|
| Orders scheduled | 425, 427 | 427, 428 |
| Rejections excluding unknown articles | 11, 12 | **8, 7** |
| `ORACLE-DROPPED` | 3, 3 | **0, 0** |
| `NEVER-PLACED` | 3, 3 | 3, 2 |
| Rejected units | 481k, 499k | **389k, 439k** |
| Scheduled units | 4.208M, 4.212M | **4.230M, 4.284M** |
| Changeover minutes | 8,020, 8,140 | 8,180, 8,210 |
| `toestelconflicten` over all iterations | 0, 0 | 357, 206 |

`ORACLE-DROPPED` goes to zero in both measured runs — though a third run with
the flag on landed on 1, so the honest claim is that the bucket is largely
gone, not that it is provably always empty. Unlike the S2/phase-context work,
this one also moves **volume**: rejected units drop 60–110k with no overlap
between the arms. That fits what the bucket meant — "capacity looked available
in the proxy model but wasn't once real changeovers applied" was largely the
machine, and the solver could not learn it because the reason never survived
the day builder.

Read honestly: `NEVER-PLACED` is unchanged (2–3 either way) — real
oversubscription, untouched by any of this. Changeover is ~1% higher. And the
conflict counter is summed per pair per day per iteration, so 357 is not 357
distinct pairs; it is the same evidence re-observed each iteration. What it
tells you is that the mechanism has evidence at all, which it never had before.
