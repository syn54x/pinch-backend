# PRD 0012: expression group keys — temporal bucketing for grouped aggregates

> **Filed:** [ferro-orm#327](https://github.com/syn54x/ferro-orm/issues/327)
> (2026-07-22).

**Requested by:** Pinch • **Blocks: nothing** — filed as recorded demand from
M8 CP0 (pinch#46), the verification spike for the reporting milestone
(pinch#45). Unlike prior PRDs this is not a gate: M8's ruling is that
daily-grain SQL (`GROUP BY` the raw `date` column — works today, verified)
plus Python folding of the daily aggregate rows into weekly/monthly display
buckets is a legitimate posture, because the fetched row count is *days with
activity*, not transactions. This PRD records where that posture stops
scaling and what surface would retire it.

## Summary

Group keys in an aggregate projection are **columns only** — plain
(`t.category_id`), or traversed (`t.account.label`). There is no way to
group by an *expression of* a column, and the canonical reporting case is
temporal: `GROUP BY date_trunc('month', t.date)`. Verified on ferro 0.17.1:
the dict-lambda projection accepts column references and aggregate verbs
(`sum`/`avg`/`min`/`max`/`count`) and nothing else; there is no date-part
accessor or function surface anywhere in the query API (no `date_trunc`,
`.year`, `.month`, `extract` in `src/ferro/query/` or the aggregation
guide).

## Motivating Pinch shape

M8's spending and net-worth series endpoints serve monthly buckets for
long ranges (the Net Worth screen's `All`). Wanted, in one round trip:

```python
rows = await Transaction.select(
    lambda t: {
        "month": t.date.trunc("month"),        # ← does not exist
        "total": t.amount_minor.sum(),
    }
).where(lambda t: t.amount_minor < 0).all()
```

Today's blessed spelling fetches daily grain and folds in Python:

```python
rows = await Transaction.select(
    lambda t: {"d": t.date, "total": t.amount_minor.sum()}
).where(lambda t: t.amount_minor < 0).all()      # verified: PASSES
# ...then fold ~N-days rows into months in Python
```

Fine at personal-ledger scale (a decade ≈ 3,650 daily rows, usually far
fewer). It stops being fine when the grouped *key space* is what explodes,
or when a `HAVING`-style filter over bucket totals should prune server-side.

## Desired behavior

An expression-capable group key in the dict-lambda projection, temporal
first:

- A portable truncation surface — e.g. `t.date.trunc("month")` or
  `ferro.trunc("month", t.date)` — compiling to `date_trunc` on Postgres.
  Granularities: `day` (identity for `date` columns), `week`, `month`,
  `quarter`, `year`.
- Build-time rejection of granularities/types with no portable meaning,
  matching the aggregation guide's build-time-honesty stance.
- Composes as a group key and as an `order_by` source exactly like a plain
  projected field.

ADR-0007/0009's record-plan shape already reserves a per-field `expr` slot,
and #282's design note said the section extends without reshaping — this is
the first concrete `expr` consumer.

## Out of scope (for this PRD)

General computed expressions (`t.amount_minor / 100`), `HAVING`, and
window functions — mentioned only to bound the ask; temporal truncation is
the whole request.
