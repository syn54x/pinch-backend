# PRD 0011: auto-migrate — apply `on_delete` changes to existing foreign keys

> **Filed:** [ferro-orm#325](https://github.com/syn54x/ferro-orm/issues/325)
> (2026-07-19).

**Requested by:** Pinch • **Blocks:** M7 CP1 (connect & connection
lifecycle, pinch#33) — disconnect's severs-never-destroys contract. Found
at M7 CP0 (pinch#32), the scratch-verification spike gating the milestone's
schema slices (ADR-0003: block on ferro, never work around).

## Summary

Changing a `ForeignKey`'s `on_delete` on an existing model is **silently
ignored** by auto-migrate: the model declares the new action, the database
keeps the old one, and the two disagree without any error. This is the
dangerous kind of miss — not a crash but a silent divergence whose failure
mode is destructive.

## Motivating Pinch shape

Every pre-M7 install has `account.connection_id` with the default
`ON DELETE CASCADE`. M7's disconnect contract is *sever, never destroy*:
deleting a `Connection` must null the reference and leave the accounts —
and everything hanging off them — intact:

```python
# before (M1..M6)
connection: Annotated[Connection | None, ForeignKey(related_name="accounts")] = None
# after (M7)
connection: Annotated[Connection | None, ForeignKey(related_name="accounts", on_delete="SET NULL")] = None
```

If the migration is silently skipped, the first disconnect on an upgraded
install cascade-deletes the connection's accounts, their transactions,
splits, and transfer links — the user's reviewed history, gone, from an
operation documented as non-destructive. Pinch cannot ship disconnect on
top of this silence.

## Verified failure (ferro 0.17.0, Postgres 18, scratch-verified)

Phase A process: child model with FK `on_delete="CASCADE"` (the default),
auto-migrated, rows inserted. `pg_constraint.confdeltype` = `'c'`. Phase B
process: identical models except `on_delete="SET NULL"`, boots with
`auto_migrate=True, migrate_updates=True`:

```text
confdeltype stays 'c'                 # no ALTER emitted, no warning
DELETE parent  -> child row deleted   # CASCADE still live; model says SET NULL
```

For contrast, `on_delete="SET NULL"` declared at table-creation time works
correctly (verified in the same spike): the constraint is created as
`SET NULL`, deleting the target nulls the child's FK, the child survives.
Only the *alteration* path is missing.

## Desired behavior

`migrate_updates` detects an `on_delete` mismatch between the declared FK
and the live constraint and rebuilds it (`ALTER TABLE ... DROP CONSTRAINT
... ADD CONSTRAINT ... ON DELETE <new>`). If ferro prefers not to auto-drop
constraints, an explicit loud error ("FK on_delete drift; migrate manually")
would still unblock Pinch — silence is the only unacceptable outcome.
