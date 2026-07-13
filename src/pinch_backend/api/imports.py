"""/api/v1/imports — the CSV import lifecycle (PRD M4, issue #15).

Upload creates a batch that touches nothing; mapping confirmation parses
rows into a preview; commit is one synchronous atomic transaction; DELETE
discards. Same conventions as every domain surface: ``current_ledger``
(I-2), ``Page[T]`` lists, allowlist responses, tenancy 404s, and the scope
guard by construction on every unsafe method.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from ferro import transaction
from litestar import Router, delete, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.exceptions import ClientException, HTTPException, NotFoundException
from litestar.params import Body, FromPath
from litestar.status_codes import HTTP_200_OK, HTTP_409_CONFLICT
from pydantic import BaseModel, ConfigDict

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.imports import inference
from pinch_backend.imports.fingerprint import compute_fingerprint
from pinch_backend.imports.parsing import currency_exponent, parse_rows
from pinch_backend.imports.spec import MappingSpec
from pinch_backend.models import (
    Account,
    Import,
    ImportRow,
    ImportStatus,
    Ledger,
    Transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


class ImportUploadIn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: uuid.UUID
    file: UploadFile


class CommitIn(BaseModel):
    """Empty for now; CP3 (#16) adds per-row duplicate overrides here."""


class ImportOut(BaseModel):
    """What a client may see about an import — an allowlist, never the row.
    The raw bytes never leave the server."""

    id: uuid.UUID
    account_id: uuid.UUID
    filename: str
    status: ImportStatus
    suggested_mapping: MappingSpec | None
    confirmed_mapping: MappingSpec | None
    row_count: int | None
    valid_row_count: int | None
    error_row_count: int | None
    transaction_count: int | None
    created_at: datetime


class ImportRowOut(BaseModel):
    """One preview row: parsed values where parsing succeeded, errors where
    it didn't (story 7)."""

    id: uuid.UUID
    row_index: int
    raw_cells: list[str]
    date: date | None
    amount_minor: int | None
    currency: str
    description_raw: str | None
    errors: list[str]


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=HTTP_409_CONFLICT, detail=detail)


async def _get_import(ledger: Ledger, import_id: uuid.UUID) -> Import:
    """Scoped to the acting ledger: another ledger's import answers the
    same 404 as a nonexistent one."""
    batch = await Import.where(lambda i: (i.id == import_id) & (i.ledger_id == ledger.id)).first()
    if batch is None:
        raise NotFoundException(detail="No such import")
    return batch


async def _import_out(batch: Import) -> ImportOut:
    batch_id = batch.id
    row_count: int | None = None
    valid_count: int | None = None
    error_count: int | None = None
    transaction_count: int | None = None
    if batch.status in (ImportStatus.PREVIEWED, ImportStatus.COMMITTED):
        row_count = await ImportRow.where(lambda r: r.import_batch_id == batch_id).count()
        valid_count = await ImportRow.where(
            lambda r: (r.import_batch_id == batch_id) & (r.valid == True)  # noqa: E712
        ).count()
        error_count = row_count - valid_count
    if batch.status is ImportStatus.COMMITTED:
        transaction_count = await Transaction.where(
            lambda t: t.source_import_id == batch_id
        ).count()
    return ImportOut(
        id=batch.id,
        account_id=batch.account_id,  # ty: ignore[unresolved-attribute]
        filename=batch.filename,
        status=batch.status,
        suggested_mapping=(
            MappingSpec(**batch.suggested_mapping) if batch.suggested_mapping else None
        ),
        confirmed_mapping=(
            MappingSpec(**batch.confirmed_mapping) if batch.confirmed_mapping else None
        ),
        row_count=row_count,
        valid_row_count=valid_count,
        error_row_count=error_count,
        transaction_count=transaction_count,
        created_at=batch.created_at,
    )


def _row_out(row: ImportRow, currency: str) -> ImportRowOut:
    return ImportRowOut(
        id=row.id,
        row_index=row.row_index,
        raw_cells=row.raw_cells,
        date=row.date,
        amount_minor=row.amount_minor,
        currency=currency,
        description_raw=row.description_raw,
        errors=row.errors,
    )


@post("/")
async def create_import(
    data: Annotated[ImportUploadIn, Body(media_type=RequestEncodingType.MULTI_PART)],
    current_ledger: NamedDependency[Ledger],
) -> ImportOut:
    """Multipart upload onto a manual account (story 4): creates an
    ``uploaded`` batch carrying a suggested mapping; nothing touches the
    ledger. The caps make the synchronous commit honest (PRD M4)."""
    account_id = data.account_id
    ledger_id = current_ledger.id
    account = await Account.where(
        lambda a: (a.id == account_id) & (a.ledger_id == ledger_id)
    ).first()
    if account is None:
        raise NotFoundException(detail="No such account")
    if account.connection_id is not None:  # ty: ignore[unresolved-attribute]
        raise ClientException(detail="File imports are for manual accounts")

    content = await data.file.read()
    if len(content) > settings.import_max_bytes:
        raise ClientException(
            detail=f"File exceeds the {settings.import_max_bytes}-byte import limit"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ClientException(detail="File is not valid UTF-8 text") from None

    suggestion = await inference.active_inferrer.suggest(text)
    # The honest row count arrives when a confirmed mapping parses the file;
    # at upload the suggestion's read of the shape (or line count, without
    # one) bounds obvious oversends. Confirm re-checks against real rows.
    approximate_rows = (
        len(parse_rows(text, suggestion, exponent=2))
        if suggestion
        else sum(1 for line in text.splitlines() if line.strip())
    )
    if approximate_rows > settings.import_max_rows:
        raise ClientException(
            detail=f"File exceeds the {settings.import_max_rows}-row import limit"
        )

    batch = await Import.create(
        ledger=current_ledger,
        account=account,
        filename=data.file.filename,
        file_bytes=content,
        suggested_mapping=suggestion.model_dump() if suggestion else None,
    )
    log.info(
        "import.created",
        import_id=str(batch.id),
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
        filename=data.file.filename,
        bytes=len(content),
    )
    return await _import_out(batch)


@get("/{import_id:uuid}")
async def get_import(
    import_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> ImportOut:
    return await _import_out(await _get_import(current_ledger, import_id))


@post("/{import_id:uuid}/mapping", status_code=HTTP_200_OK)
async def confirm_mapping(
    import_id: FromPath[uuid.UUID],
    data: MappingSpec,
    current_ledger: NamedDependency[Ledger],
) -> ImportOut:
    """Confirm or correct the mapping (story 5): parses every record with
    the confirmed spec and lands the batch at ``previewed``. Re-confirming
    re-parses — rows are replaced, never appended — because mapping is a
    review, and reviews get second looks."""
    batch = await _get_import(current_ledger, import_id)
    if batch.status is ImportStatus.COMMITTED:
        raise _conflict("This import is committed; undo it to change the mapping")
    account = await Account.get(batch.account_id)  # ty: ignore[unresolved-attribute]

    text = batch.file_bytes.decode("utf-8-sig")
    parsed = parse_rows(text, data, exponent=currency_exponent(account.currency))
    if len(parsed) > settings.import_max_rows:
        raise ClientException(
            detail=f"File exceeds the {settings.import_max_rows}-row import limit"
        )

    batch_id = batch.id
    async with transaction():
        batch.confirmed_mapping = data.model_dump()
        batch.status = ImportStatus.MAPPED
        await batch.save()
        await ImportRow.where(lambda r: r.import_batch_id == batch_id).delete()
        rows = [
            # Shadow-FK constructor kwargs are runtime-synthesized and
            # invisible to ty (ferro PRD 0004 / ferro-orm#290).
            ImportRow(  # ty: ignore[missing-argument]
                ledger_id=current_ledger.id,  # ty: ignore[unknown-argument]
                import_batch_id=batch.id,  # ty: ignore[unknown-argument]
                row_index=index,
                raw_cells=row.raw_cells,
                date=row.date,
                amount_minor=row.amount_minor,
                description_raw=row.description_raw,
                valid=row.valid,
                errors=row.errors,
            )
            for index, row in enumerate(parsed)
        ]
        if rows:
            await ImportRow.bulk_create(rows)
        batch.status = ImportStatus.PREVIEWED
        await batch.save()
    log.info(
        "import.previewed",
        import_id=str(batch.id),
        ledger_id=str(current_ledger.id),
        rows=len(parsed),
        valid_rows=sum(1 for row in parsed if row.valid),
    )
    return await _import_out(batch)


@get("/{import_id:uuid}/rows")
async def list_import_rows(
    import_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[ImportRowOut]:
    """The preview, paginated (story 6): uuid7 order is creation order is
    file order, so pages read like the file does."""
    batch = await _get_import(current_ledger, import_id)
    account = await Account.get(batch.account_id)  # ty: ignore[unresolved-attribute]
    batch_id = batch.id
    rows, next_cursor = await paginate(
        ImportRow.where(lambda r: r.import_batch_id == batch_id), cursor=cursor, limit=limit
    )
    return Page(items=[_row_out(row, account.currency) for row in rows], next_cursor=next_cursor)


@post("/{import_id:uuid}/commit", status_code=HTTP_200_OK)
async def commit_import(
    import_id: FromPath[uuid.UUID],
    data: CommitIn,
    current_ledger: NamedDependency[Ledger],
) -> ImportOut:
    """One atomic batch (story 9): every valid row becomes a Transaction —
    with its stored fingerprint — inside a single database transaction, or
    none do. Anything a commit *triggers* (M5 classification, M10 webhooks)
    is the reacting subsystem's background job, never this request's."""
    batch = await _get_import(current_ledger, import_id)
    if batch.status is not ImportStatus.PREVIEWED:
        raise _conflict("Only a previewed import can be committed")
    account = await Account.get(batch.account_id)  # ty: ignore[unresolved-attribute]

    batch_id = batch.id
    included = await ImportRow.where(
        lambda r: (r.import_batch_id == batch_id) & (r.valid == True)  # noqa: E712
    ).all()
    async with transaction():
        transactions = [
            # Shadow-FK kwargs: ferro PRD 0004 / ferro-orm#290. The None
            # ignores are guarded by the SQL filter: valid rows have dates
            # and amounts by construction.
            Transaction(  # ty: ignore[missing-argument]
                ledger_id=current_ledger.id,  # ty: ignore[unknown-argument]
                account_id=account.id,  # ty: ignore[unknown-argument]
                date=row.date,  # ty: ignore[invalid-argument-type]
                amount_minor=row.amount_minor,  # ty: ignore[invalid-argument-type]
                currency=account.currency,
                description_raw=row.description_raw or "",
                source_import_id=batch.id,  # ty: ignore[unknown-argument]
                fingerprint=compute_fingerprint(
                    account.id,
                    row.date,  # ty: ignore[invalid-argument-type] — valid rows have dates
                    row.amount_minor,  # ty: ignore[invalid-argument-type]
                    row.description_raw or "",
                ),
            )
            for row in included
        ]
        if transactions:
            await Transaction.bulk_create(transactions)
        batch.status = ImportStatus.COMMITTED
        await batch.save()
    log.info(
        "import.committed",
        import_id=str(batch.id),
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
        transactions=len(included),
    )
    return await _import_out(batch)


@delete("/{import_id:uuid}")
async def delete_import(
    import_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Discard an uncommitted import: the batch and its rows go atomically;
    the audit trail is the structured event (story 4). Committed imports
    409 until CP3 (#16) makes DELETE the unconditional undo."""
    batch = await _get_import(current_ledger, import_id)
    if batch.status is ImportStatus.COMMITTED:
        raise _conflict("Committed imports are undone in CP3")
    batch_id = batch.id
    async with transaction():
        await ImportRow.where(lambda r: r.import_batch_id == batch_id).delete()
        await batch.delete()
    log.info(
        "import.discarded",
        import_id=str(batch_id),
        ledger_id=str(current_ledger.id),
        status=batch.status.value,
    )


imports_router = Router(
    path="/api/v1/imports",
    route_handlers=[
        create_import,
        get_import,
        confirm_mapping,
        list_import_rows,
        commit_import,
        delete_import,
    ],
)
