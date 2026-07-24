# M5 CP1 — subagent-driven execution ledger

Plan: docs/superpowers/plans/2026-07-13-m5-cp1-taxonomy-tags-transactions.md
Branch: m5-classification

## Tasks
Task 1: complete (commits 4997a1d..ff50815, review clean after 2 fixes: co-author trailer added, docs/superpowers gitignore reverted)
Task 2: complete (commit 22a247f, review clean; 1 minor diff-readability note, no defect)
Task 3: complete (commit 47c057b, review clean; full suite 215 green, trailer verified)
Task 4: complete (commit 48bbc70, review clean; 2 minors logged)
Task 5: complete (commit 7f850a1, review clean; atomicity + 12/28 verified, trailer ok)
Task 6: complete (commit a6d5eda, review clean; keyset predicate verified, trailer ok)
Task 7: complete (commits 39dc7e4 + fix 7fc5556, review clean after 1 Critical/1 Important/1 depth-gap fix; full suite 232 green, trailer ok)
Task 8: complete (commit 7d41113, review clean; 2 minors logged)
Task 9: complete (commit bbd5c02 + test fix 731ee6a, review clean after Important coverage gap fixed; impl verified correct by reviewer; full suite 241 green, trailer ok)
Task 10: complete (commit 10b2c8a + fix 03eeeed, review clean after 2 Important fixes: PATCH atomicity + tag-detach test; full suite 250 green, trailer ok)
Task 11: complete (250 green both backends; ruff+ty clean; pushed; draft PR #23 opened). CP1 DONE.

## Minor findings (for final review triage)
- Task 4 taxonomy.py: bare `# ty: ignore` used; codebase convention is scoped `# ty: ignore[unresolved-attribute]` (accounts.py, guards.py, models.py). Fix in final polish.
- Task 4 taxonomy.py:53 validate_placement: `ledger_id` param unused (plan-mandated snippet). Harmless; consider removing or commenting that parent ownership is the caller's job.

## Notes
- Task 8 minor (brief-verbatim, low priority): POST /tags is check-then-act — concurrent same-name creates could 500 (UniqueViolationError) instead of 409. Single-tenant/low-concurrency; note for whoever adds ON CONFLICT handling.
- PLAN DOC BUG to fix at wrap: Task 7 in the plan has `category.parent = new_parent` (should be `category.parent_id = ...`) and lacks the subtree-height depth check. Shipped code is correct (7fc5556); the plan doc still shows the buggy snippet. Patch docs/superpowers/plans/2026-07-13-...md before final PR.

# M5 CP2 — subagent-driven execution ledger

Plan: docs/superpowers/plans/2026-07-14-m5-cp2-rules.md

## CP2 Tasks
CP2 Task 1: complete (commit 6220bd2, review clean, trailer ok)
CP2 Task 2: complete (commit e665994, review clean, trailer ok)
CP2 Task 3: complete (commits d19e937 + d70d73f, review clean after zero-amount/day-bounds tests pinned)
CP2 Task 4: complete (commit 591c219, review clean; sign arithmetic hand-verified)
CP2 Task 5: complete (commit 5297ba0, review clean, trailer ok)
CP2 Task 6: complete (commit f11df95, review clean, trailer ok)
CP2 Task 7: complete (commit c513538, review clean; plan bug fixed: preview answers 200 not 201)
CP2 Task 8: complete (commit 75a24e8, review clean, trailer ok)
CP2 Task 9: complete (gate 294 green both backends; polish 4e9ba06; pushed; PR #23 body updated). CP2 DONE.

## CP2 Minor findings (for final review triage)
- Task 6: _out per-rule Category.get is N+1 on list_rules (low cardinality, acceptable; batch if it grows).
- Task 6: test_patch_replaces_condition_whole assertion is vacuous (exclude_none drops keys; the dict-comprehension guard defeats it). Strengthen to `assert "payee" not in body["condition"]` in final wave.
- Task 4: narrow() currency clause doesn't defend clause.currency=None (matches() raises loudly; narrow would silently zero-row). Unreachable today (API fills currency); consider mirroring the ValueError in narrow() for defense-in-depth.
- Task 3 report file has a garbled coverage line (cosmetic, report-only).

## CP2 Notes

# M5 CP3 — subagent-driven execution ledger

Plan: docs/superpowers/plans/2026-07-15-m5-cp3-pipeline.md
Branch: m5-classification (PR #23)

## CP3 Tasks
CP3 Task 1: complete (commit 0c06d8c, review clean; 294 green on Postgres; minor: brief grep-wording nit, resolved — explanatory sqlite mentions intended)
CP3 Task 2: BLOCKED on ferro-orm#302; fix PR ferro-orm#303 REVIEWED (comment left) and VERIFIED from pinch: minimal repro passes, Task 2 models migrate, 298/298 full suite green on the PR branch. Corrected impl+tests in stash "cp3-task2-models" (old stash was polluted by bisection edits — dropped; current one is the verified version; FK spelling is plain ForeignKey(related_name="proposals", unique=True), NO nullable=False). PRD docs/ferro-prds/0006 (462b1b1).
CP3 PARKED awaiting ferro#303 merge + release. Resume: bump ferro-orm floor in pyproject -> uv sync -> git stash pop -> full suite green -> commit Task 2 per brief Step 5 -> review as normal -> Tasks 3-11. Do NOT re-implement Task 2.
CP3 Task 2: complete (commit 9750f45 after ferro 0.16.2 bump f9f6907, review clean, trailer ok)
## CP3 Minor findings (for final review triage)
- Task 2: Proposal.category FK unindexed — matters if category delete-block ever checks live proposals (Task 11 only bulk-updates); consider index in final polish.
- Task 2: CorrectionLogEntry.voids unindexed — undo does voids.in_(batch), fine at current scale; note for void-lookup queries later.
- Task 2 (brief-inherited): naming asymmetry provenance_detail (Proposal) vs proposal_detail (CorrectionLogEntry).
CP3 Task 3: complete (commit 8340ca0, review clean; minor: `# noqa: TC003` on `import uuid` in classifier.py/history.py — codebase convention is TYPE_CHECKING block + quoted annotations (fingerprint.py, taxonomy.py, evaluator.py precedent); fix in final polish)
CP3 Task 4: complete (commit 5c7e7c0, review clean; trailer verified by controller)
- Task 4 minor: decision_tags logs raw caller list, not casefold-deduped applied set — divergence possible with case-variant dupes; note for CP4 review payloads.
- Task 4 minor (brief-inherited): no non-vacuous test that consume leaves `notes` untouched; close in CP4.
- Task 4 minor: consume.py defers uuid to TYPE_CHECKING but imports Transaction eagerly though also annotation-only.
CP3 Task 5: complete (commits 3721757 + fix eba58f3, review clean after 1 Important fix: reviewed_at freshness re-check inside the write tx + deterministic race test; suite 314 green)
- Task 5 minor (residual): freshness check is plain SELECT not FOR UPDATE — microsecond TOCTOU window remains, documented in-code; consider atomic conditional insert if invariant ever needs airtight.
- Task 5 minor: phase-1 walk rescans all unreviewed txns (Python-side proposal filter, no SQL anti-join — ferro capability); auto-file phase is per-txn queries not batched. Both brief-inherited; scale notes only.
- CP4 semantics note (from Task 5 review): PATCH reviewed=true leaves an attached Proposal row undeleted (no race needed) — CP4's review/un-review design should decide whether PATCH also consumes/clears.
CP3 Task 6: complete (commit d873fde, review clean; trailer verified; check_connection polarity verified against procrastinate source by reviewer)
- Task 6 minor: job_app singleton binds settings.database_url at import — latent coupling if a test ever uses it before the autouse fixture; note only.
CP3 Task 7: complete (commit dba00db, review clean; trailer verified by reviewer; 319 green + 3 staged xfails)
- Task 7 minor: CommitIn (like the codebase's other In models) silently ignores unknown body fields; extra="forbid" would be a repo-wide policy decision, not CP3's.
CP3 Task 8: complete (commit 44bdc6a, review clean; 323 green + 1 staged xfail)
- Task 8 minor: hydrate_transactions docstring says "a fixed number of queries" — name the count in final polish.
CP3 Task 9: complete (commit b70ea7e — amended from 94f2424 by controller to fix the one Important: co-author trailer said Sonnet not Fable; verified by inspection, code untouched. Review otherwise clean; 326 green, zero xfails remain)
- Task 9 minors: no multi-page cursor test on this endpoint (paginate covered in conventions tests); no combined-filter test. Coverage polish only.
CP3 Task 10: complete (commit d8f7220, review clean; trailer verified verbatim; 328 green)
- Task 10 minor: inline comment above the retraction block still says "forward contract" while the docstring was reworded — align in final polish.
CP3 Task 11: complete (commits 68e40a3 + docstring polish 3ce4779, review clean; trailers verified; 330 green, prek green)
All 11 CP3 tasks done — final whole-branch review next.
CP3 final review (fable): 1 Important (auto-file freshness guard — phase-2 mirror of eba58f3) + 4 fix-before-push minors; fixed in 01a063a + 5c8d5c5; re-review verdict: READY TO MERGE. Accepted warts recorded: PATCH reviewed=true leaves proposal (CP4 design input, in PR body); coverage nits (auto-file-feeds-history test, log multi-page cursor) → CP4.

# M5 CP4 — subagent-driven execution ledger

Plan: docs/superpowers/plans/2026-07-16-m5-cp4-review-promotion-manual-entry.md
Branch: m5-classification (PR #23)

## CP4 Tasks
CP4 Task 1: complete (commit 7555b77, review clean; 349 green; trailer verified verbatim by reviewer)
- Task 1 minors: unneeded ty-ignore on test rule.action_category_id; _MAX_PAYEE_CONDITION_LENGTH duplicates PayeeCondition max_length=200 (drift nit); no empty-payee-branch test (brief-inherited)
CP4 Task 2: complete (commit b35b673, review clean; 359 green; trailer verified; optional body worked, no fallback needed)
- Task 2 minors: no explicit-null-clear test (category_id/tags/display_name null); data-is-None bodyless branch untested (tests always send json={})
CP4 Task 3: complete (commit a50aa98, review clean; 364 green; trailer verified)
- Task 3 minors: no scope/CSRF test on the batch route itself; mixed-category-same-payee-in-one-batch overwrite semantics untested; per-item consume N+1 (spec-mandated own-transaction, perf note only)
CP4 Task 4: complete (commits dea3a10 + fix 6900dd4, review clean after 1 Important fix: no-op PATCH reviewed:true bumped reviewed_at — write is now transition-only, pinned RED->GREEN; carry-forward-category test added; 371 green)
- Task 4 minor (residual): pure no-op PATCH still runs an identical-values txn.save() + transaction.updated event — consistent with other value-unchanged PATCHes, no action
CP4 Task 5: complete (commit 44f7ea2, review clean; 379 green; fingerprint parity with imports verified by reviewer at argument/type/unit level; trailer verbatim)
- Task 5 minors: no forced-mid-consume-failure test pinning create+consume atomicity (comment-only reasoning); scope-403 test duplicates payload inline
CP4 Task 6: complete (commit 8da6de5, review clean; 384 green; all four CP3 debts pinned non-vacuously; delete-vs-dismiss docstring names the real mechanism; trailer verbatim)
- Task 6 minor: correction-log tests' _ledger_for() relies on default signup email — hygiene note only
CP4 Task 7: complete (commit c296fd0, review clean; flywheel e2e passed unmodified on first run — byte-exact transcription verified; 385 green)
CP4 Task 8: complete (gate 385 green + ruff + ty + prek all green first try; PR #23 CP4 box ticked, CP4 section appended). Final whole-branch review next.
CP4 final review (fable): READY TO PUSH — zero Critical/Important. 1 pre-push doc fix (spec said 422 for batch cap; impl+repo convention is 400 — spec corrected). Accepted residuals recorded: freshness re-check absent at review-side consume seam (move transition-guard into consume_proposal — future hardening, same TOCTOU class CP3 documented); double proposal fetch in review paths (same class); TransactionCreateIn amount_minor plain int vs accounts' StrictInt (align in later polish); review-vs-PATCH accept asymmetry (spec-locked, both surfaces correct). All 8 task minors triaged: accept (3, 4 logged for CP5).

## PR #23 review response (0x054's 2026-07-16 comment)
All 19 findings addressed in one combined commit (4 sequential fix groups, per-group targeted tests, combined gate 424 green):
- Critical 1: reassign-to-self 409 guard + regression test (transactions survive)
- Major 2: on_delete SET NULL (Transaction.category, Proposal.category) / RESTRICT (Category.parent, Rule.action_category), DDL empirically verified; ferro gap noted: RESTRICT violation via instance .delete() surfaces OperationalError not an IntegrityError subclass (candidate ferro issue)
- Major 3: narrow() skips contains-narrowing on backslash too; equals always narrows (== not LIKE); docstring de-sqlite'd
- Major 4: consume_proposal CAS claim (UPDATE ... WHERE reviewed_at IS NULL) + AlreadyReviewedError; callers translate (409/skip/continue); orphan-proposal residual documented in pipeline phase-1 comment (accepted)
- Minors/nits 5-19 all fixed: delete-reassign e2e tests, sibling-name trim+casefold, tags blank-name 400, payee normalize-nonempty, spec extra=forbid, Transaction.category index, amount_minor int4 bounds, API-startup ensure_job_schema, tri-state pinned/aligned (txn tags null-clears; review null-display=corrected; rules null add_tags clears + null status 400s), extra=forbid on txn body models, status=proposed fabrication 400, stale comments fixed, preview-cap + tampered-cursor tests

# M6 — execution ledger

PRD: #24 • Branch: m6-transfers-splits (PR #30) • Sessions started 2026-07-16

## CP0 (#25): ferro scratch-verification spike
CP0: complete (findings comment posted 2026-07-16; scratch scripts run against Postgres 18 and deleted). Verdict 2/4:
- cap 1 (two nullable unique FKs on one model): VERIFIED — unique=True per FK, enforcement via per-column unique index, UniqueViolationError on occupied re-reference, NULLs don't collide. Note: ferro on_delete defaults to CASCADE — CP2's Transfer model should spell its choice explicitly.
- cap 2 (EXISTS membership from Transaction root): MISS — BackRefs never enter __ferro_relation_specs__ (forward-FK-only traversal); in_() rejects subqueries. PRD 0008 → ferro-orm#307.
- cap 3 (OR across left-joined child): MISS — same root cause. PRD 0009 → ferro-orm#308.
- cap 4 (on_delete=CASCADE): VERIFIED — DDL emits ON DELETE CASCADE; lines die with their transaction.

## Slice status after CP0
- CP1 (#26): BLOCKED by ferro-orm#308 (native edge wired, db id). Do not start; no id-materialization workaround (ADR-0003).
- CP2 (#27): BLOCKED by ferro-orm#307 (native edge wired). Same rule.
- CP4 (#29): additionally blocked by ferro-orm#307 (history-stage extension) — edge wired.
- Resume protocol (the CP3/ferro#302 precedent): when ferro ships, bump the floor in pyproject → uv sync → re-run the two upstream-issue repros to confirm the verified spelling → proceed with CP1/CP2 TDD. Both upstream issues carry runnable repros.

## Resume 2026-07-18: ferro 0.17.0 unblocked CP1/CP2
- ferro 0.17.0 shipped PRDs 0008/0009 as existence tests (.exists() on reverse/M2M relations, correlated EXISTS, root-shaped) + uniform ~ negation; ferro-orm#307/#308 closed. Floor bumped (d1fd6f4), both repros re-verified by scratch on Postgres 18; re-verification comment on #25.
- Verified spellings now in use: is_transfer = t.transfer_out.exists() | t.transfer_in.exists() (false via ~); line-aware category filter = (t.category_id.in_(ids)) | (t.split_lines.exists(lambda ln: ln.category_id.in_(ids))). Reverse relations are tested-never-traversed (t.lines.category_id stays a build error); in_(subquery) and left_join-on-reverse stay loud TypeErrors.
- CP1 (#26): complete (commit 314c709; 12 tests in test_splits_api.py; suite 436 green). Splits document 400s (repo convention — issue named no codes); DELETE on unsplit txn = 404; MAX_SPLIT_LINES=100 cap (bounded-input stance, unspecced — flag in review); memo max 500.
- CP2 (#27): complete (commit 8eaa3ba; 10 tests in test_transfers_api.py; suite 446 green). Pair-shape rejections 422 (issue-specified for same-sign; extended to the class: unequal magnitude, mixed currency, same account, zero amount, duplicate id); occupied 409 (pre-check + UniqueViolationError race catch); Transfer FKs explicit ON DELETE CASCADE (dissolution backstop; CP3 wires reopen-the-survivor); GET /transfers uses uuid7 keyset paginate with left_join both sides for either-side account_id; TransferKind lives in api/transfers.py (derived, never stored) — CP3's decision_transfer imports it from there.
- Deliberately NOT here (CP3 #28): split×transfer 409 both directions, review-body splits/transfer, consume awareness, log columns, undo wiring beyond the DB CASCADE.
- Suite 446 green at push (424 base + 12 splits + 10 transfers). PR #30 updated.

## Session 2026-07-18 (continued): CP3 + CP4
- CP3 (#28): complete (commit 3ba9a5e; 10 tests in test_review_splits_transfers.py; suite 456 green).
  Key design: consume_proposal is STATE-AWARE — reads the txn's split/transfer state inside its
  transaction, forces category None, snapshots decision_splits/decision_transfer (JSONB,
  names-not-FKs, ids as strings). All consume callers inherit awareness. Counterpart review wraps
  establish_transfer + two consumes in one outer transaction; AlreadyReviewedError on either side
  rolls the whole motion back to a 409. Session decisions (flag in review):
  - Already-reviewed counterpart → 409 pointing at POST /transfers (strict; PRD presumed both unreviewed).
  - Review-with-splits document failures reuse the PUT rules → 400; mutual-exclusivity conflicts → 422
    (issue-specified). Exclusivity counts non-null values (explicit category_id:null + transfer is legal).
  - Undo voids the survivor's transfer decision entries even if the survivor was un-reviewed meanwhile;
    reopen (reviewed_at NULL) only applies when it was reviewed; a re-classify job is deferred when
    any survivor reopened.
- CP4 (#29): complete (commit f4a81af; 9 tests in test_transfer_flywheel.py incl. the milestone
  acceptance e2e; suite 465 green). NOT cut — promotion shipped. Session decisions (flag in review):
  - action_mark_transfer + action_category on ONE rule → 400 (self-contradictory law; cross-rule
    precedence is the pipeline's).
  - Zero-amount txns never get transfer proposals: the rule clause skips them (falls through to
    category stages) and the history transfer signal is likewise unproposable onto them.
  - consume(apply_proposed_transfer=True) on review accept paths, batch, auto-file; False on PATCH
    and when the user's final word was an explicit category. Since-split/since-linked accepted
    WITHOUT a transfer (exclusivity respected, one-round-trip TOCTOU residual documented in-code,
    same class as pipeline phase-1).
  - Accept-of-transfer-proposal reports result=accepted; any other shape mismatch (incl. proposed
    transfer but decided category, or linked instead of untracked) reports corrected.
  - Promotion transfer votes: every standing vote must be kind=untracked; linked filings are
    deviations for transfer promotion (not just categories/splits).
- M6 COMPLETE pending review: all 5 CPs on PR #30, 465 green, pushed. Closes #25–#29 at merge.
- MERGED 2026-07-18: PR #30 rebase-merged to main (bfc3a88..f4a81af); #25-#29 closed; branch deleted.

# M7 — Plaid connections & sync (PRD #31, CP issues #32–#36)

Branch: m7. Delivery: single PR, one slice at a time, human verification between slices.

- CP0 (#32): complete (spike, findings on issue; ferro drafts 7b2ded7). 3/4 capabilities pass.
  Two ferro misses filed per ADR-0003: ferro-orm#324 (one-shot columns+unique migration
  orders index before columns → boot crash; gates CP2 schema) and ferro-orm#325 (on_delete
  alteration on existing FK silently ignored — DB keeps CASCADE while model says SET NULL;
  gates CP1 disconnect). Fixes happening in a separate ferro thread; Taylor signals when done.
- CP1 (#33): complete minus disconnect (commits 162d6c7 + review fixes 8ecf4de; suite 486
  green; ty clean). Settings (plaid_* + secret_encryption_key, loud half-config failure),
  crypto.py Fernet, providers.py seam + owned httpx Plaid client (wire tests via
  MockTransport), api/connections.py (link-token, exchange→Connection+Accounts atomic,
  list/detail health surface), keyless 403 on Plaid-touching endpoints only. Review run
  (standards+spec agents): fixed ProviderError recovery point (400 INVALID_PUBLIC_TOKEN /
  502 else), ledger-OWNER currency fallback (not acting user), Literal plaid_environment,
  account_out promoted public, wire coverage added. Rejected: SyncProvider rename (CP2 makes
  it honest; ConnectionProvider collides with models enum), remove-item (dead code until
  disconnect unblocks), per-call httpx client (revisit at CP2 volume).
  Archived-audit artifact: no aggregate balance surface exists pre-M8; nothing double-counts;
  M8's net-worth MUST exclude archived accounts (binding note, recorded on #33).
  AWAITING SIGN-OFF: keyless list/detail answer 200-empty (deliberate deviation from
  "every connection endpoint refuses"); plaid_country_codes setting (unspecced config).
- CP1 disconnect retrofit: complete after ferro 0.17.1 delivered #324+#325 (verified via
  re-run scratch harnesses; floor bumped). Account.connection now on_delete="SET NULL"
  (models.py), provider remove_item on seam+client+wire test, DELETE /connections/{id}:
  revoke-then-sever ordering, ITEM_NOT_FOUND treated as success (idempotent from the
  client's seat), transient revocation failure → 502 and nothing severed. Suite 492 green.
  CP1 fully done; initial-sync auto-enqueue remains CP2's retrofit.
- CP2 (#34): complete (commits edf5328 + review fixes c96fcca; suite 508 green; ty clean).
  Schema: Transaction.provider_transaction_id + pending (composite unique per account,
  NULLs distinct so imported/manual rows coexist), Connection.sync_cursor,
  BalanceSource.PROVIDER. sync.py engine (trigger-agnostic; provider calls first, all
  writes one transaction, classify deferred post-commit by the jobs wrapper);
  sync_connection task (queue=sync, lock=sync:{id}, retry 5 exp; final_attempt via
  context.job.attempts). Provider: sync_transactions drains has_more pages, persists
  cursor only after batch applies (replay-safe via existing-provider-id skip);
  Plaid sign flip + exponent-aware minor units (Decimal); update-mode link tokens.
  Endpoints: POST /{id}/sync (202), auto-enqueue on create, link-token repair mode
  (409-ish guard when no credentials). TransactionOut.pending exposed.
  Review fixes: httpx transport faults + non-JSON bodies now funnel into ProviderError
  (NETWORK_ERROR / HTTP_<status>) so exhaustion can't leave a stale-active connection;
  _record_broken helper; pydantic dataclass (runtime uuid import — pydantic resolves
  annotations at runtime, TC003 push broke it); lock-serialization, keyless-refresh,
  refresh-lands-new-txn tests added.
  Known boundary (in module docstring): unknown provider accounts skipped+logged,
  cursor advances — adopting a later-added bank account needs a cursor reset (M8+).
  Exhaustion path tested at run_sync seam (InMemoryConnector can't fast-forward
  scheduled retries). Live sandbox smoke opt-in (PINCH_PLAID_CLIENT_ID/SECRET).
- CP3 (#35): complete (commit 56cde07 + review fixes; suite 515 green).
  pinch_backend/retraction.py: import-undo's dissolution machinery extracted verbatim
  (dissolve → proposals → void → delete-last; import tests pin behavior) and shared with
  sync-removed (actor=AUTO — enum has only USER/AUTO, no SYSTEM). run_sync applies:
  rewrites first (posted-replaces-pending via pending_provider_transaction_id match,
  swallowing the paired removal; modified rewrites, unseen-modified upserts as insert —
  replay hardening, unspecced, flag for sign-off), then true removals via the seam, then
  inserts. rewrite_in_place: amount unchanged → source fields only (user data/links/review
  stand; date drift cosmetic); amount changed → lines deleted, transfer dissolved with the
  TARGET EXCLUDED from dissolve accounting (its reopen + full void handled locally — the
  double-count fix from review), decisions voided reason "amount changed by provider
  sync", proposal deleted, reviewed nulled, re-classified.
  Review findings triaged: fixed double-counted reopened + SyncOutcome ConfigDict + four
  test gaps (tags/notes/display-name survival, equal-amount transfer-link + date-drift
  survival, append-only decision-still-stands assertion). Accepted as convention: fake/
  helper duplication across test files (self-contained test files are the repo's pattern).
  Deferred to CP4: run_sync length restructure (CP4 reshapes it anyway).
- CP4 (#36): complete (commits 845227e + review fixes; NOT cut — same as M6's promotion).
  classification/detection.py: post-classification pass in classify_ledger (sync/import/
  manual all funnel through the job). Mutual-uniqueness-or-silence matching (both
  directions checked — asymmetric ambiguity stays silent), ±5-day window, reviewed rows
  are candidates but only unreviewed sides receive proposals. Proposal gains
  counterpart_transaction FK (CASCADE backstop) + ProposalProvenance.DETECTION;
  ProposalOut exposes counterpart_transaction_id. Overwrite preserves contributed
  tags/rename. consume: linked create for counterpart proposals (degrade-not-error when
  counterpart turned ineligible — reports "corrected", NOT recorded as a decline);
  accept-either-side consumes both (recursive consume, depth-2 bounded) or logs a later
  entry on a reviewed counterpart (log_transfer_decision_on_reviewed, shared with the
  relaxed one-motion path — M6's reviewed-counterpart 409 removed, old test rewritten to
  pin the new contract). Rejection memory: correction-log detection decisions carrying a
  POSITIVE alternative (category/splits/untracked) suppress re-proposal; undo-void
  re-arms. Mirror invalidation deferred-classify wired on ALL paths (single review,
  batch, PATCH-review — the review found batch/PATCH missing). CP3 integration:
  rewrite/retract invalidate mirrors + count them into needs_classification.
  Review triage: fixed batch/PATCH defers, rejection-memory poisoning (degraded accept
  != decline), voided_decision_ids shared helper, establish_transfer docstring (the
  "one implementation" claim now names consume's deliberate degrade-posture sibling —
  routing through it would invert api←classification layering), recursion-bound comment,
  linked/split-exclusion + import-commit detection tests. Accepted residuals: full-ledger
  scan per sweep (v0 posture; indexed candidate query when ledgers grow), invariant list
  encoded in establish/_eligible_counterpart/detector (documented, kept in sync by hand).
  CONTEXT.md: mirror + declined-pairing sentences added to Proposal.
- M7 MERGED 2026-07-20: PR #37 rebase-merged to main (b47ed71..df9fe3c); #31-#36 closed;
  branch m7 retained until frontend-enablers lands. Suite 528 green in CI. Live-sandbox
  smoke run by Taylor surfaced + fixed PRODUCT_NOT_READY (empty-cursor initial-pull state).
  Post-merge follow-ups filed/known: CLI domain command surface (story-31 debt, deliberate —
  pair with M10's published skill), CORS + typed-client enablers for the frontend repo.

# M8 — the look-ahead engine (PRD #45, CP issues #46–#51)

Branch: m8. Delivery: single PR, slices in order CP0 → CP1 → CP2 → CP4 → CP3 → CP5
(CP3 recurring is the pre-flagged cut, second-to-last so cutting strands nothing).

- CP0 (#46): complete (findings comment on issue; scratch scripts run against Postgres
  and deleted). ALL capabilities PASS on ferro 0.17.1 — no gates, floor unchanged.
  ferro#282 closed upstream (scope had shipped in 0.16.0 — stale issue state);
  ferro#327 filed (PRD 0012, temporal trunc, NON-blocking); no latest-per-group PRD
  (per-account seed queries acceptable, served by (account_id, as_of) index).
  Deploy note for CP3/CP4: schema changes require migrate_updates=True (ADR-0010).
  API learnings: descending = order_by(field, "desc"); raw reads = fetch_all.
- CP1 (#47): complete (11 tests in test_reports_net_worth.py; TDD red→green; suite
  567 green + ruff + ty). fx.py seam (no provider; same-currency=1, else None),
  api/reports.py /api/v1/reports/net-worth: forward-filled compute-on-read series,
  fixed-step buckets (1m/daily 30d, 6m/weekly 182d, 1y/weekly 365d, all/monthly
  from first observation), kind-split totals, MTD + since-range-start deltas
  (percent null-on-zero), OLS projection over observed buckets (horizon = range
  length, null under 2 observed buckets), archived invisible, excluded remainder
  per currency, as_of clock seam. connections.ledger_primary_currency promoted
  public (was _-private, M7 owner-fallback semantics unchanged).
- CP2 (#48): complete (8 tests in test_reports_spending.py; TDD red→green; suite 575
  green + ruff + ty). /api/v1/reports/spending: the PRD's one spending definition
  (unsplit outflows by own category + outflow lines by theirs; transfers excluded by
  existence, split parents excluded by their lines' existence; primary-currency
  scoped), positive magnitudes, sparse by_day from GROUP BY date, Python hierarchy
  rollup (uncategorized rolls to itself), previous-month block + per-category
  previous/percent_change (null-on-zero) + total change delta, foreign-currency
  outflows as excluded remainder (whole parent amounts — lines never double it),
  month=YYYY-MM validated 400, as_of default for the month. ty note: Row projection
  fields need scoped ty:ignore[unresolved-attribute] at access sites.
- CP4 (#50): complete (9 tests in test_debt_loans.py; TDD red→green; suite 584 green
  + ruff + ty). models.py: five nullable terms columns on Account (apr percent float,
  minimum_payment_minor, origination_date, origination_amount_minor account-signed
  negative, maturity_date). accounts PATCH grew from label-only to AccountPatchIn
  (present-field semantics, present-and-null clears; kind guard: loan=all five,
  credit=apr+minimum, others none → 400). AccountOut gains nested terms (null until
  any set; ACCOUNT_FIELDS updated in test_accounts_api). loans.py: observed_pace
  (median of trailing 6 COMPLETE calendar months' inflow-transfer totals, zero months
  count — a single payment yields pace 0 by design, test corrected to match spec),
  simulate_payoff (monthly apr/1200 compounding, round-to-minor interest, payment ≤
  interest OR >1200 months → never_pays_off with empty projections), add_months
  day-clamping. GET /accounts/{id}/payoff (400 on non-debt kinds, extra_monthly>0
  scenario vs at-pace, headline when both sims finite, payoff_percent from
  origination). GET /reports/debt: per-loan rows via the same account_payoff
  derivation, weighted APR balance-weighted, debt_free_by = max finite payoff,
  excluded-count markers per aggregate, foreign-currency debt in excluded remainder.
  Deploy note: this slice's schema lands via existing database_migrate_updates
  setting (verified present in db.connect_database — no wiring needed).
- CP3 (#49): complete — NOT cut, same as M6 promotion / M7 detector (13 tests in
  test_recurring.py; TDD red→green; suite 597 green + ruff + ty). models.py:
  RecurringSeries (stored MATCHER: account+payee+direction+nullable exact
  amount_minor, composite unique = re-sweep idempotency guard; no links — members
  match-on-read, self-healing vs retraction/undo) + 3 enums. recurring.py:
  detect_recurring in classify_ledger after detect_transfers (manual/import/sync all
  funnel through); two-pass fitting (merged payee first — holds price hikes and
  variable bills together; amount sub-group second — the Apple trio); per-cadence
  guards (weekly/biweekly = one weekday, monthly+ = dom spread ≤3 clamped at 28) make
  interleaved same-amount anchors deterministic silence; inflow legs on loan/credit
  accounts skipped (payment-received ≠ income — session decision, flag in review).
  Cycle state computed in a CALENDAR-MONTH frame (paid = member this month; due/
  overdue vs next expected; upcoming; lapsed = 2 cadences empty, computed never
  stored). Fixed = recents within ~1%, est = median. api/recurring.py: Page list
  (kind/unpaid filters — unpaid applied post-page, documented), PATCH kind+
  display_name only (income not re-segmentable → 400; matcher writes 400 via
  extra=forbid), POST dismiss (permanent, idempotent; detection matches and leaves
  it). /reports/recurring: monthly normalization (52/12 etc), due-next-7-days card +
  contributors, subscriptions card, by-bucket donut (Debt via transfer counterpart
  kind, else modal member category), cycle paid/total. CONTEXT.md: Recurring
  series / Cadence / Cycle section added. Engine nuance found by tests: a
  single-amount payee fits pass 1 → merged (None) matcher; amount scoping only
  exists when a shared payee forces it.
- CP5 (#51): complete (4 tests in test_ledger_stats.py; TDD red→green; suite 601
  green + ruff + ty). api/ledgers.py GET /api/v1/ledgers/current/stats:
  transactions_total, classified (reviewed + unreviewed-with-proposal — empty
  proposals count, the pipeline answered), unreviewed, unreviewed_by_provenance
  (grouped count over unreviewed txns' proposals, all five provenance keys),
  recurring_found (active series count), last_synced_at (max across connections,
  null never-synced). Provenance split uses the CP0-verified traversed-where +
  grouped-projection composition.
- M8 COMPLETE: all 6 CPs on branch m8, suite 601 green, pushed as one PR
  (closes #46–#51 at merge). Frontend follow-up: `just openapi-sync` (AccountOut
  gained terms; new reports/recurring/payoff/stats endpoints).

# M9 — Penny comes to life (PRD #53, CP issues #54–#59)

Branch: m9. Delivery: single PR, one commit per CP, rebase-merge clean.
Order: CP0 → CP1 ∥ CP3 → CP2 → CP4 → CP5 (CP5 mapping agent is the pre-flagged cut).

- CP0 (#54): complete — integration spike, ALL FOUR legs PASS (6 scratch tests, incl.
  the live gateway smoke; file deleted after findings, per M7/M8 precedent; results
  comment on #54). pydantic-ai 2.7.0 pinned observations:
  1. Vercel adapter on Litestar: PROVEN. The glue is exactly three calls —
     build_run_input(await request.body()) → VercelAIAdapter(agent, run_input,
     accept=<accept header>, sdk_version=6) → litestar Stream(
     adapter.encode_stream(adapter.run_stream(...)), media_type/headers from
     build_event_stream().content_type/.response_headers). Approval round-trip
     over the wire works both ways: round 1 emits tool-input-available +
     tool-approval-request chunks then [DONE]; round 2 (assistant part
     state=approval-responded) auto-parses into adapter.deferred_tool_results —
     approve executes the tool (tool-output-available + final text), deny
     withholds it and the reason reaches the model. Gotchas for CP1:
     (a) @post needs status_code=200 (Litestar's POST default is 201);
     (b) agent needs output_type=[str, DeferredToolRequests] or approvals error;
     (c) run_stream_events makes STREAMED model requests → FunctionModel test
     scripts must pass stream_function= (yield str or {0: DeltaToolCall(...)});
     (d) CSRF double-submit applies to cookie-credentialed chat POSTs (browser
     clients already do the dance; bearer callers exempt by construction).
  2. In-process self-call: PROVEN. httpx.AsyncClient(transport=
     ASGITransport(app=request.app)) + forwarded Authorization header calls
     GET /api/v1/accounts through the full middleware chain (ferro session
     nests cleanly) with the caller's own PAT scopes; no event-loop grief.
  3. Message persistence: PROVEN LOSSLESS. json.loads(result.all_messages_json())
     → ferro list[dict] column (JSONB) → ModelMessagesTypeAdapter.validate_python
     == original history (tool calls/returns, ThinkingPart signature, unicode,
     timestamps), and the restored history fed a second run unchanged.
  4. Gateway smoke: PROVEN LIVE (claude-haiku-4-5 via gateway-us, usage > 0) with
     ONE design consequence recorded on #53: "gateway/<seg>:<model>" resolves
     <seg> as both the provider kind and the gateway URL route segment; custom
     console route names (ours: pinch-anthropic-provider) 404 and no string
     syntax carries a route override. The one-knob model-string story therefore
     requires gateway console routes named exactly after the provider kind
     (anthropic, openai, ...). Operator action: rename the console routes; code
     stays gateway-ignorant (gateway_provider(..., route=...) rejected — Pinch
     code never knows the gateway exists).
- CP1 (#55): complete — chat core, read-only (26 new tests across
  test_penny_{status,conversations,bundle,chat}.py + settings/PAT additions;
  TDD red→green per slice; suite 627 green + ruff + ty). settings: ai_chat_model /
  ai_categorization_model / ai_mapping_model knobs (empty = disabled); conftest
  blanks all AI knobs AND pops gateway/anthropic keys — keyless is the tested
  baseline. Scopes: PatScope.PENNY is a wire value only; the rank column stays
  READ/WRITE and PAT grows penny_scope bool (orthogonal, never widens the rank;
  sessions always penny). GUARD CHANGE: routes may declare opt penny_gated=True
  to swap the blanket write-rank gate for the penny gate — POST /penny/chat is a
  conversation, not a domain write; requiring WRITE would hand every chat token
  the whole write surface (least-privilege argument, recorded in guards.py).
  Conversation model: client-minted UUIDv7 pk (enforced, 400 otherwise — keeps
  id-keyset creation order), ledger FK + BackRef, title (first user text, 80
  chars, set once), messages JSONB (pydantic-ai native; UI rendering derived at
  read via dump_messages sdk_version=6). pagination.py grew paginate_desc
  (newest-first id keyset). penny/ package: availability.py (per-agent, reason =
  infer_model's own complaint, so missing-key reporting can't drift),
  deps.py (PennyDeps + api_get raising ApiDeclined w/ error-envelope detail),
  bundles.py (read_bundle Capability: 10 tools, _relay_declines decorator makes
  4xx the tool's honest string answer, _all_pages caps at 4 pages), prompts.py
  (v1: grounding + minor-units + relay-declines + read-only), agents.py
  (chat_agent built model-less; model resolved per run from the knob).
  api/penny.py: status (credentialed, per-agent map), conversations
  list/get/delete (ownership-404), chat (503+reason keyless; penny 403;
  server-authoritative history; persist-on-complete inside the stream — ferro
  session middleware spans response streaming, so on_complete just works;
  foreign-uuid7 collision answers 404 and never appends). Deferred to CP2:
  DeferredToolRequests in chat output_type (no write tools yet), approval-run
  persistence semantics (approvals are ephemeral — decide what a deferred-ending
  run persists), conversation-delete under read+penny rank (blanket gate still
  requires WRITE for DELETE; acceptable, noted).
- CP2 (#56): complete — writes & approvals (5 tests in test_penny_writes.py;
  TDD red→green; suite 632 green + ruff + ty). bundles.py: write_bundle
  Capability — recategorize_transaction / accept_review / create_rule /
  mark_transfer / create_category, each Tool(requires_approval=True), each via
  api_request (new generic self-call in deps.py). agents.py: chat_agent
  composes read+write bundles, output_type=[str, DeferredToolRequests].
  _caller_headers forwards the session caller's CSRF pair (csrftoken cookie +
  x-csrftoken header) — the chat POST passed the same check, so the material
  is the caller's own; bearer path unchanged. Approval flow (api/penny.py):
  verdict messages (assistant, all parts approval-responded) are CONSUMED into
  deferred_tool_results and dropped from run input — the server's stored
  tool-call args execute, never the client's echo (tamper-proof by
  construction); ordering matters: read adapter.deferred_tool_results (caches)
  BEFORE filtering run_input.messages (adapter.messages not yet cached).
  EXPIRY = _expire_dangling: stored history ending in unanswered
  approval-required calls gets denied ToolReturnParts appended ("approval
  expired unanswered; the action was never taken") before the run — no
  provider ever sees a dangling tool_use, the write never happens, Penny says
  so on resume, and the repair persists with the completed run. Approve/deny/
  expire all asserted from the wire; accept_review actor=user in the
  correction log BY CONSTRUCTION (same public endpoint as the inbox, caller's
  credential); read+penny caller approving a write hears the API's 403
  conversationally (tool returns the decline as its answer). Learned: on
  resume, pydantic-ai merges the repair request + new user prompt into one
  ModelRequest — scripts asserting expiry should look at returns anywhere in
  the last request, not branch on message count.
- CP3 (#57): complete — evals harness + categorization agent (10 tests across
  test_penny_categorization.py + test_penny_evals.py; TDD red→green; suite 642
  green + ruff + ty). penny/categorization.py: Categorization output
  ({category_path: str|null}, no confidence), output_validator draws ModelRetry
  naming the hallucinated path, PennyClassifier fills the M5 seam (keyless /
  empty-taxonomy / exhausted-retries / provider-error ALL → abstain);
  active_classifier = PennyClassifier() — provenance=ai reachable, CI keyless
  baseline abstains without constructing a model (test pinned).
  format_prompt shared between classifier and harness so measured ≡ production.
  penny/evals.py: CategoryScore (exact 1.0 > ancestor 0.5 > abstain 0.25 >
  wrong 0.0; expected-null scores only an abstain) returning rate flags
  (exact/abstained/wrong become report columns); seed dataset
  evals/categorization/seed.yaml (26 cases incl. 4 curated abstains);
  correction-log exporter (actor=USER, kind=DECISION, voided excluded,
  split/transfer decisions excluded, uncategorized accepts become abstain
  cases, expected as full path via live taxonomy). cli: pinch-dev evals
  run/export (run instruments pydantic-ai for cost, reports Logfire experiment;
  export target evals/exports/ gitignored). BASELINE (recorded on #57):
  gateway/anthropic:claude-haiku-4-5, 26 cases — mean 0.923, exact 92.3%,
  abstain 0%, wrong 7.7% (uber-ride → Travel > Rideshare vs Transportation >
  Auto & Ride Share — the taxonomy genuinely contains both, case pins the
  convention; ambiguous-amazon → Shopping vs curated abstain). ~$0.0014/case,
  ~1.7s/case. Improvement law in effect. ty gotcha: Agent(output_type=...)
  doesn't thread the generic — annotate + scoped ignore.
- CP4 (#58): complete — chat golden-task evals (10 tests in
  test_penny_chat_evals.py; suite 652 green + ruff + ty).
  penny/evals_chat.py: ChatTrajectory evaluator (right_tool any-of, pause_ok,
  write_safe = no unapproved write executed, grounded = money-shaped numbers
  traceable to tool results); numbers_grounded heuristic is minor-units
  aligned, ignores bare years/dates, and accepts SHOWN-WORK sums (a total
  equal to pair/whole-sum of grounded addends is math, not fabrication —
  bare unshown totals still fail). chat_task runs the REAL capability stack:
  provision_sandbox (model-layer user+PAT, uuid4 email suffix — uuid7 hex
  prefix collides within ~65s! — then seeds via public API as the caller),
  deterministic ledger (checking 12.5k, car loan -8.9k, 3mo rent/payroll/
  Netflix, July groceries+coffee). evals/chat/seed.yaml: 8 golden cases
  (4 grounded reads w/ LLMJudge rubrics on gateway sonnet, 2 write-pauses,
  cannot-see-imports honesty, out-of-domain refusal). pinch-dev evals run
  chat: connect db + job app, ambient ferro session, drain job chain once
  (recurring detection), evaluate max_concurrency=2. IMPROVEMENT LAW CYCLE 1
  (recorded on #58): prompt v1 baseline 0.875 trajectory — grounded-spending
  FAILED for a real reason: literal substring search for "Whole Foods" missed
  "WHOLEFDS MKT" and Penny concluded no transactions existed. Prompt v2 adds
  (a) show-your-addends when totaling and (b) bank-abbreviation retry
  guidance → 1.000 trajectory (8/8), ~$0.0069/case, ~3s/case. Both heuristic
  refinements (years, shown-work sums) unit-pinned. Chat evals sandbox note:
  runs write throwaway penny-evals-*@pinch.local users into the connected
  dev database — acceptable dev-db litter, delete by hand if it bothers.
- CP5 (#59): complete — NOT cut, same as M6 promotion / M7 detector / M8
  recurring (6 seam tests in test_penny_mapping.py + 3 evals tests; suite 661
  green + ruff + ty). penny/mapping.py: mapping_agent (output_type=MappingSpec
  directly — the M4 spec IS the structured output), output_validator checks
  the spec against the ACTUAL sample (delimiter re-split, 0-based column
  bounds incl. description_columns, date_format must parse a majority of
  sampled date values — ModelRetry names the failure); PennyInferrer layers
  heuristic-first (normal files never cost a token), agent on abstention,
  bounded_sample 20 lines, every failure → no suggestion (manual mapping,
  today's floor). Seam swap at inference.py tail (lazy HeuristicInferrer
  import in PennyInferrer.__init__ breaks the cycle). Keyless byte-identical
  (explode-model pinned). evals: MappingScore (4 equal field-groups: shape/
  date/amount+sign/descriptions; abstain 0.25 > wrong 0; hopeless-file
  expects None), evals/mapping/seed.yaml 6 gnarly shapes with a hygiene test
  PINNING that the heuristic abstains on every case (an inert case measures
  nothing); pinch-dev evals run mapping. BASELINE (haiku, on #59): mean
  0.958, 5/6 exact; the miss is paren-negatives sign=positive_out (parser
  reads parens as negative, so negative_out is right — the trickiest sign
  case, kept). ~$0.0025/case, ~2s.
- M9 COMPLETE: all 6 CPs on branch m9, one commit per CP, suite 661 green.
  PR closes #53–#59 at merge. Frontend follow-up: `just openapi-sync`
  (penny routes: status/conversations/chat). F6 is next (useChat +
  sdk_version 6 approval rendering; GET conversation answers UI messages).
