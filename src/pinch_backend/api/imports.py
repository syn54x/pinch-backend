"""/api/v1/imports — the CSV import lifecycle (PRD M4, issue #15).

Upload creates a batch that touches nothing; mapping confirmation parses
rows into a preview; commit is one synchronous atomic transaction; DELETE
discards. Same conventions as every domain surface: ``current_ledger``
(I-2), ``Page[T]`` lists, allowlist responses, tenancy 404s, and the scope
guard by construction on every unsafe method.
"""

import csv
import io
import uuid
from collections import Counter
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
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.imports import inference
from pinch_backend.imports.fingerprint import compute_fingerprint, normalize_description
from pinch_backend.imports.parsing import (
    ParsedRow,
    currency_exponent,
    parse_rows,
    record_parses_as_data,
)
from pinch_backend.imports.profiles import normalized_header_tuple, shape_key
from pinch_backend.imports.spec import MappingSpec
from pinch_backend.models import (
    Account,
    Import,
    ImportProfile,
    ImportRow,
    ImportStatus,
    Ledger,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


class ImportUploadIn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: uuid.UUID
    file: UploadFile


class CommitIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    include_duplicates: list[uuid.UUID] = Field(default_factory=list)
    """Row ids to commit despite their duplicate flag (story 8) — the
    two-identical-coffees escape hatch, per row and explicit."""


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
    duplicate: bool
    errors: list[str]


class ImportProfileOut(BaseModel):
    """One saved file shape — an allowlist, never the row."""

    id: uuid.UUID
    header_tuple: list[str]
    delimiter: str
    mapping: MappingSpec
    created_at: datetime


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
        duplicate=row.duplicate,
        errors=row.errors,
    )


def _parse_within_caps(text: str, spec: MappingSpec, account: Account) -> list[ParsedRow]:
    parsed = parse_rows(text, spec, exponent=currency_exponent(account.currency))
    if len(parsed) > settings.import_max_rows:
        raise ClientException(
            detail=f"File exceeds the {settings.import_max_rows}-row import limit"
        )
    return parsed


async def _fingerprint_and_flag(
    account_id: uuid.UUID, parsed: list[ParsedRow]
) -> list[tuple[str | None, bool]]:
    """Per parsed row: (fingerprint, duplicate) — flagged against existing
    transactions AND other rows of this file (CONTEXT.md: Duplicate flag).
    Within-file collisions flag every colliding row, not just the second:
    both identical coffees deserve the user's eyes."""
    fingerprints = [
        compute_fingerprint(
            account_id,
            row.date,  # ty: ignore[invalid-argument-type] — valid rows have dates
            row.amount_minor,  # ty: ignore[invalid-argument-type]
            row.description_raw or "",
        )
        if row.valid
        else None
        for row in parsed
    ]
    candidates = sorted({fp for fp in fingerprints if fp is not None})
    existing: set[str] = set()
    if candidates:
        matches = await Transaction.where(
            lambda t: (t.account_id == account_id) & (t.fingerprint.in_(candidates))
        ).all()
        existing = {t.fingerprint for t in matches}
    within_file = Counter(fp for fp in fingerprints if fp is not None)
    return [
        (fp, fp is not None and (fp in existing or within_file[fp] >= 2)) for fp in fingerprints
    ]


async def _materialize_preview(
    batch: Import, spec: MappingSpec, parsed: list[ParsedRow], ledger: Ledger
) -> None:
    """Store the confirmed mapping and replace the batch's rows with the
    freshly parsed, duplicate-flagged preview. Runs inside the caller's
    transaction; walks the locked stages uploaded → mapped → previewed."""
    flagged = await _fingerprint_and_flag(
        batch.account_id,  # ty: ignore[unresolved-attribute]
        parsed,
    )
    batch_id = batch.id
    batch.confirmed_mapping = spec.model_dump()
    batch.status = ImportStatus.MAPPED
    await batch.save()
    await ImportRow.where(lambda r: r.import_batch_id == batch_id).delete()
    rows = [
        # Shadow-FK constructor kwargs are runtime-synthesized and
        # invisible to ty (ferro PRD 0004 / ferro-orm#290).
        ImportRow(  # ty: ignore[missing-argument]
            ledger_id=ledger.id,  # ty: ignore[unknown-argument]
            import_batch_id=batch.id,  # ty: ignore[unknown-argument]
            row_index=index,
            raw_cells=row.raw_cells,
            date=row.date,
            amount_minor=row.amount_minor,
            description_raw=row.description_raw,
            valid=row.valid,
            errors=row.errors,
            duplicate=duplicate,
            fingerprint=fingerprint,
        )
        for index, (row, (fingerprint, duplicate)) in enumerate(zip(parsed, flagged, strict=True))
    ]
    if rows:
        await ImportRow.bulk_create(rows)
    batch.status = ImportStatus.PREVIEWED
    await batch.save()


def _first_record(text: str, delimiter: str) -> list[str] | None:
    return next(
        (cells for cells in csv.reader(io.StringIO(text), delimiter=delimiter) if cells), None
    )


async def _matching_profile(
    ledger: Ledger, account: Account, text: str
) -> tuple[ImportProfile, MappingSpec] | None:
    """A saved shape match for this file, or None. Headerless files never
    match: if the first record parses as data under the candidate profile's
    own mapping, it isn't a header and the identity is untrustworthy."""
    delimiter = inference.sniff_delimiter(text[:4096])
    first = _first_record(text, delimiter)
    if first is None:
        return None
    ledger_id = ledger.id
    key = shape_key(normalized_header_tuple(first), delimiter)
    profile = await ImportProfile.where(
        lambda p: (p.ledger_id == ledger_id) & (p.shape_key == key)
    ).first()
    if profile is None:
        return None
    spec = MappingSpec(**profile.mapping)
    if record_parses_as_data(first, spec, exponent=currency_exponent(account.currency)):
        return None
    return profile, spec


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

    # A saved shape maps deterministically with zero inference (story 11):
    # the file lands at previewed — never past it — and the inferrer is
    # never consulted. Everything else goes through the suggestion path.
    matched = await _matching_profile(current_ledger, account, text)
    if matched is not None:
        profile, spec = matched
        parsed = _parse_within_caps(text, spec, account)
        async with transaction():
            batch = await Import.create(
                ledger=current_ledger,
                account=account,
                filename=data.file.filename,
                file_bytes=content,
            )
            await _materialize_preview(batch, spec, parsed, current_ledger)
        log.info(
            "import.created",
            import_id=str(batch.id),
            account_id=str(account.id),
            ledger_id=str(current_ledger.id),
            filename=data.file.filename,
            bytes=len(content),
            profile_id=str(profile.id),
        )
        return await _import_out(batch)

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
    parsed = _parse_within_caps(text, data, account)
    async with transaction():
        await _materialize_preview(batch, data, parsed, current_ledger)
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
    rows = await ImportRow.where(lambda r: r.import_batch_id == batch_id).all()
    by_id = {row.id: row for row in rows}
    for override_id in data.include_duplicates:
        row = by_id.get(override_id)
        if row is None or not row.valid or not row.duplicate:
            raise ClientException(
                detail="include_duplicates must name valid duplicate-flagged rows of this import"
            )
    overridden = set(data.include_duplicates)
    included = sorted(
        (r for r in rows if r.valid and (not r.duplicate or r.id in overridden)),
        key=lambda r: r.row_index,
    )
    async with transaction():
        # Compare-and-set on the previewed → committed edge: of two racing
        # commits, exactly one wins; the loser rolls back and answers the
        # same 409 as the fast-path check above.
        claimed = await Import.where(
            lambda i: (i.id == batch_id) & (i.status == ImportStatus.PREVIEWED)
        ).update(status=ImportStatus.COMMITTED, updated_at=utcnow())
        if claimed == 0:
            raise _conflict("Only a previewed import can be committed")
        # Duplicate flags are point-in-time: if unflagged rows now collide
        # with transactions committed since this preview, committing them
        # would double-book data the user never saw flagged. Refuse loudly;
        # re-confirming the mapping refreshes the flags (I-1).
        account_id = account.id
        clean = sorted(
            {r.fingerprint for r in included if not r.duplicate and r.fingerprint is not None}
        )
        if clean:
            gone_stale = await Transaction.where(
                lambda t: (t.account_id == account_id) & (t.fingerprint.in_(clean))
            ).count()
            if gone_stale:
                raise _conflict(
                    "Duplicates appeared after this preview; confirm the mapping "
                    "again to refresh it"
                )
        transactions = [
            # Shadow-FK kwargs: ferro PRD 0004 / ferro-orm#290. The None
            # ignores are guarded by the valid filter: valid rows carry
            # dates, amounts, and fingerprints by construction.
            Transaction(  # ty: ignore[missing-argument]
                ledger_id=current_ledger.id,  # ty: ignore[unknown-argument]
                account_id=account.id,  # ty: ignore[unknown-argument]
                date=row.date,  # ty: ignore[invalid-argument-type]
                amount_minor=row.amount_minor,  # ty: ignore[invalid-argument-type]
                currency=account.currency,
                description_raw=row.description_raw or "",
                description_normalized=normalize_description(row.description_raw or ""),
                source_import_id=batch.id,  # ty: ignore[unknown-argument]
                fingerprint=row.fingerprint,  # ty: ignore[invalid-argument-type]
            )
            for row in included
        ]
        if transactions:
            await Transaction.bulk_create(transactions)
        batch.status = ImportStatus.COMMITTED  # the CAS wrote the row; sync the instance
        await _save_profile(batch, account, current_ledger)
    log.info(
        "import.committed",
        import_id=str(batch.id),
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
        transactions=len(transactions),
        duplicates_skipped=sum(1 for r in rows if r.valid and r.duplicate) - len(overridden),
        duplicates_overridden=len(overridden),
    )
    return await _import_out(batch)


async def _save_profile(batch: Import, account: Account, ledger: Ledger) -> None:
    """Auto-save the confirmed mapping as this shape's profile (story 11).
    Headered files only — a headerless file has no trustworthy identity.
    The freshest confirmation wins: re-committing a shape with a corrected
    mapping updates its profile. Part of the commit transaction: a profile
    exists iff its commit succeeded."""
    if batch.confirmed_mapping is None:
        raise RuntimeError(f"Import {batch.id} reached commit without a confirmed mapping")
    spec = MappingSpec(**batch.confirmed_mapping)
    if not spec.has_header:
        return
    first = _first_record(batch.file_bytes.decode("utf-8-sig"), spec.delimiter)
    if first is None:
        return
    headers = normalized_header_tuple(first)
    profile, created = await ImportProfile.update_or_create(
        ledger_id=ledger.id,
        shape_key=shape_key(headers, spec.delimiter),
        defaults={
            "header_tuple": headers,
            "delimiter": spec.delimiter,
            "mapping": batch.confirmed_mapping,
        },
    )
    log.info(
        "import.profile.saved",
        profile_id=str(profile.id),
        ledger_id=str(ledger.id),
        created=created,
    )


@delete("/{import_id:uuid}")
async def delete_import(
    import_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """This import never happened (stories 4, 10): transactions, rows, and
    the batch go atomically — unconditionally, dead = gone, even after the
    user has worked with the data. The audit trail is the structured event;
    the learned profile survives. Forward contract (M5, bound here):
    subsystems referencing transactions must tolerate retraction — the
    correction log voids affected decisions with a later entry."""
    batch = await _get_import(current_ledger, import_id)
    undone = batch.status is ImportStatus.COMMITTED
    batch_id = batch.id
    async with transaction():
        await Transaction.where(lambda t: t.source_import_id == batch_id).delete()
        await ImportRow.where(lambda r: r.import_batch_id == batch_id).delete()
        await batch.delete()
    log.info(
        "import.undone" if undone else "import.discarded",
        import_id=str(batch_id),
        ledger_id=str(current_ledger.id),
        status=batch.status.value,
    )


@get("/", name="list_import_profiles")
async def list_import_profiles(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[ImportProfileOut]:
    """Every saved shape for the acting ledger, on the list convention."""
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        ImportProfile.where(lambda p: p.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(
        items=[
            ImportProfileOut(
                id=p.id,
                header_tuple=p.header_tuple,
                delimiter=p.delimiter,
                mapping=MappingSpec(**p.mapping),
                created_at=p.created_at,
            )
            for p in rows
        ],
        next_cursor=next_cursor,
    )


@delete("/{profile_id:uuid}", name="delete_import_profile")
async def delete_import_profile(
    profile_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Forget a shape: the next matching upload goes back through the
    suggestion path. Same 404 for another ledger's profile."""
    ledger_id = current_ledger.id
    profile = await ImportProfile.where(
        lambda p: (p.id == profile_id) & (p.ledger_id == ledger_id)
    ).first()
    if profile is None:
        raise NotFoundException(detail="No such import profile")
    await profile.delete()
    log.info(
        "import.profile.deleted",
        profile_id=str(profile_id),
        ledger_id=str(current_ledger.id),
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

import_profiles_router = Router(
    path="/api/v1/import-profiles",
    route_handlers=[list_import_profiles, delete_import_profile],
)
