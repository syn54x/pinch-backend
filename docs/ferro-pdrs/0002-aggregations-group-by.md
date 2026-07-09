# PDR 0002: Aggregations and group-by

**Requested by:** Pinch • **Blocks:** net worth history, spending reports,
loan payoff projection inputs, budgets (future).

## Summary

Aggregate functions (`SUM`, `COUNT`, `AVG`, `MIN`, `MAX`) with `GROUP BY`,
`HAVING`, and hydration of aggregate rows into plain Pydantic result models
(not table models).

## Motivating Pinch queries

Spending by category for a period (the core report; hierarchy rollup happens
app-side over these leaf sums, so no recursive CTEs are needed):

```sql
SELECT sl.category_id, SUM(sl.amount_minor) AS spent
FROM split_line sl
JOIN "transaction" t ON t.id = sl.transaction_id
JOIN account a ON a.id = t.account_id
WHERE a.ledger_id = $1
  AND t.date BETWEEN $2 AND $3
  AND t.is_transfer = FALSE
GROUP BY sl.category_id;
```

Net worth over time (daily balance snapshots summed across accounts):

```sql
SELECT snapshot_date, SUM(balance_minor) AS net_worth_minor
FROM balance_snapshot
WHERE ledger_id = $1
GROUP BY snapshot_date
ORDER BY snapshot_date;
```

Loan payoff projection input (observed payment behavior):

```sql
SELECT date_trunc('month', t.date) AS month, SUM(t.amount_minor) AS paid
FROM "transaction" t
WHERE t.transfer_to_account_id = $1  -- payments into the loan account
GROUP BY month ORDER BY month;
```

## Requirements

- [ ] `SUM`/`COUNT`/`AVG`/`MIN`/`MAX` over model columns
- [ ] `GROUP BY` one or more columns/expressions (incl. `date_trunc`)
- [ ] `HAVING`
- [ ] Results hydrate into caller-supplied Pydantic models
- [ ] Composes with joins (PDR 0001) and filters

## Notes

Depends on PDR 0001 for the joined variants; the single-table forms
(net worth query) are independently useful and could land first.
