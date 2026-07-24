"""The read bundle (PRD M9 CP1): curated capabilities, not endpoint
mirrors — friendly names, digested arguments, internal pagination, compact
summaries. Tool design is prompt design: for the smallest viable model, a
short catalog is a correctness feature.

Every tool bottoms out in ``api_get`` — the public v1 API as the caller —
and reports a declined call as a sentence, never an exception: the model
relays it (tested at the capability seam).
"""

import functools
from typing import TYPE_CHECKING, Any

# Runtime import despite TC002: pydantic-ai resolves tool signatures at
# Capability construction, so RunContext must be importable when the
# annotations are evaluated (the litestar-decorator precedent, guards.py).
from pydantic_ai import RunContext  # noqa: TC002
from pydantic_ai.capabilities import Capability
from pydantic_ai.tools import Tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from pinch_backend.penny.deps import ApiDeclined, PennyDeps, api_get, api_request

_MAX_TOOL_PAGES = 4
"""Internal-pagination ceiling: enough for every realistic taxonomy or
account list, bounded so a pathological ledger can't flood the context."""


def _relay_declines[**P](
    tool: "Callable[P, Awaitable[Any]]",
) -> "Callable[P, Awaitable[Any]]":
    """A declined self-call becomes the tool's honest answer."""

    @functools.wraps(tool)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return await tool(*args, **kwargs)
        except ApiDeclined as declined:
            return str(declined)

    return wrapped


async def _all_pages(ctx: RunContext[PennyDeps], path: str, params: dict | None = None) -> list:
    """Drain a cursor-paginated endpoint, bounded by _MAX_TOOL_PAGES."""
    items: list = []
    cursor: str | None = None
    for _ in range(_MAX_TOOL_PAGES):
        page = await api_get(ctx.deps, path, {**(params or {}), "cursor": cursor, "limit": 100})
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return items


@_relay_declines
async def list_accounts(ctx: RunContext[PennyDeps]) -> list[dict]:
    """Every account with its kind, currency, current balance (integer minor
    units), and archived flag."""
    accounts = await _all_pages(ctx, "/api/v1/accounts")
    return [
        {
            "id": a["id"],
            "label": a["label"],
            "kind": a["kind"],
            "currency": a["currency"],
            "balance_minor": (a.get("balance") or {}).get("amount_minor"),
            "balance_as_of": (a.get("balance") or {}).get("as_of"),
            "archived": a["archived"],
        }
        for a in accounts
    ]


@_relay_declines
async def search_transactions(
    ctx: RunContext[PennyDeps],
    query: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    uncategorized: bool | None = None,
    limit: int = 20,
) -> dict:
    """Search transactions, newest first. ``query`` is a case-insensitive
    substring over descriptions, display names, and notes; dates are
    ISO (YYYY-MM-DD); ``limit`` caps the rows returned (max 50). The answer
    notes when more rows matched than were returned."""
    page = await api_get(
        ctx.deps,
        "/api/v1/transactions",
        {
            "q": query,
            "account_id": account_id,
            "category_id": category_id,
            "date_from": date_from,
            "date_to": date_to,
            "uncategorized": uncategorized,
            "limit": max(1, min(limit, 50)),
        },
    )
    return {
        "transactions": [_txn_digest(t) for t in page["items"]],
        "more_matches": page["next_cursor"] is not None,
    }


def _txn_digest(t: dict) -> dict:
    return {
        "id": t["id"],
        "date": t["date"],
        "amount_minor": t["amount_minor"],
        "currency": t["currency"],
        "description": t["display_name"] or t["description_raw"],
        "category": (t.get("category") or {}).get("name"),
        "account_id": t["account_id"],
        "pending": t["pending"],
        "reviewed": t["reviewed_at"] is not None,
        "tags": [tag["name"] for tag in t.get("tags") or []],
    }


@_relay_declines
async def get_transaction(ctx: RunContext[PennyDeps], transaction_id: str) -> dict:
    """One transaction in full detail, including splits and any pending
    classification proposal."""
    return await api_get(ctx.deps, f"/api/v1/transactions/{transaction_id}")


@_relay_declines
async def spending_report(ctx: RunContext[PennyDeps], month: str | None = None) -> dict:
    """One month of spending (YYYY-MM; defaults to the current month):
    total, by-category rollup, daily trend, and the change versus the
    prior month. Transfers are excluded by design."""
    return await api_get(ctx.deps, "/api/v1/reports/spending", {"month": month})


@_relay_declines
async def net_worth_report(ctx: RunContext[PennyDeps], range: str = "6m") -> dict:
    """Net worth now, its trend over the range (1m/3m/6m/1y/all), and the
    run-rate projection. The point-by-point history is omitted from this
    digest; the trend endpoints of the range remain."""
    report = await api_get(ctx.deps, "/api/v1/reports/net-worth", {"range": range})
    history = report.pop("history", None)
    if history:
        report["history_omitted"] = f"{len(history)} points omitted from this digest"
        report["history_start"] = history[0]
        report["history_end"] = history[-1]
    return report


@_relay_declines
async def debt_report(ctx: RunContext[PennyDeps]) -> dict:
    """Every loan and credit account: balances, APR where known, observed
    versus minimum payoff projections, and the debt-free date."""
    return await api_get(ctx.deps, "/api/v1/reports/debt")


@_relay_declines
async def list_recurring_series(
    ctx: RunContext[PennyDeps], kind: str | None = None, unpaid: bool | None = None
) -> list[dict]:
    """Detected recurring money movements (bills, subscriptions, income)
    with cadence and current cycle state. ``kind`` filters bill /
    subscription / income; ``unpaid`` keeps only due or overdue."""
    page = await api_get(
        ctx.deps, "/api/v1/recurring", {"kind": kind, "unpaid": unpaid, "limit": 100}
    )
    return page["items"]


@_relay_declines
async def list_categories(ctx: RunContext[PennyDeps]) -> list[dict]:
    """The user's category taxonomy: id, name, and parent_id (null for a
    top-level category)."""
    categories = await _all_pages(ctx, "/api/v1/categories")
    return [{"id": c["id"], "name": c["name"], "parent_id": c["parent_id"]} for c in categories]


@_relay_declines
async def list_rules(ctx: RunContext[PennyDeps]) -> list[dict]:
    """The user's classification rules: condition and actions, in
    evaluation order."""
    rules = await _all_pages(ctx, "/api/v1/rules")
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "condition": r["condition"],
            "category": (r.get("action_category") or {}).get("name"),
            "add_tags": r["action_add_tags"],
            "rename_to": r["action_rename_to"],
            "mark_transfer": r["action_mark_transfer"],
        }
        for r in rules
    ]


@_relay_declines
async def ledger_stats(ctx: RunContext[PennyDeps]) -> dict:
    """Ledger-level counts: transactions total, classified, unreviewed (by
    provenance), recurring series found, last sync time."""
    return await api_get(ctx.deps, "/api/v1/ledgers/current/stats")


# --- The write bundle (CP2 #56): every tool requires approval ------------


@_relay_declines
async def create_category(
    ctx: RunContext[PennyDeps], name: str, parent_id: str | None = None
) -> dict:
    """Create a category in the user's taxonomy. ``parent_id`` nests it
    under an existing category."""
    return await api_request(
        ctx.deps, "POST", "/api/v1/categories", json_body={"name": name, "parent_id": parent_id}
    )


@_relay_declines
async def create_rule(
    ctx: RunContext[PennyDeps],
    payee_contains: str,
    category_id: str | None = None,
    add_tags: list[str] | None = None,
    rename_to: str | None = None,
    mark_transfer: bool = False,
) -> dict:
    """Create a classification rule: when a transaction's payee contains
    ``payee_contains``, propose the given category, tags, rename, or
    transfer marking. Rules never rewrite history — they act on incoming
    transactions."""
    return await api_request(
        ctx.deps,
        "POST",
        "/api/v1/rules",
        json_body={
            "condition": {"payee": {"op": "contains", "value": payee_contains}},
            "action_category_id": category_id,
            "action_add_tags": add_tags or [],
            "action_rename_to": rename_to,
            "action_mark_transfer": mark_transfer,
        },
    )


@_relay_declines
async def recategorize_transaction(
    ctx: RunContext[PennyDeps],
    transaction_id: str,
    category_id: str | None = None,
    clear_category: bool = False,
    tags: list[str] | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> dict:
    """Edit a transaction's user data: category, tags (the complete new tag
    set), display name, notes. Only the fields you pass change;
    ``clear_category=true`` makes it uncategorized."""
    body: dict[str, Any] = {}
    if clear_category:
        body["category_id"] = None
    elif category_id is not None:
        body["category_id"] = category_id
    if tags is not None:
        body["tags"] = tags
    if display_name is not None:
        body["display_name"] = display_name
    if notes is not None:
        body["notes"] = notes
    return await api_request(
        ctx.deps, "PATCH", f"/api/v1/transactions/{transaction_id}", json_body=body
    )


@_relay_declines
async def accept_review(
    ctx: RunContext[PennyDeps],
    transaction_id: str,
    category_id: str | None = None,
    tags: list[str] | None = None,
    display_name: str | None = None,
) -> dict:
    """Accept an unreviewed transaction, optionally correcting the category,
    tags, or display name first — the same decision as the inbox's Accept
    button, recorded with the same weight."""
    body = {
        k: v
        for k, v in {
            "category_id": category_id,
            "tags": tags,
            "display_name": display_name,
        }.items()
        if v is not None
    }
    return await api_request(
        ctx.deps,
        "POST",
        f"/api/v1/transactions/{transaction_id}/review",
        json_body=body or None,
    )


@_relay_declines
async def mark_transfer(
    ctx: RunContext[PennyDeps],
    transaction_id: str,
    counterpart_transaction_id: str | None = None,
) -> dict:
    """Mark money movement between accounts as a transfer (excluded from
    spending). With a counterpart, the two transactions link; without one,
    the counterparty is untracked (the other account isn't in Pinch)."""
    ids = [transaction_id]
    if counterpart_transaction_id is not None:
        ids.append(counterpart_transaction_id)
    return await api_request(
        ctx.deps, "POST", "/api/v1/transfers", json_body={"transaction_ids": ids}
    )


write_bundle: Capability[PennyDeps] = Capability(
    id="pinch-writes",
    description="Change the user's data through the public API — every tool "
    "pauses for the user's explicit in-conversation approval.",
    tools=[
        Tool(tool, requires_approval=True)
        for tool in (
            recategorize_transaction,
            accept_review,
            create_rule,
            mark_transfer,
            create_category,
        )
    ],
)


read_bundle: Capability[PennyDeps] = Capability(
    id="pinch-reads",
    description="Read the user's own financial data through the public API.",
    tools=[
        list_accounts,
        search_transactions,
        get_transaction,
        spending_report,
        net_worth_report,
        debt_report,
        list_recurring_series,
        list_categories,
        list_rules,
        ledger_stats,
    ],
)
