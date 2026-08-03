# Investment activity is a separate model, not a Transaction

Investment account events from Plaid's Investments product (buys, sells,
dividends, fees, securities transfers) are stored as `InvestmentActivity` —
a sibling model of `Transaction`, in the same context and ledger tenancy —
never as rows in the `Transaction` table. `Transaction` carries heavy law
(review obligation, proposals, the correction log, splits, transfers,
source-vs-user data, pending/posted replacement) and investment activity
obeys none of it: activities are provider-owned records with zero user
data, never reviewed, classified, split, or transfer-linked. Folding them
into `Transaction` would have forced an exclusion clause into every
consumer of that table (classification sweep, transfer detection, recurring
detection, spending reports) — one table, two laws. Monarch ships the
opposite choice (investment activity mixed into the main transactions feed,
buys/sells categorized as transfers); we deliberately follow Copilot's
holdings-centric shape instead. The two worlds touch in exactly one place:
a cash contribution into an investment account is, on the funding side, an
ordinary Transaction marked as a transfer with an untracked counterparty —
"untracked" meaning the counterpart movement is not a Transaction in Pinch
(see CONTEXT.md's Transfer entry). A full separate bounded context was also
rejected: Account, Ledger, Connection, and BalanceEntry are literally the
same rows on both sides of the line, and a context boundary would cut
through the Account aggregate.

Because activities carry zero user data, sync uses stateless mirror
semantics — each pass re-fetches Plaid's full 24-month window and makes the
stored set match exactly within it, never touching rows older than the
window start — rather than a cursor or overlap heuristic. This is safe
precisely because there is nothing user-owned to preserve; it would be
wrong for `Transaction` and becomes wrong for `InvestmentActivity` the day
it grows user-owned fields.
