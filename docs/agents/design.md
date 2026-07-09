# General Software Design Guide

**Status**: Approved

---

## Overview

This document defines the general coding philosophy and design preferences for
pinch-backend. Domain-specific guides under `docs/agents/` build on this
baseline; this one applies everywhere.

The preferred style is a **pragmatic mix of OOP and functional design**. Neither
extreme is the goal: not everything in a class (Java-style), and not everything
as a flat bag of functions. Classes organize related behavior and state;
standalone functions are fine for genuinely independent operations. The test is
always clarity and navigability, not pattern adherence.

---

## Core Decisions

| Decision | Choice | Rationale |
|---|---|---|
| OOP vs functional | Mix both pragmatically | Classes when behavior/state/resources cluster together; standalone functions when an operation is truly independent |
| When to write a class | Shared behavior, shared state, resource encapsulation, or interface contract | Four valid drivers — any one is sufficient |
| When to write a function | When it's called from multiple places, complex enough to name, or materially clarifies the call site | Single-use inline logic does not need to be extracted |
| DRY | Important, but not at the expense of over-abstraction | Duplication is sometimes the clearer choice; premature abstraction is a cost, not a virtue |
| Structured data | Pydantic models by default | Validation, serialization, and type safety in one; `pydantic.dataclasses` for simple internal structs that never cross a boundary |
| Inheritance | Pragmatic | Use when it's genuinely the clearest model; composition otherwise; no deep hierarchies |
| Error handling | Raise at the point of failure; catch at the recovery point | Don't swallow errors; don't add catch blocks that don't meaningfully handle the failure |

---

## 1. Class Design

Reach for a class when one or more of the following applies:

1. **Grouping related state** — several pieces of data naturally belong together and methods operate on that shared state.
2. **Grouping related behavior** — a family of related operations that would otherwise be loose functions scattered across a module (e.g., a `TranscriptParser` with several methods).
3. **Encapsulating a resource** — anything with setup/teardown, connection state, or lifecycle (DB clients, API clients, external services).
4. **Defining a contract** — an abstract base class or protocol that multiple implementations will satisfy.

A flat module full of loosely related functions is harder to navigate than a well-named class that groups them. When functions share a subject, make it a class.

```python
# Avoid — loose functions with a shared implicit subject
def parse_transcript_header(raw: str) -> TranscriptHeader: ...
def parse_transcript_turns(raw: str) -> list[Turn]: ...
def normalize_transcript_text(raw: str) -> str: ...
def validate_transcript_format(raw: str) -> None: ...


# Prefer — the subject is explicit; the module is navigable
class TranscriptParser:
    """Parses and normalizes raw transcript text into structured form."""

    def parse(self, raw: str) -> Transcript:
        """Entry point. Returns a fully structured Transcript."""
        self._validate(raw)
        return Transcript(
            header=self._parse_header(raw),
            turns=self._parse_turns(raw),
        )

    def _validate(self, raw: str) -> None: ...
    def _parse_header(self, raw: str) -> TranscriptHeader: ...
    def _parse_turns(self, raw: str) -> list[Turn]: ...
```

Not everything needs a class. A standalone utility function that does one thing and has no natural siblings belongs at module level.

```python
# Fine as a standalone function — no siblings, no shared state
def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
```

---

## 2. Function Design

A function should earn its existence. Extract logic into a named function when at least one of the following is true:

- It is called from **more than one place**
- It is **complex enough to warrant a name** that clarifies what it does
- Extracting it makes the **call site materially clearer**

If none of these apply, keep the logic inline. A single-use helper that wraps three lines of straightforward code adds a layer of indirection for no benefit.

```python
# Avoid — unnecessary extraction; the one-liner is clear at the call site
def _build_candidate_key(candidate_id: UUID, role_id: UUID) -> str:
    return f"{candidate_id}:{role_id}"

def process(candidate_id: UUID, role_id: UUID) -> None:
    key = _build_candidate_key(candidate_id, role_id)
    ...


# Prefer — inline when the logic is obvious and used once
def process(candidate_id: UUID, role_id: UUID) -> None:
    key = f"{candidate_id}:{role_id}"
    ...
```

DRY matters when the same logic appears in multiple places. When logic appears once, DRY is not in play — don't apply it preemptively.

```python
# Good extraction — used in multiple places; naming adds clarity
def format_score_for_display(score: float) -> str:
    """Formats a raw score as a percentage string, e.g. 0.87 → '87%'."""
    return f"{round(score * 100)}%"
```

---

## 3. Data Models

Use **Pydantic `BaseModel`** as the default for all structured data. Pydantic provides validation, serialization, IDE support, and JSON schema generation in one — there is rarely a reason to reach for something else.

Prefer **attribute docstrings** over `Field(description=...)` for documenting fields. Attribute docstrings keep the type annotation clean and are natively supported by Pydantic v2.

Attribute docstrings require `use_attribute_docstrings=True` in the model's `ConfigDict`. Set this on every model that uses them.

```python
from pydantic import BaseModel, ConfigDict, Field

class ScorecardOutcome(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str
    """The outcome name as it appears in the scorecard."""
    description: str
    """Full definition of the outcome used by the scoring agent."""
    weight: float = Field(gt=0.0, le=1.0)
    """Relative importance of this outcome; all weights in a scorecard sum to 1."""
    required: bool = True
    """If True, absence of evidence for this outcome is always flagged as a gap."""
```

Reserve `Field(description=...)` for when constraints (`gt`, `le`, `min_length`, etc.) are also needed — but even then, an attribute docstring on the next line is preferred over combining both into `Field`.

**Dataclasses** are acceptable for simple internal structs that:
- Are never serialized or deserialized
- Don't need field validation
- Never cross a module or service boundary

Agent `deps` are the primary example — they are constructed in one place and consumed immediately; validation adds no value.

Use `pydantic.dataclasses.dataclass` rather than the stdlib `dataclasses.dataclass` to stay in the Pydantic ecosystem. It is a drop-in replacement that adds construction-time validation.

```python
from pydantic.dataclasses import dataclass
from supabase import AsyncClient

@dataclass
class SkillsDeps:
    supabase: AsyncClient
    assessment: Assessment
    scorecard: Scorecard
```

If a struct starts crossing boundaries or needs validation, migrate it to `BaseModel`.

---

## 4. Inheritance & Composition

Use inheritance when it is genuinely the clearest model — typically when you have a concrete base with shared implementation and multiple variants that extend it. Use composition otherwise.

```python
# Inheritance appropriate — shared base behavior, concrete variants
class BaseScorer:
    """Shared scaffolding for all scoring agents."""

    def __init__(self, agent: Agent, phase: str) -> None:
        self.agent = agent
        self.phase = phase

    async def score(self, assessment_id: UUID, deps: Any) -> UUID:
        output = await self._run(assessment_id, deps)
        return await write_snapshot(assessment_id, phase=self.phase, data=output)

    async def _run(self, assessment_id: UUID, deps: Any) -> BaseModel:
        raise NotImplementedError


class SkillsScorer(BaseScorer):
    async def _run(self, assessment_id: UUID, deps: SkillsDeps) -> SkillsOutput:
        ...
```

```python
# Composition appropriate — unrelated capabilities; no shared implementation
class AssessmentReporter:
    """Generates the LoS report from scored phase outputs."""

    def __init__(self, formatter: ReportFormatter, storage: ReportStorage) -> None:
        self._formatter = formatter
        self._storage = storage

    async def publish(self, assessment_id: UUID) -> str:
        report = self._formatter.build(assessment_id)
        return await self._storage.save(report)
```

Avoid deep hierarchies. If a third level of inheritance feels necessary, it is almost always a sign that the abstraction needs to be reconsidered.

---

## 5. Error Handling

Raise exceptions at the point of failure. Let them propagate to the layer that can meaningfully handle them. Do not add catch blocks that don't actually recover from the error.

```python
# Avoid — swallowing the error; the caller has no idea what happened
def fetch_scorecard(role_id: UUID) -> Scorecard:
    try:
        return db.query(Scorecard, role_id)
    except Exception:
        return None  # silent failure; caller proceeds with bad state


# Prefer — raise and let it propagate
def fetch_scorecard(role_id: UUID) -> Scorecard:
    result = db.query(Scorecard, role_id)
    if result is None:
        raise ValueError(f"No scorecard found for role {role_id}")
    return result
```

Catch at the recovery point — the layer that has enough context to decide what to do:

```python
# Catch where you can actually handle it
async def run_job(assessment_id: UUID) -> UUID:
    try:
        ...
    except ValueError as e:
        logger.error("Job failed", error=str(e))
        await mark_failed(assessment_id)
        raise  # still re-raise so the orchestrator sees the failure
```

Use specific exception types. Catching bare `Exception` is acceptable at flow boundaries for cleanup, but always re-raise or log with full context.

---

## 6. Module Organization

A module should have a clear subject. When a module grows beyond that subject, split it — not before.

- **Classes are the primary unit of organization** within a module. Related methods belong on a class, not as peer functions alongside it.
- **Module-level code** (constants, `__all__`, top-level instantiation like agent singletons) is fine and expected.
- **Don't split prematurely.** A single well-organized module is easier to navigate than several micro-modules connected by cross-imports. Split when a module genuinely has two separable subjects, not simply because it's grown long.

```
agents/
  skills.py        # SkillsDeps, SkillsOutput, skills_agent, run_skills_agent — all in one
  categorization.py
  benchmark.py
  types.py         # only if types are genuinely shared across multiple agent modules
```
