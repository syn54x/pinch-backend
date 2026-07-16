# PRD 0007: DELETE paths don't map FK violations into the IntegrityError hierarchy

> **Filed:** [ferro-orm#306](https://github.com/syn54x/ferro-orm/issues/306)
> (2026-07-16).

**Requested by:** Pinch • **Blocks:** nothing (annoyance, not a blocker) —
found at M5 CP4 while adding `on_delete="RESTRICT"` backstops behind the
category-delete API guards (PR #23 review, finding 2).

## Summary

> **Corrected 2026-07-16** (after the CI-vs-local divergence): the original
> framing ("delete paths never map") was too broad. The behavior is
> **Postgres-version-dependent**: on PG 17 the delete path classifies the
> RESTRICT violation correctly (`ForeignKeyViolationError`); on PG 18.4 the
> same operation surfaces `OperationalError` with `sqlstate=None`. PG 18
> changed the RESTRICT message wording ("violates **RESTRICT setting of**
> foreign key constraint"), so classification evidently keys on message
> text rather than SQLSTATE, and the SQLSTATE isn't propagated on the
> fallback wrap. The save path is unaffected (INSERT wording unchanged in
> PG 18). Two asks: classify by SQLSTATE (23503), and carry `sqlstate`
> even when classification misses.

## Minimal repro (ferro 0.16.2; run against Postgres 18 to see the miss)

```python
import asyncio, uuid
from typing import Annotated
import ferro
from ferro import BackRef, Field, ForeignKey, Model, Relation, engines, execute

DSN = "postgres://postgres:password@localhost:5432/postgres"

class Parent(Model):
    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    name: str
    children: Relation[list["Child"]] = BackRef()

class Child(Model):
    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    parent: Annotated[Parent, ForeignKey(related_name="children", on_delete="RESTRICT")]

async def main() -> None:
    schema = f"ferro_repro_{uuid.uuid4().hex[:8]}"
    await ferro.connect(DSN)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    ferro.reset_engine()
    await ferro.connect(f"{DSN}?ferro_search_path={schema}", auto_migrate=True)
    async with engines.session():
        parent = await Parent.create(name="p")
        await Child.create(parent=parent)

        try:
            await Child.create(parent_id=uuid.uuid7())      # dangling FK
        except Exception as e:
            print(f"insert dangling FK -> {type(e).__name__}")

        try:
            await parent.delete()                            # RESTRICT hit
        except Exception as e:
            print(f"instance .delete() -> {type(e).__name__}")

        pid = parent.id
        try:
            await Parent.where(lambda p, pid=pid: p.id == pid).delete()
        except Exception as e:
            print(f"filtered .delete() -> {type(e).__name__}")

        await execute(f'DROP SCHEMA "{schema}" CASCADE')

asyncio.run(main())
```

Output:

```
insert dangling FK -> ForeignKeyViolationError
instance .delete() -> OperationalError
filtered .delete() -> OperationalError
```

## Expected

All three raise `ForeignKeyViolationError` — one SQLSTATE class, one
exception type, regardless of which statement tripped it. Postgres reports
23503 for both directions (`insert or update on table "child" violates …`
and `update or delete on table "parent" violates RESTRICT …`); the driver
error carries the code either way.

## Suggested shape

The save path already translates 23503 (and its siblings 23505/23502/23514)
into the `IntegrityError` subclasses; the delete RPC path
(`delete_filtered`, which instance `.delete()` also routes through) appears
to wrap driver errors as `OperationalError` without consulting SQLSTATE.
Routing delete-path driver errors through the same SQLSTATE→exception
translation used by saves closes the gap for all four integrity subclasses
at once, not just FK violations.

## Impact on Pinch when fixed

Already made version-agnostic in `d033032`:
`tests/test_taxonomy_models.py::test_category_parent_fk_restricts_delete_at_the_db`
accepts either class and pins the invariant (delete refused, rows intact).
When ferro classifies by SQLSTATE, narrow it to `ForeignKeyViolationError`
alone. CI and dev are now both on postgres:18 (the version skew that hid
this is closed).
