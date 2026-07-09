# PRD 0001: Table joins

**Requested by:** Pinch • **Blocks:** transaction list endpoints, review
inbox, transfer detection — i.e. the MVP spine.

## Summary

Query a model with related models fetched in the same statement, via the
already-supported FK relationships: at minimum inner and left joins,
filterable and sortable on joined columns.

## Motivating Pinch queries

The transaction list — Pinch's hottest read path — joins three tables and
filters on two of them:

```sql
SELECT t.*, a.name AS account_name, c.name AS category_name
FROM "transaction" t
JOIN account a ON a.id = t.account_id
LEFT JOIN category c ON c.id = t.category_id
WHERE a.ledger_id = $1
  AND t.date BETWEEN $2 AND $3
  AND c.id = ANY($4)
ORDER BY t.date DESC
LIMIT 50;
```

The review inbox groups unreviewed transactions by day with their account
and proposed category; transfer detection scans candidate pairs across
accounts (self-join on amount/date windows).

## Requirements

- [ ] Inner join and left join through declared FK relations
- [ ] Filter (`WHERE`) and sort (`ORDER BY`) on joined columns
- [ ] Hydrate joined rows into their Pydantic models (no N+1)
- [ ] Compose with existing limit/offset and (ideally) keyset pagination
- [ ] Self-joins (same model twice under different aliases)

## Strawman API (illustrative only — ferro idioms win)

```python
txs = await Transaction.objects.join(Transaction.account).left_join(
    Transaction.category
).filter(
    Transaction.account.ledger_id == ledger_id,
    Transaction.date.between(start, end),
).order_by(-Transaction.date).limit(50).all()
```
