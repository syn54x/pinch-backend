# PRD 0005: `bulk_create` must chunk under backend bind-parameter limits

> **Filed:** [ferro-orm#298](https://github.com/syn54x/ferro-orm/issues/298);
> the upstream issue body is the canonical PRD.

**Requested by:** Pinch • **Blocks:** M4 import commit (PRD pinch-backend#13
— atomic creation of up to 10,000 transactions per CSV import).

## Summary

`Model.bulk_create(instances)` renders the whole batch as one multi-row
`INSERT`, binding `n_rows × n_columns` parameters into a single statement.
Both backends enforce hard bind-parameter ceilings, so the call fails at a
row count that silently depends on the model's column count and the active
backend. `bulk_create` should chunk internally — split the batch into as
many statements as the backend's bind limit requires — transparently to the
caller.

## Observed failures (ferro 0.16.0)

- **sqlite** (limit 32,766, `SQLITE_MAX_VARIABLE_NUMBER`): a 5-column model
  inserts 6,553 rows and fails at 6,554 — 6,553 × 5 = 32,765 binds —
  with `OperationalError: … too many SQL variables`.
- **Postgres** (limit 65,535: the wire protocol's `Bind` carries an int16
  parameter count): a 12-column model at 10,000 rows fails with
  `InterfaceError: … PgConnection::run(): too many arguments for query:
  110000 (sqlx_postgres)`.

## Motivating Pinch workload

The M4 import commit inserts up to `PINCH_IMPORT_MAX_ROWS` (default 10,000)
Transaction rows (~12 columns ≈ 120k binds) atomically inside one
`transaction()`:

```python
async with transaction():
    await Transaction.bulk_create(rows)   # rows ≈ 10_000, ~12 columns
    import_.status = ImportStatus.COMMITTED
    await import_.save()
```

Chunking caller-side would mean a `CHUNK_SIZE` constant in Pinch encoding
sqlx/sqlite bind internals — exactly the leak ADR 0003 exists to prevent.

## Requirements

- [ ] `bulk_create` succeeds for any batch size; statement splitting is an
      internal concern driven by the active backend's bind limit
- [ ] Inside an ambient `transaction()`, the transaction remains the
      atomicity boundary (verified on 0.16.0: multiple `bulk_create` calls
      in one transaction roll back to zero rows on a mid-batch failure, on
      both backends — the building blocks already compose correctly)
- [ ] With no ambient transaction, ferro wraps the chunks in its own
      transaction, preserving today's all-or-nothing semantics of the
      single statement rather than regressing them
- [ ] Return value stays the total inserted count across chunks

## Notes

Performance is not a concern: under the limit, 10k rows insert in ~0.15s on
Postgres, and chunked inserts measured comparably. An explicit
`batch_size:` override is nice-to-have, not required — the backend-derived
maximum is the correct default.
