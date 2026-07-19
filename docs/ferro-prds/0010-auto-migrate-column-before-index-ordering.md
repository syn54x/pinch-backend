# PRD 0010: auto-migrate ordering — add columns before indexes that reference them

> **Filed:** [ferro-orm#324](https://github.com/syn54x/ferro-orm/issues/324)
> (2026-07-19).

**Requested by:** Pinch • **Blocks:** M7 CP2 (the sync job, pinch#34) — the
provider-identity columns and their composite unique. Found at M7 CP0
(pinch#32), the scratch-verification spike gating the milestone's schema
slices (ADR-0003: block on ferro, never work around).

## Summary

When one auto-migrate pass must both **add new columns** to an existing
table and **create an index/constraint referencing those columns**, ferro
orders the index creation first and the boot crashes with
`column ... does not exist`. Each half works alone: new nullable (and
non-null-with-default) columns are added and backfilled correctly, and the
composite unique index is created and enforced correctly when the columns
already exist. Only the combined single-deploy shape fails — which is
exactly the shape a real upgrade takes.

## Motivating Pinch shape

M7 gives `Transaction` provider identity. Pre-M7 installs have populated
transaction tables; the M7 code version declares, on the existing model:

```python
class Transaction(Model):
    __ferro_composite_uniques__ = (("account_id", "provider_transaction_id"),)
    ...existing fields...
    provider_transaction_id: str | None = None   # new
    pending: bool = False                        # new
```

First boot with `auto_migrate=True, migrate_updates=True` against the
populated schema must add both columns, backfill, and create the unique
index. Today it dies before adding the columns.

## Verified failures (ferro 0.17.0, Postgres 18, scratch-verified)

Phase 1 process creates + populates the table (no provider columns, no
unique). Phase 2 process boots with the model above:

```text
await connect(url, auto_migrate=True, migrate_updates=True)
  -> ferro.exceptions.OperationalError: SQL Execution failed for
     'scratchtxn' index: error returned from database:
     column "provider_transaction_id" does not exist
```

Removing `__ferro_composite_uniques__` from the phase-2 model: both columns
are added, existing rows backfilled (`NULL` / `false`) — pass. Re-adding
the unique in a third boot (columns now present): index
`uq_scratchtxn_account_id_provider_transaction_id` is created and enforced
— duplicate `(account, provider_txn_id)` rejected via `UniqueViolationError`,
NULL duplicates allowed (Postgres default NULLS DISTINCT — the semantics
Pinch wants: pre-M7 rows all carry NULL). So ordering is the only gap.

## Desired behavior

Within one auto-migrate pass over a table: column additions execute before
index/constraint creation that references them. No new API — a sequencing
fix in the migration planner.
