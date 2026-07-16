# PRD 0007: DELETE paths don't map FK violations into the IntegrityError hierarchy

> **Filed:** not yet — draft.

**Requested by:** Pinch • **Blocks:** nothing (annoyance, not a blocker) —
found at M5 CP4 while adding `on_delete="RESTRICT"` backstops behind the
category-delete API guards (PR #23 review, finding 2).

## Summary

ferro maps Postgres FK violations (SQLSTATE class 23503) into its exception
hierarchy on the **save** path but not on either **delete** path. An INSERT
with a dangling FK raises `ForeignKeyViolationError` (an `IntegrityError`
subclass, as documented); the same 23503 violation triggered by deleting a
RESTRICT-referenced parent — via instance `.delete()` or filtered
`.where(...).delete()` — surfaces as bare `OperationalError`.

Callers who use `RESTRICT` as a DB-level backstop (the natural pairing with
request-time guards) cannot catch FK violations uniformly: the same
integrity failure is an `IntegrityError` on one path and an operational
error on the other. Pinch's test for the backstop is currently pinned to
`OperationalError` (`tests/test_taxonomy_models.py::
test_category_parent_fk_restricts_delete_at_the_db`) with a comment noting
it's asserting the wrong contract on purpose — it should flip to
`ForeignKeyViolationError` when this is fixed.

## Minimal repro (ferro 0.16.2, Postgres 17)

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

Flip the pinned exception in
`tests/test_taxonomy_models.py::test_category_parent_fk_restricts_delete_at_the_db`
from `OperationalError` to `ForeignKeyViolationError` and delete its
apology comment. No production code change — the API guards answer 409
before the backstop can fire; the backstop firing is a loud 500 either way.
