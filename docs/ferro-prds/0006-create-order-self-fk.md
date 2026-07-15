# PRD 0006: CREATE TABLE ordering breaks around self-referential FKs

> **Filed:** [ferro-orm#302](https://github.com/syn54x/ferro-orm/issues/302)
> (2026-07-15).

**Requested by:** Pinch • **Blocks:** M5 CP3 — the `Proposal` table (and any
future table with a required FK to `Transaction` or `Category`) cannot be
created on Postgres.

## Summary

`auto_migrate` on a fresh Postgres schema emits `CREATE TABLE` statements in
an order that ignores FK dependency edges whenever the dependency graph
contains a self-referential FK. A referrer whose table name sorts before its
target's is created first; its inline `REFERENCES` fails with
`relation "..." does not exist`.

Every existing Pinch table survived by alphabetical luck (`transaction_tag`
> `transaction`, `rule` > `category`, `balance_entry`… all referrers happen
to sort after their required-FK targets once the cycle members are
involved). M5 CP3's `proposal` < `transaction` breaks the luck: the entire
suite fails at fixture setup the moment the model is declared.

## Minimal repro (ferro 0.16.1, Postgres 17)

```python
import asyncio, uuid
from typing import Annotated, Optional
from ferro import BackRef, Field, ForeignKey, Model, Relation, connect

class ZNode(Model):
    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    parent: Annotated[Optional["ZNode"], ForeignKey(related_name="children")] = None
    children: Relation[list["ZNode"]] = BackRef()
    referrers: Relation[list["AReferrer"]] = BackRef()

class AReferrer(Model):
    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    node: Annotated[ZNode, ForeignKey(related_name="referrers")]

asyncio.run(connect("postgres://postgres:password@localhost:5432/postgres",
                    auto_migrate=True))
# ferro.exceptions.OperationalError: SQL Execution failed for 'areferrer'
# table: error returned from database: relation "znode" does not exist
```

Remove `ZNode.parent` (the self-FK) and the same pair migrates fine — the
topological ordering works. With the self-FK present, ordering degrades to
name order for the affected component.

Verified facts (scratch-probed 2026-07-15):

- The compiled SchemaIR envelope **does** carry the edge
  (`foreign_keys: [{column: node_id, to_table: znode, …}]`) — the loss is in
  the core's CREATE ordering, not the Python-side compiler.
- In Pinch's real registry, a new model with a required FK to `Transaction`
  or `Category` fails; FKs to `User`/`Tag`/`Import`/`Ledger` order
  correctly. `Category` carries the self-FK; `Transaction` references
  `Category` — consistent with the cycle component falling out of the sort.
- Isolated pairs without a self-FK anywhere order correctly even with
  reversed-alphabetical names, including a target literally named
  `transaction`.

## Requirements

- [ ] `CREATE TABLE` order respects FK edges when the graph contains
      self-referential FKs (a self-loop is not a cycle that needs breaking —
      the table depends only on itself)
- [ ] True multi-table cycles (if ever supported) create tables first and
      add the cycle-closing constraints via `ALTER TABLE … ADD CONSTRAINT`
      afterward — or are rejected loudly
- [ ] Regression test: self-FK model + referrer whose name sorts before it,
      fresh Postgres schema

## Non-requirements

Pinch does not need multi-table FK cycles; only the self-FK case (an
adjacency-list hierarchy — `Category.parent`) plus ordinary referrers of
tables in that component.
