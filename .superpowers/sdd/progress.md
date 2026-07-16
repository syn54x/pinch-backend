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
