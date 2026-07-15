# Postgres is the only datastore; ferro-orm is the only domain data access

One Postgres instance is the system of record for everything: relational
domain data, raw provider payloads (JSONB), full-text transaction search
(PG FTS/trigram), and the job queue. No Redis, no OpenSearch, no document
store — personal-finance data is relational to its bones (splits →
transactions → accounts → connections → ledgers) and money wants ACID, and
at realistic scale (~10k transactions/user/year) nothing here outgrows a
B-tree on `(ledger_id, date)`.

All domain data access goes through **ferro-orm**, with a deliberate policy:
when ferro lacks a capability Pinch needs (joins, aggregations, JSONB, …),
**Pinch blocks and files a PRD on ferro's issue board** rather than working
around it. Pinch is ferro's forcing function; drafts live in
`docs/ferro-prds/`. Third-party infrastructure that manages its own tables
(e.g. the job queue, alembic) is exempt — the policy governs Pinch's domain
data only.

## Considered options

- OpenSearch for transaction search — rejected: operational weight for a
  problem PG FTS covers for years.
- Raw-SQL reporting layer alongside ferro — rejected in favor of blocking on
  ferro, accepting that ferro's roadmap is Pinch's critical path.

## Amendment (M5 CP3, 2026-07-15)

sqlite dev/test support is retired. Procrastinate (ADR-0006, pulled forward
to M5) made Postgres load-bearing for the product's core loop, and CP3's
concurrency guarantees are untestable on a single-writer backend. Postgres
is dev, test, and CI; the dev default DSN matches the local-pg container.
