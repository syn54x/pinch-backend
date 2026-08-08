# Encryption & transit evidence pack

Answers Plaid questionnaire **Q6** (encryption in transit, TLS 1.2+) and
**Q7** (consumer data from the Plaid API encrypted at rest), and feeds the
encryption section of `SECURITY.md` (S4). Tracked by
[#100](https://github.com/pinch-finance/pinch-backend/issues/100), parent epic
[#97](https://github.com/pinch-finance/pinch-backend/issues/97).

Both answers are **Yes**. This document is the evidence behind the claims, so
that if Plaid asks what backs them the answer is a citation, not a
recollection. Code citations are `path:line` against the commit this document
ships in.

## The boundary — state it before answering

Two different things are encrypted by two different mechanisms. Conflating
them would overstate the claim, so the answer names each precisely:

| Data | What it is | Protected by |
|---|---|---|
| **Provider secrets** | Plaid access tokens (and any per-connection provider secret) | **Application-layer Fernet AEAD** (`crypto.py`) **and** the database's at-rest disk encryption underneath |
| **Consumer financial data** | Transactions, balances, account names and masks, holdings | The database's **at-rest disk encryption** |

The Fernet envelope covers **provider secrets only**. Consumer financial data
itself is protected by database at-rest encryption, not by the Fernet layer.
Both are true; we do not claim the Fernet envelope wraps the transaction data.

---

## Q7 — Consumer data encrypted at rest

### Layer 1 — Database disk encryption (covers all consumer financial data)

Render Postgres stores the entire domain graph — accounts, transactions,
balances, account names and masks, holdings — and encrypts its storage at
rest.

> **Vendor citation — operator to capture.** Render does not publish a
> per-instance attestation; capture the current published claim from Render's
> security/compliance documentation (data encrypted at rest, and the cipher
> where stated), with the URL and the date retrieved. Paste it here:
>
> - Source URL: `‹TODO: Render security docs URL›`
> - Claim (verbatim): `‹TODO›`
> - Retrieved: `‹TODO: YYYY-MM-DD›`
>
> This is the one at-rest claim that rests on a vendor statement rather than
> on our own code, which is exactly why it needs a dated citation.

### Layer 2 — Application-level Fernet AEAD (covers provider secrets)

Provider access tokens are encrypted **by the application, before the row is
written** — a second layer most applicants do not have, and worth stating
explicitly.

- **Mechanism.** Fernet (AES-128-CBC + HMAC-SHA256, authenticated, versioned
  tokens). `encrypt_secret` / `decrypt_secret` in `src/pinch_backend/crypto.py:25-30`.
- **Key source.** `settings.secret_encryption_key`
  (`PINCH_SECRET_ENCRYPTION_KEY`), a `Fernet.generate_key()` value held only as
  a Render environment variable, never in the repo
  (`src/pinch_backend/settings.py:198-201`).
- **Fail-closed at startup.** When Plaid is configured the key is *required*;
  the app refuses to boot without it, so a half-configured instance cannot
  discover the gap when a user first links a bank
  (`src/pinch_backend/settings.py:222-228`).
- **Rotation path.** v0 is single-key. Rotation is the documented `MultiFernet`
  upgrade — decrypt with old keys, encrypt with the new — with no machinery
  built until it is needed (`src/pinch_backend/crypto.py:1-10`,
  `settings.py:199-201`).
- **What is stored.** The ciphertext lives in `Connection.encrypted_secret`
  (`bytes`), `src/pinch_backend/models.py:369`. MX mints no per-connection
  secret, so there is nothing to encrypt on that provider and no key is
  required for it (`settings.py:207-212`).

### Write-only at the API surface

The plaintext token is written once (encrypted) and is **never returned** in a
response, a log line, or `error_detail`:

- The connections API serialises through an explicit allowlist DTO,
  `ConnectionOut` — *"an allowlist, never the row, and never the access token
  in any form"* (`src/pinch_backend/api/connections.py:106-128`).
  `encrypted_secret` is not a field on it.
- Every read of the secret is an internal decrypt handed straight to a
  provider client (sync, reconcile, erasure, CLI export) — never a response
  body. Usage sites: `api/connections.py:236,443`, `sync.py:458,707`,
  `reconcile.py:114`, `erasure.py:136`, `cli/app.py:345`.

### Q7 draft answer (for S9)

> **Yes.** Consumer data received from Plaid is encrypted at rest. It is held
> in a Render-hosted PostgreSQL database whose storage is encrypted at rest by
> the platform [Render citation]. In addition, Plaid access tokens are
> encrypted by the application before they are written, using Fernet
> authenticated encryption (AES-128-CBC + HMAC-SHA256) with a key held only as
> a deployment environment variable; those tokens are write-only at the API
> surface and are never returned in any response, log, or error field. The
> application-layer envelope covers provider secrets; the transaction, balance,
> and account data itself is covered by the database's at-rest encryption.

---

## Q6 — Encryption in transit (TLS 1.2+)

### Transport floor at each public edge

All public traffic terminates TLS at the Cloudflare edge (DNS + TLS for the
`pinch.cash` properties) and at Render (the API service). The database is
reached only over TLS (`docs/security/access-inventory.md`).

> **Empirical verification — operator to capture.** The dashboard default is
> not evidence; confirm that TLS below 1.2 is actively *refused* at each public
> hostname. Run the checks in the appendix against the live production edges
> and paste the results:
>
> | Public edge | Hostname | TLS 1.0/1.1 refused? | TLS 1.2 offered? | Checked (date) |
> |---|---|---|---|---|
> | Marketing site (Cloudflare) | `‹TODO›` | `‹TODO›` | `‹TODO›` | `‹TODO›` |
> | App frontend (Cloudflare→Render) | `‹TODO›` | `‹TODO›` | `‹TODO›` | `‹TODO›` |
> | API (Render) | `‹TODO›` | `‹TODO›` | `‹TODO›` | `‹TODO›` |
>
> Do not tick Q6 as evidenced until sub-1.2 is confirmed *refused*, not merely
> "1.2 available".

### Application-layer transit posture (verifiable in code now)

The transport floor is backed by app-layer choices that keep credentials from
crossing an untrusted channel or origin:

- **Secure session cookie.** `session_cookie_secure` defaults `True`
  (`settings.py:96`); the session and clear-session cookies are set `secure`,
  `httponly`, `samesite=lax` (`auth/sessions.py:60-81`). The session secret
  leaves the server exactly once, in that cookie; the database only ever stores
  its hash.
- **Credentialed CORS pinned to exact origins — never `*`.** The credentialed
  CORS allowance is the single configured frontend origin and nothing else
  (`api/app.py:168-172`, rationale at `164-167`). `allow_credentials=True`
  makes a wildcard origin impossible by spec.
- **CSRF enforced on every unsafe cookie request.**
  `CredentialAwareCSRFMiddleware` applies Litestar's double-submit check to all
  cookie-credentialed unsafe requests; its CSRF cookie inherits
  `cookie_secure=session_cookie_secure` and `samesite=lax`
  (`api/app.py:182-190`). Bearer-token requests are exempt *by construction* —
  a cross-site caller cannot attach an `Authorization` header — and the
  exemption is sound only because the credential resolver is
  bearer-wins-and-fails-closed (`auth/csrf.py:1-32`).

### Q6 draft answer (for S9)

> **Yes.** All traffic between clients and our servers is encrypted with TLS
> 1.2 or better. Public endpoints terminate TLS at Cloudflare and at Render;
> TLS below 1.2 is refused at each edge [empirical results]. Session cookies
> are issued `Secure` + `HttpOnly` + `SameSite=Lax`; credentialed CORS is
> restricted to an exact origin (never a wildcard); and CSRF protection is
> enforced on all cookie-credentialed state-changing requests. The database is
> reached only over TLS.

---

## Appendix — TLS verification commands

Run from any host with network egress to the production edges. For each public
hostname, TLS 1.0 and 1.1 must **fail to connect** and TLS 1.2 must succeed.

```sh
HOST=example.pinch.cash        # repeat per public edge

# Must FAIL (connection refused / handshake failure) — sub-1.2 not allowed:
openssl s_client -connect "$HOST:443" -tls1   </dev/null 2>&1 | grep -Ei 'protocol|handshake failure|no protocols'
openssl s_client -connect "$HOST:443" -tls1_1 </dev/null 2>&1 | grep -Ei 'protocol|handshake failure|no protocols'

# Must SUCCEED — TLS 1.2 (and ideally 1.3) offered:
openssl s_client -connect "$HOST:443" -tls1_2 </dev/null 2>&1 | grep -Ei 'protocol|cipher'
openssl s_client -connect "$HOST:443" -tls1_3 </dev/null 2>&1 | grep -Ei 'protocol|cipher'
```

For an independent second read, an SSL Labs report (`ssllabs.com/ssltest`) or
`nmap --script ssl-enum-ciphers -p 443 "$HOST"` grades the same floor; attach
whichever is easiest to capture.

## Maintenance

Re-verify the empirical TLS floor and refresh the Render at-rest citation
whenever an edge, TLS policy, or hosting provider changes. The code-derived
claims are pinned to `path:line` and should be re-checked if those files move.

Last updated: **2026-08-08** — code/config claims verified; the two vendor /
empirical slots above remain for the operator to capture.
