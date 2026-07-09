# PRD 0003: JSONB columns

**Requested by:** Pinch • **Blocks:** provider payload storage (Plaid raw
transactions/accounts), AI proposal metadata, import profiles.

## Summary

First-class JSONB column support: declare a model field as JSONB (typed as
`dict`/`list` or a nested Pydantic model), round-trip it, and — second
priority — filter on containment/paths.

## Motivating Pinch usage

Every synced transaction keeps its raw Plaid payload for reprocessing and
audit; import profiles store a user-confirmed column mapping; proposals store
model/prompt metadata for the correction log:

```sql
CREATE TABLE "transaction" (
  ...,
  raw_payload JSONB,          -- verbatim provider data
  proposal_meta JSONB         -- provenance details, model id, confidence
);

SELECT * FROM "transaction"
WHERE raw_payload @> '{"personal_finance_category": {"primary": "TRANSPORTATION"}}';
```

## Requirements

- [ ] Declare JSONB fields on models (`dict[str, Any]` / nested `BaseModel`)
- [ ] Insert/update/select round-trip with Pydantic (de)serialization
- [ ] Migration support (auto + alembic) emits JSONB column types
- [ ] (Later) containment (`@>`) and path (`->`, `#>>`) filters
- [ ] (Later) GIN index declaration on JSONB fields

## Notes

Round-trip alone unblocks Pinch v0 — query operators can follow. Fallback of
`TEXT` + manual JSON was rejected per the block-on-ferro policy (ADR 0003).
