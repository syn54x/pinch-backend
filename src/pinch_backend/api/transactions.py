"""/api/v1/transactions — the transaction list, get, and user-data PATCH
(PRD M5 #19).

The inbox and every classification screen read from this list: it inlines
the assigned category and tags (batch-fetched per page — no N+1, and never
via INNER-join traversal that would drop uncategorized rows), and orders
newest-first behind a composite (date, id) keyset cursor. current_ledger
(I-2), Page[T], allowlist responses, tenancy 404s, scope guard by
construction throughout.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from ferro import transaction
from litestar import Router, delete, get, patch, post, put
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, HTTPException, NotFoundException
from litestar.params import FromPath, QueryParameter
from litestar.status_codes import HTTP_409_CONFLICT
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend import taxonomy
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate_by_date,
)
from pinch_backend.api.transfers import TransferKind, assert_not_in_transfer, kind_of
from pinch_backend.classification.consume import AlreadyReviewedError, consume_proposal
from pinch_backend.classification.promotion import maybe_propose_rule
from pinch_backend.imports.fingerprint import compute_fingerprint, normalize_description
from pinch_backend.jobs import classify_ledger
from pinch_backend.models import (
    Account,
    Category,
    CorrectionActor,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    SplitLine,
    Tag,
    Transaction,
    TransactionTag,
    Transfer,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import apply_tag_set, dedupe_tag_names

log = get_logger(__name__)


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str


class TagRef(BaseModel):
    id: uuid.UUID
    name: str


class SplitLineOut(BaseModel):
    """One line of the split document (M6 CP1) — amount, resolved category,
    memo. No line id: ids are not durable across a re-PUT, so nothing a
    client could hold is exposed."""

    amount_minor: int
    category: CategoryRef | None
    memo: str | None


class TransferRef(BaseModel):
    """The transfer membership riding the transaction (M6 CP2): enough for
    the register to render the link without a second request. Counterpart
    fields are null on an untracked transfer."""

    id: uuid.UUID
    kind: TransferKind
    counterpart_transaction_id: uuid.UUID | None
    counterpart_account_id: uuid.UUID | None


class ProposalOut(BaseModel):
    """The pending pipeline suggestion riding the transaction (M5 CP3) —
    enough for the inbox to render from the list alone."""

    category: CategoryRef | None
    tags: list[str]
    display_name: str | None
    proposed_transfer: bool
    provenance: ProposalProvenance


class TransactionOut(BaseModel):
    """What a client may see about a transaction — an allowlist (M5 CP1).
    ``proposal`` inlines the pending pipeline suggestion, if any (M5 CP3)."""

    id: uuid.UUID
    account_id: uuid.UUID
    date: date
    amount_minor: int
    currency: str
    description_raw: str
    description_normalized: str
    display_name: str | None
    notes: str | None
    reviewed_at: datetime | None
    category: CategoryRef | None
    tags: list[TagRef]
    proposal: ProposalOut | None
    splits: list[SplitLineOut] | None
    """The split document in creation order, or null when unsplit (M6 CP1).
    Non-null implies ``category`` is null — exactly one layer holds
    categories."""
    transfer: TransferRef | None
    """Transfer membership, or null (M6 CP2). Non-null implies ``category``
    is null — being a transfer is the classification."""
    created_at: datetime


class TransactionPatchIn(BaseModel):
    """User-data allowlist (M5). Only the fields present in the request body
    are applied — source data (date, amount, description, fingerprint) is not
    addressable here, and unknown keys are a 400 (extra="forbid"), never a
    silently accepted no-op."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    category_id: uuid.UUID | None = None
    """Present-and-null clears the category (→ uncategorized)."""
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    """The complete tag set for the transaction; reconciled (implicit-create
    new names, detach removed ones). Present-and-empty clears all tags, and
    an explicit null does the same (both mean "no tags"). Each
    name is bounded to the same 100 chars POST /tags enforces on this table,
    and the set is capped so one PATCH can't mint an unbounded tag list."""
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    """An override of the raw description; NULL shows description_raw (an
    override, never a copy). Empty is rejected — clear with null, not ""."""
    notes: str | None = Field(default=None, max_length=2000)
    reviewed: bool | None = None
    """True sets reviewed_at to now; False clears it (back to the inbox)."""


async def _get(ledger: Ledger, txn_id: uuid.UUID) -> Transaction:
    txn = await Transaction.where(lambda t: (t.id == txn_id) & (t.ledger_id == ledger.id)).first()
    if txn is None:
        raise NotFoundException(detail="No such transaction")
    return txn


async def _current_tag_names(txn: Transaction) -> list[str]:
    txn_id = txn.id
    links = await TransactionTag.where(lambda tt, tid=txn_id: tt.transaction_id == tid).all()
    tag_ids = sorted({link.tag_id for link in links})  # ty: ignore[unresolved-attribute]
    if not tag_ids:
        return []
    rows = await Tag.where(lambda t, ids=tag_ids: t.id.in_(ids)).all()
    return sorted((t.name for t in rows), key=str.casefold)


async def hydrate_transactions(txns: list[Transaction]) -> list[TransactionOut]:
    """Batch-hydrate categories, tags, and pending proposals for a page in a
    fixed number of queries, never per-row. Public: the rules preview (CP2)
    reuses it."""
    txn_ids = [t.id for t in txns]
    links = (
        await TransactionTag.where(lambda tt: tt.transaction_id.in_(txn_ids)).all()
        if txn_ids
        else []
    )
    tag_ids = sorted({link.tag_id for link in links})  # ty: ignore[unresolved-attribute]
    tags = {t.id: t for t in await Tag.where(lambda t: t.id.in_(tag_ids)).all()} if tag_ids else {}
    by_txn: dict[uuid.UUID, list[TagRef]] = {}
    for link in links:
        tag = tags[link.tag_id]  # ty: ignore[unresolved-attribute]
        by_txn.setdefault(link.transaction_id, []).append(  # ty: ignore[unresolved-attribute]
            TagRef(id=tag.id, name=tag.name)
        )
    for refs in by_txn.values():
        refs.sort(key=lambda ref: ref.name.casefold())

    proposals = (
        await Proposal.where(lambda p, ids=txn_ids: p.transaction_id.in_(ids)).all()
        if txn_ids
        else []
    )
    by_txn_proposal = {p.transaction_id: p for p in proposals}  # ty: ignore[unresolved-attribute]
    proposal_ids = [p.id for p in proposals]
    proposal_tag_rows = (
        await ProposalTag.where(lambda pt, ids=proposal_ids: pt.proposal_id.in_(ids)).all()
        if proposal_ids
        else []
    )
    tags_by_proposal: dict[uuid.UUID, list[str]] = {}
    for pt in sorted(proposal_tag_rows, key=lambda pt: pt.name.casefold()):
        tags_by_proposal.setdefault(pt.proposal_id, []).append(pt.name)  # ty: ignore[unresolved-attribute]

    lines = (
        await SplitLine.where(lambda ln: ln.transaction_id.in_(txn_ids)).all() if txn_ids else []
    )
    lines_by_txn: dict[uuid.UUID, list[SplitLine]] = {}
    for ln in sorted(lines, key=lambda ln: ln.id):  # uuid7: creation = document order
        lines_by_txn.setdefault(ln.transaction_id, []).append(ln)  # ty: ignore[unresolved-attribute]

    transfers = (
        await Transfer.where(
            lambda tr: (
                (tr.outflow_transaction_id.in_(txn_ids)) | (tr.inflow_transaction_id.in_(txn_ids))
            )
        ).all()
        if txn_ids
        else []
    )
    transfer_by_txn: dict[uuid.UUID, Transfer] = {}
    for tr in transfers:
        for member in (tr.outflow_transaction_id, tr.inflow_transaction_id):  # ty: ignore[unresolved-attribute]
            if member is not None:
                transfer_by_txn[member] = tr
    # Counterpart account ids: the other side may not be on this page.
    counterpart_ids = sorted(set(transfer_by_txn) - set(txn_ids))
    account_by_txn = {t.id: t.account_id for t in txns}  # ty: ignore[unresolved-attribute]
    if counterpart_ids:
        others = await Transaction.where(lambda t, ids=counterpart_ids: t.id.in_(ids)).all()
        account_by_txn |= {t.id: t.account_id for t in others}  # ty: ignore[unresolved-attribute]

    cat_ids = sorted(
        {t.category_id for t in txns if t.category_id is not None}  # ty: ignore[unresolved-attribute]
        | {p.category_id for p in proposals if p.category_id is not None}  # ty: ignore[unresolved-attribute]
        | {ln.category_id for ln in lines if ln.category_id is not None}  # ty: ignore[unresolved-attribute]
    )
    cats = (
        {c.id: c for c in await Category.where(lambda c: c.id.in_(cat_ids)).all()}
        if cat_ids
        else {}
    )

    result = []
    for t in txns:
        cat = cats.get(t.category_id) if t.category_id else None  # ty: ignore[unresolved-attribute]
        txn_lines = lines_by_txn.get(t.id)
        splits_out = (
            [
                SplitLineOut(
                    amount_minor=ln.amount_minor,
                    category=(
                        CategoryRef(id=lcat.id, name=lcat.name)
                        if ln.category_id and (lcat := cats.get(ln.category_id))  # ty: ignore[unresolved-attribute]
                        else None
                    ),
                    memo=ln.memo,
                )
                for ln in txn_lines
            ]
            if txn_lines
            else None
        )
        transfer = transfer_by_txn.get(t.id)
        transfer_ref = None
        if transfer is not None:
            counterpart = (
                transfer.inflow_transaction_id  # ty: ignore[unresolved-attribute]
                if transfer.outflow_transaction_id == t.id  # ty: ignore[unresolved-attribute]
                else transfer.outflow_transaction_id  # ty: ignore[unresolved-attribute]
            )
            transfer_ref = TransferRef(
                id=transfer.id,
                kind=kind_of(transfer),
                counterpart_transaction_id=counterpart,
                counterpart_account_id=account_by_txn.get(counterpart) if counterpart else None,
            )
        proposal = by_txn_proposal.get(t.id)
        proposal_out = None
        if proposal is not None:
            pcat = cats.get(proposal.category_id) if proposal.category_id else None  # ty: ignore[unresolved-attribute]
            proposal_out = ProposalOut(
                category=CategoryRef(id=pcat.id, name=pcat.name) if pcat else None,
                tags=tags_by_proposal.get(proposal.id, []),
                display_name=proposal.proposed_display_name,
                proposed_transfer=proposal.proposed_transfer,
                provenance=proposal.provenance,
            )
        result.append(
            TransactionOut(
                id=t.id,
                account_id=t.account_id,  # ty: ignore[unresolved-attribute]
                date=t.date,
                amount_minor=t.amount_minor,
                currency=t.currency,
                description_raw=t.description_raw,
                description_normalized=t.description_normalized,
                display_name=t.display_name,
                notes=t.notes,
                reviewed_at=t.reviewed_at,
                category=CategoryRef(id=cat.id, name=cat.name) if cat else None,
                tags=by_txn.get(t.id, []),
                proposal=proposal_out,
                splits=splits_out,
                transfer=transfer_ref,
                created_at=t.created_at,
            )
        )
    return result


@get("/")
async def list_transactions(
    current_ledger: NamedDependency[Ledger],
    account_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    date_from: Annotated[date | None, QueryParameter()] = None,
    date_to: Annotated[date | None, QueryParameter()] = None,
    reviewed: Annotated[bool | None, QueryParameter()] = None,
    category_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    uncategorized: Annotated[bool | None, QueryParameter()] = None,
    tag: Annotated[list[str] | None, QueryParameter()] = None,
    is_transfer: Annotated[bool | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TransactionOut]:
    ledger_id = current_ledger.id
    query = Transaction.where(lambda t: t.ledger_id == ledger_id)

    if account_id:
        accounts = list(account_id)
        query = query.where(lambda t: t.account_id.in_(accounts))
    if date_from is not None:
        start = date_from
        query = query.where(lambda t: t.date >= start)
    if date_to is not None:
        end = date_to
        query = query.where(lambda t: t.date <= end)
    if reviewed is True:
        query = query.where(lambda t: t.reviewed_at != None)  # noqa: E711
    elif reviewed is False:
        query = query.where(lambda t: t.reviewed_at == None)  # noqa: E711
    if uncategorized is True:
        query = query.where(lambda t: t.category_id == None)  # noqa: E711
    elif uncategorized is False:
        query = query.where(lambda t: t.category_id != None)  # noqa: E711
    # Membership derives spending exclusion — one existence test per side,
    # never a stored flag (M6 CP2).
    if is_transfer is True:
        query = query.where(lambda t: t.transfer_out.exists() | t.transfer_in.exists())
    elif is_transfer is False:
        query = query.where(lambda t: ~(t.transfer_out.exists() | t.transfer_in.exists()))
    if category_id:
        subtree = await taxonomy.collect_descendant_ids(list(category_id), ledger_id)
        ids = sorted(subtree)
        # Line-aware (M6 CP1): a split transaction matches through any line's
        # category — splits must never make transactions less findable. The
        # existence test renders a correlated EXISTS (root-shaped, ferro
        # 0.17.0), so a multi-line split still matches once.
        query = query.where(
            lambda t: (
                (t.category_id.in_(ids))
                | (t.split_lines.exists(lambda ln: ln.category_id.in_(ids)))
            )
        )
    if tag:
        wanted = list(tag)
        wanted_folds = sorted({name.strip().casefold() for name in wanted})
        matched_tags = await Tag.where(
            lambda t: (t.ledger_id == ledger_id) & (t.name_fold.in_(wanted_folds))
        ).all()
        if len(matched_tags) < len(wanted_folds):
            return Page(items=[], next_cursor=None)  # an unknown tag matches nothing
        keep: set[uuid.UUID] | None = None
        for tg in matched_tags:
            tid = tg.id
            links = await TransactionTag.where(lambda tt, tid=tid: tt.tag_id == tid).all()
            ids_for_tag = {link.transaction_id for link in links}  # ty: ignore[unresolved-attribute]
            keep = ids_for_tag if keep is None else (keep & ids_for_tag)
        # Scaling seam: this materializes every matching transaction id before
        # the keyset page, partly defeating keyset pagination at large scale.
        # Acceptable at CP1 data volumes; revisit when the inbox query is
        # optimized.
        keep_ids = sorted(keep or set())
        if not keep_ids:
            return Page(items=[], next_cursor=None)
        query = query.where(lambda t: t.id.in_(keep_ids))

    rows, next_cursor = await paginate_by_date(query, cursor=cursor, limit=limit)
    return Page(items=await hydrate_transactions(rows), next_cursor=next_cursor)


@get("/{txn_id:uuid}")
async def get_transaction(
    txn_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> TransactionOut:
    txn = await _get(current_ledger, txn_id)
    (out,) = await hydrate_transactions([txn])
    return out


@patch("/{txn_id:uuid}")
async def patch_transaction(
    txn_id: FromPath[uuid.UUID],
    data: TransactionPatchIn,
    current_ledger: NamedDependency[Ledger],
) -> TransactionOut:
    txn = await _get(current_ledger, txn_id)
    fields = data.model_fields_set

    # Validation reads happen before the transaction: a foreign/missing
    # category is a 404 that shouldn't open (and roll back) a write scope.
    category_id: uuid.UUID | None = None
    if "category_id" in fields and data.category_id is not None:
        category = await Category.where(
            lambda c: (c.id == data.category_id) & (c.ledger_id == current_ledger.id)
        ).first()
        if category is None:
            raise NotFoundException(detail="No such category")
        category_id = category.id

    reviewing = "reviewed" in fields and data.reviewed is True and txn.reviewed_at is None
    unreviewing = "reviewed" in fields and data.reviewed is False and txn.reviewed_at is not None

    if reviewing:
        # Setting reviewed always consumes and logs (M5 CP4): the pending
        # proposal is consumed with the transaction's post-PATCH state as
        # the decision — the final state IS the decision. consume_proposal
        # saves the row, so in-memory mutations ride its transaction.
        if "category_id" in fields:
            txn.category_id = category_id  # ty: ignore[unresolved-attribute]
        if "display_name" in fields:
            txn.display_name = data.display_name
        if "notes" in fields:
            txn.notes = data.notes
        final_tags = (
            dedupe_tag_names(list(data.tags or []))
            if "tags" in fields
            else await _current_tag_names(txn)
        )
        try:
            await consume_proposal(
                current_ledger,
                txn,
                category_id=txn.category_id,  # ty: ignore[unresolved-attribute]
                tags=final_tags,
                display_name=txn.display_name,
                actor=CorrectionActor.USER,
            )
        except AlreadyReviewedError:
            # A concurrent decision won between this handler's reviewed_at
            # read and consume's in-transaction CAS claim.
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail="Already reviewed; un-review first (PATCH reviewed: false)",
            ) from None
        await maybe_propose_rule(
            current_ledger,
            txn.description_normalized,
            txn.category_id,  # ty: ignore[unresolved-attribute]
        )
    else:
        async with transaction():
            if "category_id" in fields:
                txn.category_id = category_id  # ty: ignore[unresolved-attribute]
            if "display_name" in fields:
                txn.display_name = data.display_name
            if "notes" in fields:
                txn.notes = data.notes
            if unreviewing:
                # Transition-only write: reviewed:true here can only mean
                # already-reviewed (a no-op — never bump the original review
                # timestamp), and reviewed:false on an unreviewed row is
                # None -> None, safe to skip.
                txn.reviewed_at = None
            await txn.save()
            if "tags" in fields:
                await apply_tag_set(current_ledger, txn, data.tags or [])
        if unreviewing:
            # Deferred AFTER the transaction commits (the imports.py
            # precedent) so the round-trip is prompt: un-review -> sweep
            # re-proposes -> re-review appends; earlier entries stand.
            await classify_ledger.configure(lock=f"ledger:{current_ledger.id}").defer_async(
                ledger_id=str(current_ledger.id), auto_file_import_id=None
            )

    log.info("transaction.updated", transaction_id=str(txn.id), ledger_id=str(current_ledger.id))
    (out,) = await hydrate_transactions([txn])
    return out


class SplitLineIn(BaseModel):
    """One line of the split document (M6 CP1). Unknown keys are a 400
    (extra="forbid")."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    amount_minor: int = Field(ge=-2_147_483_647, le=2_147_483_647)
    """Signed from the account's perspective, same convention as the parent;
    bounded to the int4 column width so an out-of-range amount is a 400."""
    category_id: uuid.UUID | None = None
    """NULL = an uncategorized line (legal — same rule as transactions)."""
    memo: str | None = Field(default=None, min_length=1, max_length=500)
    """Optional free-form label. Empty is rejected — omit or null instead."""


MAX_SPLIT_LINES = 100
"""Document cap: a split is a receipt's worth of lines, not a bulk-insert
surface. Same bounded-input stance as the 50-tag cap on PATCH."""


def validate_split_document(txn: Transaction, data: list[SplitLineIn]) -> None:
    """The whole-document invariants (PRD M6): validates-all-first — any
    failure rejects the document before anything persists. A zero-amount
    parent is unsplittable by construction: no nonzero parent-signed line
    set can sum to zero."""
    if len(data) < 2:
        raise ClientException(detail="A split needs at least two lines")
    if len(data) > MAX_SPLIT_LINES:
        raise ClientException(detail=f"A split is capped at {MAX_SPLIT_LINES} lines")
    parent_negative = txn.amount_minor < 0
    for line in data:
        if line.amount_minor == 0:
            raise ClientException(detail="Split lines must be nonzero")
        if (line.amount_minor < 0) != parent_negative:
            raise ClientException(detail="Split lines must carry the parent transaction's sign")
    total = sum(line.amount_minor for line in data)
    if total != txn.amount_minor:
        raise ClientException(
            detail=(
                f"Split lines must sum exactly to the parent amount ({total} != {txn.amount_minor})"
            )
        )


async def resolve_split_categories(ledger: Ledger, data: list[SplitLineIn]) -> None:
    """Every named line category must exist in the acting ledger — a foreign
    or unknown id is a 404 with no existence leak. Public: the review-with-
    splits motion (CP3) validates the same document."""
    wanted = sorted({line.category_id for line in data if line.category_id is not None})
    if not wanted:
        return
    ledger_id = ledger.id
    found = await Category.where(
        lambda c, ids=wanted, lid=ledger_id: (c.id.in_(ids)) & (c.ledger_id == lid)
    ).all()
    if len(found) < len(wanted):
        raise NotFoundException(detail="No such category")


async def replace_split_lines(ledger: Ledger, txn: Transaction, data: list[SplitLineIn]) -> None:
    """Swap the split document wholesale and vacate the parent category.
    Callers wrap in a transaction (put_splits' own, or the review motion's
    outer one — CP3); validation happens before this is reached."""
    txn_id = txn.id
    await SplitLine.where(lambda ln, tid=txn_id: ln.transaction_id == tid).delete()
    for line in data:
        await SplitLine.create(
            ledger=ledger,
            transaction=txn,
            amount_minor=line.amount_minor,
            category_id=line.category_id,
            memo=line.memo,
        )
    txn.category_id = None  # ty: ignore[unresolved-attribute]


@put("/{txn_id:uuid}/splits")
async def put_splits(
    txn_id: FromPath[uuid.UUID],
    data: list[SplitLineIn],
    current_ledger: NamedDependency[Ledger],
) -> TransactionOut:
    """Replace the split document wholesale (M6 CP1): one atomic write that
    validates-all-first, vacates the parent category (exactly one layer
    holds categories), and never touches review state or the correction log
    — edits edit, review reviews. Line ids are not durable across a re-PUT."""
    txn = await _get(current_ledger, txn_id)
    await assert_not_in_transfer(txn_id)  # split x transfer exclusivity (CP3)
    validate_split_document(txn, data)
    await resolve_split_categories(current_ledger, data)

    async with transaction():
        await replace_split_lines(current_ledger, txn, data)
        await txn.save()
    log.info(
        "transaction.split",
        transaction_id=str(txn.id),
        ledger_id=str(current_ledger.id),
        lines=len(data),
    )
    (out,) = await hydrate_transactions([txn])
    return out


@delete("/{txn_id:uuid}/splits")
async def delete_splits(
    txn_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
) -> None:
    """Unsplit (M6 CP1): the lines go, the parent stays exactly as it is —
    uncategorized (vacating is not undone) and with its review state
    untouched. An unsplit transaction has no split document to delete: 404."""
    txn = await _get(current_ledger, txn_id)
    existing = await SplitLine.where(lambda ln, tid=txn_id: ln.transaction_id == tid).count()
    if existing == 0:
        raise NotFoundException(detail="Transaction is not split")
    await SplitLine.where(lambda ln, tid=txn_id: ln.transaction_id == tid).delete()
    log.info("transaction.unsplit", transaction_id=str(txn.id), ledger_id=str(current_ledger.id))


class TransactionCreateIn(BaseModel):
    """Manual entry (M5 CP4): source fields + the full optional user-data
    set. Manual accounts only; the currency is always the account's. With
    category or tags the transaction is reviewed at birth; display_name and
    notes alone are annotations, not decisions. Unknown keys are a 400
    (extra="forbid")."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    account_id: uuid.UUID
    date: date
    amount_minor: int = Field(ge=-2_147_483_647, le=2_147_483_647)
    """Signed from the account's perspective — negative is money out.
    Bounded to the int4 column width so an out-of-range amount is a 400,
    never a database error."""
    description: str = Field(min_length=1, max_length=500)
    category_id: uuid.UUID | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


@post("/")
async def create_transaction(
    data: TransactionCreateIn, current_ledger: NamedDependency[Ledger]
) -> TransactionOut:
    ledger_id = current_ledger.id
    wanted_account = data.account_id
    account = await Account.where(
        lambda a, aid=wanted_account, lid=ledger_id: (a.id == aid) & (a.ledger_id == lid)
    ).first()
    if account is None:
        raise NotFoundException(detail="No such account")
    if account.connection_id is not None:  # ty: ignore[unresolved-attribute]
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail="Manual entry is for manual accounts"
        )
    if data.category_id is not None:
        wanted = data.category_id
        category = await Category.where(
            lambda c, cid=wanted, lid=ledger_id: (c.id == cid) & (c.ledger_id == lid)
        ).first()
        if category is None:
            raise NotFoundException(detail="No such category")

    decided = data.category_id is not None or bool(data.tags)
    tags = dedupe_tag_names(list(data.tags or []))
    async with transaction():
        txn = await Transaction.create(
            ledger=current_ledger,
            account=account,
            date=data.date,
            amount_minor=data.amount_minor,
            currency=account.currency,
            description_raw=data.description,
            description_normalized=normalize_description(data.description),
            fingerprint=compute_fingerprint(
                account.id, data.date, data.amount_minor, data.description
            ),
            display_name=data.display_name,
            notes=data.notes,
        )
        if decided:
            # Reviewed at birth: no proposal exists, so consume snapshots
            # provenance=none — the pipeline never ran. Nested transaction
            # is atomic with the outer (scratch-verified): no observable
            # state where the row exists categorized-but-unlogged.
            # AlreadyReviewedError cannot fire here — the row was created
            # unreviewed inside this same transaction, so no concurrent
            # decision can have claimed it; no handler on purpose.
            await consume_proposal(
                current_ledger,
                txn,
                category_id=data.category_id,
                tags=tags,
                display_name=data.display_name,
                actor=CorrectionActor.USER,
            )
    rule = None
    if decided:
        rule = await maybe_propose_rule(
            current_ledger, txn.description_normalized, data.category_id
        )
    else:
        await classify_ledger.configure(lock=f"ledger:{ledger_id}").defer_async(
            ledger_id=str(ledger_id), auto_file_import_id=None
        )
    log.info(
        "transaction.created",
        transaction_id=str(txn.id),
        ledger_id=str(ledger_id),
        reviewed=decided,
        promoted_rule_id=str(rule.id) if rule else None,
    )
    (out,) = await hydrate_transactions([txn])
    return out


transactions_router = Router(
    path="/api/v1/transactions",
    route_handlers=[
        list_transactions,
        get_transaction,
        patch_transaction,
        create_transaction,
        put_splits,
        delete_splits,
    ],
)
