# PRD 0004: Static typing for shadow FK columns and relation traversal

**Requested by:** Pinch • **Blocks:** nothing hard — Pinch has a contained
interim pattern — but the cost grows linearly: every FK-traversing module
accrues `ty: ignore` comments, starting with M2's auth guards, and M3's API
layer will traverse FKs in nearly every handler.

## Summary

Make the two everyday foreign-key idioms type-check under static checkers
(Astral's `ty` is what Pinch runs in CI) without per-call-site ignores:

1. **Shadow columns** — reading the synthesized `model.<fk>_id`
2. **Forward traversal** — `await model.<fk>` resolving to the target model

The runtime behavior is correct and ergonomic; this is purely a static-
visibility gap. Both features are synthesized at class creation
(`_shadow_fk_types.py` injects `<fk>_id` into `__annotations__`; attribute
access returns an awaitable proxy), which no plugin-less checker can see.

## Current behavior

Given M1/M2-style models:

```python
class Session(TimestampMixin, Model):
    user: Annotated[User, ForeignKey(related_name="sessions", index=True)]
    ...
```

both idioms are flagged:

```
error[invalid-await]: `User` is not awaitable
   |
26 |             return await session.user
   |                          ^^^^^^^^^^^^
info: `__await__` is missing

error[unresolved-attribute]: Object of type `Session` has no attribute `user_id`
   |
26 |             return await User.get(session.user_id)
   |
```

## Motivating Pinch code

M2's request guards — the dependency every authenticated endpoint from M3
onward runs (`pinch_backend/auth/guards.py`):

```python
async def provide_current_user(request: Request) -> User:
    secret = request.cookies.get(settings.session_cookie_name)
    if secret:
        session = await resolve_session(secret)
        if session is not None:
            # Shadow *_id columns are runtime-synthesized; invisible to ty.
            return await User.get(session.user_id)  # ty: ignore[unresolved-attribute]
    raise NotAuthorizedException(detail="Not authenticated")


async def provide_current_ledger(current_user: User) -> Ledger:
    membership = await LedgerMember.where(lambda m: m.user_id == current_user.id).first()
    if membership is None:
        raise RuntimeError(f"User {current_user.id} has no ledger membership")
    return await Ledger.get(membership.ledger_id)  # ty: ignore[unresolved-attribute]
```

Note the second lambda: `m.user_id` inside `.where()` happens to escape
diagnosis today only because of how lambdas are analyzed — the same
expression outside a lambda is an error. The idiom is load-bearing across
the codebase either way.

## Requirements

- [ ] `model.<fk>_id` is statically typed as the target's PK scalar,
      matching the runtime shadow annotation (`UUID | None` pre-assignment)
- [ ] `await model.<fk>` — or a sanctioned, equally terse typed traversal —
      statically resolves to the target model type
- [ ] No per-call-site ignore comments; no checker plugins (`ty` has no
      plugin system, so the mypy-plugin route is a dead end)
- [ ] Verified under `ty` (Pinch's CI checker); pyright/mypy compatibility
      is nice-to-have

## Strawman options (illustrative only — ferro idioms win)

1. **Descriptor-typed forward relations** — the same move that fixed
   BackRefs in ferro-orm#28 (`Relation[list[T]] = BackRef()`): a
   `Related[T]` annotation/descriptor whose `__get__` is typed as
   `Awaitable[T]`-compatible, e.g.
   `user: Related[User] = ForeignKey(related_name="sessions")`.
2. **Opt-in explicit shadow declaration** — let models declare the column
   ferro would synthesize, and have ferro adopt rather than duplicate it:
   `user_id: uuid.UUID = ShadowOf("user")`.
3. **Stub generation** — a `ferro stubgen` emitting `.pyi` overlays for
   model modules (django-stubs / SQLAlchemy precedent). Heaviest option;
   keeps runtime untouched.

## Notes

- Prior art: ferro-orm#28 solved the BackRef half of this problem; this PRD
  is the forward half (shadow columns + awaitable traversal).
- Pinch's interim pattern, for reference: fetch by shadow id
  (`await User.get(session.user_id)`) with a reasoned
  `ty: ignore[unresolved-attribute]` — correctness is unaffected; the cost
  is ignore-comment sprawl and the erosion of `ty`'s signal on real bugs.
