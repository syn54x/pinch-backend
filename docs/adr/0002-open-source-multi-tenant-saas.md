# Open-source, multi-tenant hosted SaaS with ledger-owned data

Pinch is operated as a hosted, multi-tenant SaaS, and the code is open
source: power users may self-host, bringing their own Plaid credentials and
AI keys. The schema is multi-tenant from day one because retrofitting tenancy
is among the most painful migrations there is. The tenancy unit is the
**Ledger**, not the user: all financial data belongs to a ledger and users
are members of ledgers (exactly one auto-created ledger per user in v0).
This makes post-v0 ledger sharing (households, an LLC ledger) an additive
feature — "add a member" — rather than a rewrite, and gives row-level
security a single ownership column to key on.

## Consequences

- Hosted-only behavior (bot checks, breach-password checks, required email
  verification) must be config, never forks in the code.
- Secrets handling and telemetry are written to be read: the code is public.
