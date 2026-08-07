# Budgets are derived, never authored

Pinch ships no budget feature in the category's sense — no envelope,
spend band, or per-category limit the user sets and the app polices. The
questions a budget was supposed to answer (am I bringing in more than I'm
spending? are my trends shifting? where's the give this month?) are
answered by **Rhythm**: a surface derived entirely from the ledger's own
history — the typical month under the report lens (category-based,
transfers excluded), decomposed into committed spend (recurring series)
and discretionary spend, read against the current month as income →
committed → discretionary → kept. No number in Rhythm can be set,
adjusted, or pinned by the user; a control that would author a target
belongs to a future Goals concept, not to Rhythm. The law exists because
an authored budget fails Pinch's own principles twice: authoring and
maintaining spend bands is bookkeeping labor ("show finished work, not
chores"), and an authored plan's only output is a violation notice
("never nags"). Mint, Monarch, and Copilot all ship that machine; YNAB
ships envelope discipline as the entire product — a job (hard
constraints, paycheck-to-paycheck triage) Pinch deliberately does not
serve.

Delivery obeys the same stance. Departures from rhythm surface as
findings — a bill change on a recurring series, a sustained discretionary
trend, or an outlier expense held out of what the typical month learns —
and findings are encountered on surfaces (dashboard, the Rhythm page,
Penny), never pushed as notifications. Surfaces state facts, including
the Harmony section's recovery math ("tracking $412 over typical; the
give is in Dining"); prescriptive advice exists only in Penny, on the
user's initiative, grounded through public developer-API endpoints per
the parity law.

Rejected alternatives: a user-adjustable baseline (an envelope in
Rhythm's clothes — the first authored number breaks the no-authoring law
everywhere); the cash lens for the headline number (counting an
investment contribution as money out punishes saving, and the category
lens keeps one definition of "spending" across Rhythm and reports —
accepting the known blind spot that a loan payment's interest portion,
buried in a transfer, is invisible to spending); and folding goals in now
(a goal is a legitimately user-authored destination measured against the
derived trajectory — a different concept, deferred whole rather than
smuggled in). The full concept, user stories, and vocabulary live in the
F9 PRD: https://github.com/syn54x/pinch-frontend/issues/78.
