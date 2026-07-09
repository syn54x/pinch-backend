# Pinch owns identity and sessions; login methods are pluggable; no managed auth

Pinch is the sole identity store: it owns the user table, server-side
sessions (Postgres-backed, httpOnly cookies, argon2id password hashes),
personal access tokens, and ledger membership. Login is a narrow pluggable
step — every method terminates in the same "identity confirmed → issue Pinch
session" seam: password (v0), social OAuth (later), external OIDC for
self-hosters (someday). Managed auth services (Clerk/Auth0) were rejected
because they would own the user lifecycle, session format, login UI, and
org/invite model — breaking self-hosting and forcing ledger sharing to be
designed around a vendor's "organizations". Hosted-vs-self-host differences
(Turnstile, breach-password checks, required email verification) are config.

## Consequences

- The auth module is exempt from AI-assisted improvisation: hand-reviewed,
  boring patterns, tests required.
- JWTs are not used for browser sessions; revocable server-side sessions are.
