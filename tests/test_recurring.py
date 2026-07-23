"""M8 CP3 seam: the recurring engine over the public API (issue #49).

Detection rides classify_ledger's post-classification pass (manual entry
defers the same job as sync — no Plaid needed). A RecurringSeries stores a
matcher, never links: members resolve by query, cycle state computes on
read, lapse is the data's verdict and dismissal is the user's.
"""

from datetime import date, timedelta

RECURRING = "/api/v1/recurring"
SUMMARY = "/api/v1/reports/recurring"

PASSWORD = "correct horse battery staple"

AS_OF = "2026-07-22"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _account(client, kind: str = "depository", label: str = "Checking") -> str:
    response = await client.post(
        "/api/v1/accounts",
        json={"kind": kind, "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _txn(client, account_id: str, date_: str, amount_minor: int, description: str) -> str:
    response = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": account_id,
            "date": date_,
            "amount_minor": amount_minor,
            "description": description,
        },
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _series(client, **params) -> list[dict]:
    response = await client.get(RECURRING, params={"as_of": AS_OF, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _one(items: list[dict], payee: str, amount_minor: int | None = None) -> dict:
    matches = [s for s in items if s["payee"] == payee and s["amount_minor"] == amount_minor]
    assert len(matches) == 1, [(s["payee"], s["amount_minor"]) for s in items]
    return matches[0]


async def test_fixed_monthly_series_detected_across_price_hike(client, run_jobs):
    """Netflix: merged-payee pass 1 holds one series together through a
    price change; the cycle reads paid (charged yesterday)."""
    await _signup(client)
    checking = await _account(client)
    for day, amount in [
        ("2026-04-21", -1_549),
        ("2026-05-21", -1_549),
        ("2026-06-20", -1_549),
        ("2026-07-21", -1_699),
    ]:
        await _txn(client, checking, day, amount, "NETFLIX.COM")
    await run_jobs()

    series = _one(await _series(client), "netflix.com")
    assert series["cadence"] == "monthly"
    assert series["kind"] == "bill"
    assert series["state"]["status"] == "paid"
    assert series["state"]["last_paid_date"] == "2026-07-21"
    assert series["state"]["fixed"] is False  # the hike makes recents differ
    assert series["state"]["est_amount_minor"] == -1_549  # median of recents


async def test_variable_monthly_bill_reads_overdue_with_estimate(client, run_jobs):
    """PG&E: amounts vary, July unpaid at as_of Jul 22 → overdue, est is
    the median."""
    await _signup(client)
    checking = await _account(client)
    for day, amount in [
        ("2026-04-05", -11_822),
        ("2026-05-06", -9_610),
        ("2026-06-05", -13_240),
    ]:
        await _txn(client, checking, day, amount, "PGANDE")
    await run_jobs()

    series = _one(await _series(client), "pgande")
    assert series["cadence"] == "monthly"
    state = series["state"]
    assert state["status"] == "overdue"
    assert state["next_due_date"] == "2026-07-05"
    assert state["due_in_days"] < 0
    assert state["fixed"] is False
    assert state["est_amount_minor"] == -11_822


async def test_biweekly_income_detected_by_weekday(client, run_jobs):
    """Payroll: every second Friday, positive → income, received."""
    await _signup(client)
    checking = await _account(client)
    payday = date(2026, 1, 9)  # a Friday
    while payday <= date(2026, 7, 17):
        await _txn(client, checking, payday.isoformat(), 420_000, "ACME PAYROLL")
        payday += timedelta(days=14)
    await run_jobs()

    series = _one(await _series(client), "acme payroll")
    assert series["cadence"] == "biweekly"
    assert series["kind"] == "income"
    assert series["state"]["status"] == "paid"


async def test_aggregator_payee_splits_by_amount(client, run_jobs):
    """The Apple trio: one payee, three fixed amounts on different days →
    three amount-scoped series with disambiguated display names."""
    await _signup(client)
    checking = await _account(client)
    for month in ["2026-04", "2026-05", "2026-06", "2026-07"]:
        await _txn(client, checking, f"{month}-01", -299, "APPLE.COM/BILL")
        await _txn(client, checking, f"{month}-10", -1_499, "APPLE.COM/BILL")
        await _txn(client, checking, f"{month}-20", -99, "APPLE.COM/BILL")
    await run_jobs()

    items = await _series(client)
    trio = [s for s in items if s["payee"] == "apple.com/bill"]
    assert {s["amount_minor"] for s in trio} == {-299, -1_499, -99}
    assert all(s["cadence"] == "monthly" for s in trio)
    bear = _one(items, "apple.com/bill", -1_499)
    assert bear["display_name"] == "apple.com/bill · 14.99"
    assert bear["state"]["fixed"] is True


async def test_same_amount_interleaved_anchors_stay_silent(client, run_jobs):
    """Two $2.99 subs on the 1st and 15th: the interval fit alone would
    read biweekly; the weekday guard makes it silence, deterministically."""
    await _signup(client)
    checking = await _account(client)
    for month in ["2026-01", "2026-02", "2026-03", "2026-04"]:
        await _txn(client, checking, f"{month}-01", -299, "SHARED VENDOR")
        await _txn(client, checking, f"{month}-15", -299, "SHARED VENDOR")
    await run_jobs()

    assert [s for s in await _series(client) if s["payee"] == "shared vendor"] == []


async def test_irregular_noise_stays_silent(client, run_jobs):
    """Amazon shopping: irregular dates and amounts fit nothing."""
    await _signup(client)
    checking = await _account(client)
    for day, amount in [
        ("2026-06-02", -3_450),
        ("2026-06-05", -1_299),
        ("2026-06-19", -8_710),
        ("2026-07-01", -549),
        ("2026-07-20", -2_100),
    ]:
        await _txn(client, checking, day, amount, "AMAZON.COM")
    await run_jobs()

    assert [s for s in await _series(client) if s["payee"] == "amazon.com"] == []


async def test_loan_payment_is_one_series_bucketed_debt(client, run_jobs):
    """The mortgage: the checking outflow detects (a payment is the most
    recurring thing a ledger has); the loan-side inflow leg does not — a
    payment received is the counterpart, not income."""
    await _signup(client)
    checking = await _account(client)
    loan = await _account(client, kind="loan", label="Mortgage")
    for month in ["2026-04", "2026-05", "2026-06", "2026-07"]:
        out_id = await _txn(client, checking, f"{month}-01", -165_000, "MORTGAGE CO")
        in_id = await _txn(client, loan, f"{month}-01", 165_000, "MORTGAGE CO")
        link = await client.post(
            "/api/v1/transfers",
            json={"transaction_ids": [out_id, in_id]},
            headers=await _csrf(client),
        )
        assert link.status_code == 201, link.text
    await run_jobs()

    items = await _series(client)
    mortgage = [s for s in items if s["payee"] == "mortgage co"]
    assert len(mortgage) == 1
    assert mortgage[0]["account_id"] == checking
    assert mortgage[0]["bucket"] == "Debt"


async def test_lapsed_series_leave_the_cycle_but_not_the_list(client, run_jobs):
    """No member for 2+ cycles → lapsed: out of sums and unpaid lists,
    still visible under all."""
    await _signup(client)
    checking = await _account(client)
    for day in ["2026-01-10", "2026-02-10", "2026-03-10", "2026-04-10"]:
        await _txn(client, checking, day, -1_099, "CANCELLED SUB")
    await run_jobs()

    series = _one(await _series(client), "cancelled sub")
    assert series["state"]["status"] == "lapsed"
    unpaid = await _series(client, unpaid="true")
    assert [s for s in unpaid if s["payee"] == "cancelled sub"] == []

    summary = await client.get(SUMMARY, params={"as_of": AS_OF})
    assert summary.status_code == 200
    assert summary.json()["monthly_recurring_minor"] == 0


async def test_dismissed_series_never_return(client, run_jobs):
    await _signup(client)
    checking = await _account(client)
    for month in ["2026-04", "2026-05", "2026-06"]:
        await _txn(client, checking, f"{month}-03", -1_500, "GYM")
    await run_jobs()
    series = _one(await _series(client), "gym")

    dismissed = await client.post(
        f"{RECURRING}/{series['id']}/dismiss", headers=await _csrf(client)
    )
    assert dismissed.status_code == 200
    assert [s for s in await _series(client) if s["payee"] == "gym"] == []

    # New matching data re-runs detection; the dismissal stands.
    await _txn(client, checking, "2026-07-03", -1_500, "GYM")
    await run_jobs()
    assert [s for s in await _series(client) if s["payee"] == "gym"] == []


async def test_resweep_never_duplicates(client, run_jobs):
    await _signup(client)
    checking = await _account(client)
    for month in ["2026-04", "2026-05", "2026-06"]:
        await _txn(client, checking, f"{month}-12", -999, "SPOTIFY")
    await run_jobs()
    await _txn(client, checking, "2026-07-12", -999, "SPOTIFY")
    await run_jobs()

    assert len([s for s in await _series(client) if s["payee"] == "spotify"]) == 1


async def test_curation_is_kind_and_display_name_only(client, run_jobs):
    await _signup(client)
    checking = await _account(client)
    for month in ["2026-04", "2026-05", "2026-06", "2026-07"]:
        await _txn(client, checking, f"{month}-10", -1_499, "APPLE.COM/BILL")
        await _txn(client, checking, f"{month}-09", 420_000, "ACME PAYROLL")
    await run_jobs()
    items = await _series(client)
    sub = _one(items, "apple.com/bill")
    income = _one(items, "acme payroll")

    patched = await client.patch(
        f"{RECURRING}/{sub['id']}",
        json={"kind": "subscription", "display_name": "Bear"},
        headers=await _csrf(client),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["kind"] == "subscription"
    assert patched.json()["display_name"] == "Bear"

    matcher_write = await client.patch(
        f"{RECURRING}/{sub['id']}",
        json={"payee": "netflix.com"},
        headers=await _csrf(client),
    )
    assert matcher_write.status_code == 400

    income_flip = await client.patch(
        f"{RECURRING}/{income['id']}",
        json={"kind": "bill"},
        headers=await _csrf(client),
    )
    assert income_flip.status_code == 400


async def test_summary_cards_and_donut(client, run_jobs):
    """Monthly normalization, next-7-days look-ahead, subscriptions card,
    and the by-bucket donut with Debt."""
    await _signup(client)
    checking = await _account(client)
    loan = await _account(client, kind="loan", label="Mortgage")
    # Rent: monthly on the 28th → due within the next 7 days from Jul 22.
    for month in ["2026-04", "2026-05", "2026-06"]:
        await _txn(client, checking, f"{month}-28", -165_000, "RENT LLC")
    # Spotify: monthly, paid Jul 12.
    for month in ["2026-04", "2026-05", "2026-06", "2026-07"]:
        await _txn(client, checking, f"{month}-12", -999, "SPOTIFY")
    # Mortgage transfer: monthly on the 1st.
    for month in ["2026-04", "2026-05", "2026-06", "2026-07"]:
        out_id = await _txn(client, checking, f"{month}-01", -80_000, "MORTGAGE CO")
        in_id = await _txn(client, loan, f"{month}-01", 80_000, "MORTGAGE CO")
        await client.post(
            "/api/v1/transfers",
            json={"transaction_ids": [out_id, in_id]},
            headers=await _csrf(client),
        )
    # Payroll income: must not count into monthly recurring outflow.
    payday = date(2026, 1, 9)
    while payday <= date(2026, 7, 17):
        await _txn(client, checking, payday.isoformat(), 420_000, "ACME PAYROLL")
        payday += timedelta(days=14)
    await run_jobs()

    items = await _series(client)
    spotify = _one(items, "spotify")
    await client.patch(
        f"{RECURRING}/{spotify['id']}",
        json={"kind": "subscription"},
        headers=await _csrf(client),
    )

    response = await client.get(SUMMARY, params={"as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["monthly_recurring_minor"] == 165_000 + 999 + 80_000
    assert body["due_next_7_days_minor"] == 165_000
    assert body["due_next_7_days"] == [
        {"display_name": "rent llc", "due_date": "2026-07-28", "amount_minor": -165_000}
    ]
    assert body["subscriptions"] == {"monthly_minor": 999, "count": 1}
    buckets = {b["bucket"]: b["monthly_minor"] for b in body["by_bucket"]}
    assert buckets["Debt"] == 80_000
    # Cycle: rent unpaid (due Jul 28 cycle counts), spotify paid, mortgage
    # paid, payroll received → 3 of 4.
    assert body["cycle"] == {"paid": 3, "total": 4}


async def test_requires_authentication(client):
    response = await client.get(RECURRING)
    assert response.status_code == 401
