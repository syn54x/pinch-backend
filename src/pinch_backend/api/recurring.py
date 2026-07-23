"""/api/v1/recurring — detected recurring series and their curation (PRD
M8 #45, CP3 #49).

Detection writes this resource; users curate it: exactly two PATCHable
fields (kind, display_name) and dismiss. No manual creation — "no manual
setup" is the wireframe's law; curation isn't setup. Cycle state computes
on read against ``as_of`` (the clock seam)."""

import uuid
from datetime import date
from typing import Annotated, Literal

from litestar import Router, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath, QueryParameter
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend import recurring
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import (
    Ledger,
    RecurringCadence,
    RecurringKind,
    RecurringSeries,
    RecurringStatus,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class CycleStateOut(BaseModel):
    status: Literal["paid", "due", "overdue", "upcoming", "lapsed"]
    last_paid_date: date | None
    next_due_date: date | None
    due_in_days: int | None
    fixed: bool
    est_amount_minor: int | None
    monthly_minor: int | None


class RecurringSeriesOut(BaseModel):
    """A series is its matcher plus curation plus the computed cycle."""

    id: uuid.UUID
    account_id: uuid.UUID
    payee: str
    direction: int
    amount_minor: int | None
    cadence: RecurringCadence
    kind: RecurringKind
    status: RecurringStatus
    display_name: str
    bucket: str | None
    state: CycleStateOut


class RecurringPatchIn(BaseModel):
    """Curation only: the matcher and cadence belong to detection, income
    belongs to the sign. Unknown keys are a 400 (extra="forbid")."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    kind: Literal["bill", "subscription"] | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


async def _series_out(
    series: RecurringSeries, as_of: date, category_names: dict[uuid.UUID, str]
) -> RecurringSeriesOut:
    members = await recurring.series_members(series, as_of)
    state = recurring.cycle_state(series, members, as_of)
    return RecurringSeriesOut(
        id=series.id,
        account_id=series.account_id,  # ty: ignore[unresolved-attribute]
        payee=series.payee,
        direction=series.direction,
        amount_minor=series.amount_minor,
        cadence=series.cadence,
        kind=series.kind,
        status=series.status,
        display_name=recurring.default_display_name(series),
        bucket=await recurring.series_bucket(series, members, category_names),
        state=CycleStateOut(
            status=state.status,  # ty: ignore[invalid-argument-type]
            last_paid_date=state.last_paid_date,
            next_due_date=state.next_due_date,
            due_in_days=state.due_in_days,
            fixed=state.fixed,
            est_amount_minor=state.est_amount_minor,
            monthly_minor=state.monthly_minor,
        ),
    )


async def _get_series(ledger: Ledger, series_id: uuid.UUID) -> RecurringSeries:
    series = await RecurringSeries.where(
        lambda s: (s.id == series_id) & (s.ledger_id == ledger.id)
    ).first()
    if series is None:
        raise NotFoundException(detail="No such recurring series")
    return series


@get("/")
async def list_recurring(
    current_ledger: NamedDependency[Ledger],
    kind: Annotated[RecurringKind | None, QueryParameter()] = None,
    unpaid: Annotated[bool | None, QueryParameter()] = None,
    as_of: Annotated[date | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[RecurringSeriesOut]:
    """Active series with computed cycle state; dismissed series never
    appear. ``unpaid`` keeps only due/overdue items (the wireframe's
    Unpaid chip) — applied to the page, which is fine at series scale."""
    ledger_id = current_ledger.id
    as_of = as_of if as_of is not None else utcnow().date()
    query = RecurringSeries.where(
        lambda s: (s.ledger_id == ledger_id) & (s.status == RecurringStatus.ACTIVE)
    )
    if kind is not None:
        query = query.where(lambda s: s.kind == kind)
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    names = await recurring.category_names_for(ledger_id)
    items = [await _series_out(series, as_of, names) for series in rows]
    if unpaid:
        items = [item for item in items if item.state.status in ("due", "overdue")]
    return Page(items=items, next_cursor=next_cursor)


@patch("/{series_id:uuid}")
async def update_recurring(
    series_id: FromPath[uuid.UUID],
    data: RecurringPatchIn,
    current_ledger: NamedDependency[Ledger],
    as_of: Annotated[date | None, QueryParameter()] = None,
) -> RecurringSeriesOut:
    series = await _get_series(current_ledger, series_id)
    if data.kind is not None:
        if series.direction > 0:
            raise ClientException(
                detail="Income is inferred from the sign and cannot be re-segmented"
            )
        series.kind = RecurringKind(data.kind)
    if "display_name" in data.model_fields_set:
        series.display_name = data.display_name
    await series.save()
    log.info(
        "recurring.updated",
        series_id=str(series.id),
        ledger_id=str(current_ledger.id),
        fields=sorted(data.model_fields_set),
    )
    names = await recurring.category_names_for(current_ledger.id)
    return await _series_out(series, as_of if as_of is not None else utcnow().date(), names)


@post("/{series_id:uuid}/dismiss", status_code=HTTP_200_OK)
async def dismiss_recurring(
    series_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
) -> dict[str, str]:
    """The user's verdict, permanent and idempotent: a dismissed series is
    never re-proposed (detection matches it and leaves it alone)."""
    series = await _get_series(current_ledger, series_id)
    if series.status is not RecurringStatus.DISMISSED:
        series.status = RecurringStatus.DISMISSED
        await series.save()
        log.info(
            "recurring.dismissed",
            series_id=str(series.id),
            ledger_id=str(current_ledger.id),
        )
    return {"status": "dismissed"}


recurring_router = Router(
    path="/api/v1/recurring",
    route_handlers=[list_recurring, update_recurring, dismiss_recurring],
)
