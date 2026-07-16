# PRD 0009: OR-composition across a left-joined reverse child table

> **Filed:** [ferro-orm#308](https://github.com/syn54x/ferro-orm/issues/308)
> (2026-07-16).

**Requested by:** Pinch • **Blocks:** M6 CP1 (split lines, pinch#26) — the
transaction list's line-aware `category_id` filter. Found at M6 CP0
(pinch#25), the scratch-verification spike gating the milestone's model
slices (ADR-0003: block on ferro, never work around). Sibling of PRD 0008
(reverse-relation membership predicates) — plausibly one mechanism.

## Summary

A root-model query cannot filter on "a root column matches, OR a column on
a reverse (BackRef) child row matches, keeping child-less roots". The child
edge is a reverse relation, so neither `where()` traversal nor
`left_join()` can name it (forward-FK-only relation specs), and the OR's
left branch must retain roots that have no child rows at all.

## Motivating Pinch queries

M6 splits a transaction into `SplitLine` rows (child FK → transaction,
CASCADE; nullable category FK). While split, the parent's own category is
vacated — exactly one layer holds categories. The list's `category_id`
filter must then be **line-aware**, or splitting makes transactions less
findable:

```sql
-- category filter, subtree-inclusive id set in $2
SELECT t.*
FROM transaction t
WHERE t.ledger_id = $1
  AND (
    t.category_id = ANY($2)
    OR EXISTS (
      SELECT 1 FROM split_line l
      WHERE l.transaction_id = t.id AND l.category_id = ANY($2)
    )
  )
ORDER BY t.date DESC, t.id DESC   -- composes with the keyset cursor
LIMIT 50;
```

(Spelled here with EXISTS because the equivalent
`LEFT JOIN split_line … WHERE t.category_id = ANY($2) OR l.category_id = ANY($2)`
needs DISTINCT-on-root to avoid a multi-line split matching twice — either
rendering is fine; root-set semantics are the requirement.)

## Verified failures (ferro 0.16.2, scratch-verified on Postgres 18)

With `SplitLine.txn: ForeignKey(related_name="lines", on_delete="CASCADE")`
and `Transaction.lines: Relation[list[SplitLine]] = BackRef()`:

```text
Transaction.select().left_join(lambda t: t.lines)
    .where(lambda t: (t.category_id.in_(ids)) | (t.lines.category_id.in_(ids)))
  -> AttributeError: STxn has no queryable column 'lines'.
     Valid columns: ... Valid relations: category, ledger.   # BackRefs absent

Transaction.where(lambda t: (t.category_id.in_(ids)) | (t.lines.category_id.in_(ids)))
  -> AttributeError (same)
```

Root cause is shared with PRD 0008: `build_relation_specs` registers
forward FKs only, so the reverse edge is invisible to traversal and to the
explicit join chainers.

## Suggested shape (illustrative only — ferro idioms win)

Reverse-relation traversal in predicates with **semi-join (EXISTS)
rendering per branch** covers this and PRD 0008 in one mechanism:

```python
txns = await Transaction.where(
    lambda t: (t.category_id.in_(ids)) | (t.lines.category_id.in_(ids))
).all()
```

- The reverse branch renders as a correlated EXISTS, so a transaction with
  three matching lines is still one result row and child-less transactions
  survive the OR through the other branch — no LEFT/DISTINCT bookkeeping
  for the caller.
- Must compose with root-column predicates under `&`/`|`, with
  `order_by()` on root columns, and with `limit()` — the consumer is a
  keyset-paginated list query.

If instead ferro grows reverse edges on the existing `left_join()` +
whole-path LEFT machinery, the docs' pinned "a join never multiplies root
rows" property needs an answer for to-many edges (implicit DISTINCT on the
root PK, or rejection pointing at the EXISTS form).

## Impact on Pinch when fixed

M6 CP1's line-aware `category_id` filter implements directly, and M8's
"reporting operates on lines" queries build on the same reverse traversal.
Until then the slice is blocked (ADR-0003) — no id-materialization
workaround will be merged.
