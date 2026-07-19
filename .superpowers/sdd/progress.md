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
  Disconnect + initial-sync auto-enqueue arrive with ferro#325 / CP2 respectively.
