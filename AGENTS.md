# AGENTS.md

Hard project invariants and design baseline for agents and human contributors.
These are contracts — not style preferences. Violating an invariant is a
correctness or maintainability bug.

Domain-specific guides may be added under `docs/agents/` as the project grows.
The general coding baseline lives in [docs/agents/design.md](docs/agents/design.md).

---

## I-1: Build the best solution, not the quickest

Every feature, bug fix, and improvement must be designed as the best,
well-thought-out solution for the project with its long-term future in
mind — as if time and money were no object. No stop-gaps, hacks,
quick-fixes, or otherwise lesser solves.

What this means in practice:

- **Prefer first-class, reusable primitives over local patches.** If a fix
  only works for the immediate symptom while leaving the underlying
  capability gap in place, build the capability instead.
- **Fail loudly over degrading silently.** "Skip with a warning and
  continue", "best effort", and "documented residual risk" are not
  acceptable resolutions for correctness gaps. Either the operation
  succeeds completely or it aborts with a clear, actionable error.
- **Treat certain phrases as redesign triggers.** If a plan, comment, or PR
  description contains "best-effort", "partial mitigation", "documented
  residual risk", "good enough for now", "temporary workaround", or
  "fallback if X turns out to be hard" — that part of the design is not
  finished. Redesign it before presenting or implementing it.
- **Scoped-down is fine; hollowed-out is not.** Deliberately excluding
  something from scope — with the boundary stated and a real path for the
  excluded case — is good design. Shipping a half-working version of
  something that is *in* scope is not.

This rule binds human contributors and AI agents equally, and overrides any
agent default that biases toward minimal or expedient changes.

---

## Agent skills

### Issue tracker

Issues and PRDs live in the repo's GitHub Issues, via the `gh` CLI. External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles, each mapped to its default label string (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
