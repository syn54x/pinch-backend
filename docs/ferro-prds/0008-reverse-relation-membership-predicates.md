# PRD 0008: Reverse-relation membership predicates (EXISTS from the root query)

> **Filed:** [ferro-orm#307](https://github.com/syn54x/ferro-orm/issues/307)
> (2026-07-16).

**Requested by:** Pinch • **Blocks:** M6 CP2 (transfer model, pinch#27) and
M6 CP4 (pipeline & flywheel, pinch#29) — the `is_transfer` transaction-list
filter and the history stage's untracked-transfer extension. Found at M6 CP0
(pinch#25), the scratch-verification spike gating the milestone's model
slices (ADR-0003: block on ferro, never work around).

## Summary

A root-model query cannot filter on *membership in a reverse (BackRef)
relation*: "give me the transactions that appear in any transfer". Predicate
traversal in `where()` resolves forward-FK relations only — BackRef names
are not queryable — and `.in_()` accepts only concrete collections, so an
EXISTS-shaped subquery can't be spelled either. The workload needs the
predicate on the root query because it must compose with every other filter
and with keyset pagination on the root table.

## Motivating Pinch queries

M6 introduces `Transfer`, a link row with two nullable **unique** FKs to
`Transaction` (`outflow_transaction`, `inflow_transaction`; one side present
= untracked counterparty, both = linked pair). Spending exclusion *derives*
from membership — "appears in any transfer, either column" — deliberately
one EXISTS, never a flag that can drift:

```sql
-- the transaction list's is_transfer filter (true / false)
SELECT t.*
FROM transaction t
WHERE t.ledger_id = $1
  AND [NOT] EXISTS (
    SELECT 1 FROM transfer tr
    WHERE tr.outflow_transaction_id = t.id
       OR tr.inflow_transaction_id = t.id
  )
ORDER BY t.date DESC, t.id DESC   -- composes with the keyset cursor
LIMIT 50;
```

The same shape recurs in M6 CP4's history stage ("most recent reviewed
transaction with this payee that is categorized *or in an untracked
transfer*") and in M8 reporting (transfers excluded by default).

## Verified failures (ferro 0.16.2, scratch-verified on Postgres 18)

With `Transfer.outflow_transaction` / `.inflow_transaction` declared
`ForeignKey(related_name=..., unique=True)` and the paired one-to-one
BackRefs `transfer_out` / `transfer_in` on `Transaction`:

```text
Transaction.where(lambda t: t.transfer_out != None)
  -> AttributeError: STxn has no queryable column 'transfer_out'.
     Valid columns: ... Valid relations: category, ledger.   # BackRefs absent

Transaction.where(lambda t: t.id.in_(Transfer.select(lambda tr: tr.outflow_transaction_id)))
  -> TypeError: The 'in_' operator expects a list, tuple, or set, got ProjectedQuery

Transaction.select().left_join(lambda t: t.transfer_out)
  -> AttributeError (same — explicit chainers resolve the same forward-only specs)
```

Root cause (0.16.2 source): `build_relation_specs` skips every
non-`ForeignKey` entry in `ferro_relations`, so BackRef/M2M names never
reach `__ferro_relation_specs__`, and `FieldProxy.in_` type-checks for
list/tuple/set.

## Suggested shape (illustrative only — ferro idioms win)

Any one of these covers the workload:

1. **Reverse-relation traversal in predicates**, rendered as a correlated
   EXISTS semi-join (never a row-multiplying JOIN):
   `Transaction.where(lambda t: t.transfer_out != None)`, and ideally
   deeper predicates on the reverse target
   (`t.lines.category_id.in_(ids)` — see PRD 0009, which may be the same
   mechanism under `|`-composition).
2. **Subquery membership**: `.in_(query)` accepting a single-column
   projected query, rendered as `IN (SELECT ...)`.
3. **An explicit exists-predicate node**:
   `Transfer.where(...).exists_for(lambda tr: tr.outflow_transaction == t)`
   or similar correlated form.

Semi-join semantics matter: the result must stay root-shaped (no duplicate
transactions when a future reverse relation is to-many), and the predicate
must compose under `&`/`|`/negation with ordinary column predicates.

## Impact on Pinch when fixed

M6 CP2's `is_transfer` filter and CP4's history extension implement
directly; M8's report queries reuse the same predicate. Until then, both
slices are blocked (ADR-0003) — no id-materialization workaround will be
merged.
