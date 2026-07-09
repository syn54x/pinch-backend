# Procrastinate (Postgres-backed) for background jobs

Background work (Plaid webhook syncs, categorization batches, nightly
balance snapshots, valuation refreshes, import processing, outbound webhook
delivery, email) runs on Procrastinate: a Postgres-native task queue using
LISTEN/NOTIFY, with retries, periodic scheduling, and job locks (e.g. never
two concurrent syncs per connection). Chosen over SAQ/arq/Celery because
those require Redis, and the self-host promise is "one Postgres, nothing
else"; chosen over a hand-rolled poller because retries/locking/scheduling
are exactly the parts that get hand-rolled badly. Deployment shape: one API
process, one worker process, one Postgres. Procrastinate manages its own
tables and connections — infrastructure, so exempt from the block-on-ferro
policy (ADR 0003).
