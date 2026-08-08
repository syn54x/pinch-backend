# Production access inventory

Answers Plaid questionnaire Q3 (access controls on production assets) and
feeds the access-control section of `SECURITY.md` (S4). Tracked by
[#98](https://github.com/pinch-finance/pinch-backend/issues/98), parent epic
[#97](https://github.com/pinch-finance/pinch-backend/issues/97).

**Operating model:** Pinch is operated by a single person (Taylor). There are
no shared accounts, no contractors, and no break-glass users. Every system
below is reachable by exactly one human identity, plus the narrowly-scoped
machine credentials named per row. This inventory is the exhaustive list of
systems that store or process consumer financial data.

Security contact: `security@pinch.cash` (group address, monitored).

## Systems

| System | Role | Consumer financial data | Human access | Machine access | MFA |
|---|---|---|---|---|---|
| Render — web service | Runs the API | Processes all of it in memory; holds env vars incl. provider secrets | Taylor (dashboard) | Deploy via GitHub integration | ✅ enabled |
| Render — Postgres | Primary datastore | Stores all of it: accounts, transactions, balances, holdings; provider access tokens (Fernet-encrypted at the app layer, disk-encrypted at rest) | Taylor (dashboard; psql over TLS) | App via `DATABASE_URL` env var | ✅ (same Render account) |
| GitHub org `pinch-finance` | Source code + CI + deploy trigger | None stored; CI holds sandbox-only provider secrets | Taylor (`0x054`, sole member/owner) | Actions with repo-scoped tokens | ✅ enabled + required at org level |
| Plaid dashboard | Bank-data provider (production) | Provider-side copy of linked Items | Taylor | API keys held as Render env vars | ✅ enabled |
| MX dashboard | Bank-data provider (production) | Provider-side copy of linked members | Taylor | API keys held as Render env vars | ❌ not offered — see Exceptions |
| Cloudflare | DNS, TLS edge, email routing for `security@pinch.cash` | Transits all API traffic; stores none | Taylor | API token for DNS (if any) | ✅ enabled |
| Logfire | Observability / tracing | Traces may include request metadata; payload capture is limited — verify and state exactly what is captured | Taylor | Write token as Render env var | ✅ via GitHub SSO (IdP MFA); no native option |
| Pydantic AI Gateway | LLM proxy for Penny + categorization | Processes transaction text sent to the model; stores per its retention terms | Taylor | `PYDANTIC_AI_GATEWAY_API_KEY` as Render env var | ✅ via GitHub SSO (same sign-in as Logfire) |

## Access controls in force

Answer Q3's multi-select from this list only — nothing aspirational:

- Single-operator access: one human identity per system, no shared credentials.
- MFA on every dashboard account except MX (which does not offer it — see
  Exceptions); Logfire and the Pydantic AI Gateway inherit MFA through GitHub
  SSO.
- Secrets live as Render environment variables, never in the repo; CI carries
  sandbox credentials only.
- Provider access tokens are additionally encrypted at the application layer
  (Fernet AEAD, `crypto.py`) before they reach the database.
- Database reachable only over TLS; app traffic is TLS 1.2+ end to end
  (Cloudflare edge + Render).
- Deploys go through GitHub → Render integration; no manual artifact uploads.

## Exceptions

Accounts that cannot do MFA, with compensating controls:

- **MX dashboard** — MX does not offer 2FA on dashboard accounts.
  Compensating controls: unique, generated password not reused anywhere;
  account-recovery email is itself MFA-protected; MX API credentials are
  held only as Render environment variables, so dashboard compromise is the
  only exposure and it is bounded to MX's own console.

## Maintenance

Re-verify this inventory whenever a new vendor is added or an access path
changes. Last verified: **2026-08-08** (initial MFA sweep).
