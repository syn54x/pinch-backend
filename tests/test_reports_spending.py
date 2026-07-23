"""M8 CP2 seam: the spending report over the public API (issue #48).

"Spending" defined once (PRD #45): outflows on lines — split transactions
contribute their lines, unsplit ones their own category — transfers
excluded, uncategorized as its own bucket, hierarchy rollup derived at
read time, income never counted. Period-over-period rides the same call.
"""

TRANSACTIONS = "/api/v1/transactions"
SPENDING = "/api/v1/reports/spending"

PASSWORD = "correct horse battery staple"


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


async def _account(
    client, kind: str = "depository", label: str = "Checking", currency: str = "USD"
) -> str:
    response = await client.post(
        "/api/v1/accounts",
        json={"kind": kind, "label": label, "currency": currency},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _category(client, name: str, parent_id: str | None = None) -> str:
    response = await client.post(
        "/api/v1/categories",
        json={"name": name, "parent_id": parent_id},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _txn(
    client,
    account_id: str,
    date: str,
    amount_minor: int,
    description: str,
    category_id: str | None = None,
) -> str:
    response = await client.post(
        TRANSACTIONS,
        json={
            "account_id": account_id,
            "date": date,
            "amount_minor": amount_minor,
            "description": description,
            **({"category_id": category_id} if category_id else {}),
        },
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _report(client, month: str = "2026-07"):
    response = await client.get(SPENDING, params={"month": month})
    assert response.status_code == 200, response.text
    return response.json()


def _row(body, category_id):
    matches = [r for r in body["by_category"] if r["category_id"] == category_id]
    assert len(matches) == 1, body["by_category"]
    return matches[0]


async def test_month_total_and_daily_trend(client):
    """Outflow magnitudes, sign-scoped: income never counts as spending."""
    await _signup(client)
    account = await _account(client)
    groceries = await _category(client, "Groceries")
    await _txn(client, account, "2026-07-01", -1_000, "CAFE", groceries)
    await _txn(client, account, "2026-07-01", -2_000, "MARKET", groceries)
    await _txn(client, account, "2026-07-15", -3_000, "MARKET", groceries)
    await _txn(client, account, "2026-07-03", 500_000, "PAYROLL")

    body = await _report(client)
    assert body["month"] == "2026-07"
    assert body["currency"] == "USD"
    assert body["total_minor"] == 6_000
    assert body["by_day"] == [
        {"date": "2026-07-01", "total_minor": 3_000},
        {"date": "2026-07-15", "total_minor": 3_000},
    ]


async def test_splits_report_through_lines_never_double(client):
    """One-layer law: the split parent's own (vacated) category never
    counts; its lines do, each under its own category."""
    await _signup(client)
    account = await _account(client)
    groceries = await _category(client, "Groceries")
    tires = await _category(client, "Auto")
    txn = await _txn(client, account, "2026-07-05", -10_000, "COSTCO")
    response = await client.put(
        f"{TRANSACTIONS}/{txn}/splits",
        json=[
            {"amount_minor": -7_000, "category_id": groceries},
            {"amount_minor": -3_000, "category_id": tires},
        ],
        headers=await _csrf(client),
    )
    assert response.status_code == 200, response.text

    body = await _report(client)
    assert body["total_minor"] == 10_000
    assert _row(body, groceries)["direct_minor"] == 7_000
    assert _row(body, tires)["direct_minor"] == 3_000
    # No uncategorized leak from the vacated parent.
    assert not any(r["category_id"] is None for r in body["by_category"])


async def test_transfers_are_not_spending(client):
    """Paying the credit card is never $2,400 of spending (CONTEXT.md law)."""
    await _signup(client)
    checking = await _account(client)
    card = await _account(client, kind="credit", label="Card")
    out_leg = await _txn(client, checking, "2026-07-10", -240_000, "CARD PAYMENT")
    in_leg = await _txn(client, card, "2026-07-10", 240_000, "PAYMENT RECEIVED")
    groceries = await _category(client, "Groceries")
    await _txn(client, checking, "2026-07-11", -5_000, "MARKET", groceries)
    response = await client.post(
        "/api/v1/transfers",
        json={"transaction_ids": [out_leg, in_leg]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text

    body = await _report(client)
    assert body["total_minor"] == 5_000
    assert body["by_day"] == [{"date": "2026-07-11", "total_minor": 5_000}]


async def test_uncategorized_is_its_own_bucket(client):
    await _signup(client)
    account = await _account(client)
    await _txn(client, account, "2026-07-02", -4_000, "MYSTERY")

    body = await _report(client)
    uncategorized = [r for r in body["by_category"] if r["category_id"] is None]
    assert len(uncategorized) == 1
    assert uncategorized[0]["direct_minor"] == 4_000
    assert uncategorized[0]["name"] is None


async def test_hierarchy_rolls_up_without_double_counting(client):
    """A Restaurants transaction counts toward Food by ancestry — derived
    at read time, never stored (CONTEXT.md)."""
    await _signup(client)
    account = await _account(client)
    food = await _category(client, "Food")
    restaurants = await _category(client, "Restaurants", parent_id=food)
    await _txn(client, account, "2026-07-04", -2_000, "CAFE", restaurants)
    await _txn(client, account, "2026-07-06", -1_000, "MARKET", food)

    body = await _report(client)
    assert body["total_minor"] == 3_000
    food_row = _row(body, food)
    assert food_row["direct_minor"] == 1_000
    assert food_row["rolled_up_minor"] == 3_000
    child_row = _row(body, restaurants)
    assert child_row["direct_minor"] == 2_000
    assert child_row["rolled_up_minor"] == 2_000


async def test_period_over_period_percent(client):
    """Dining: June $612 → July $438, down 28% — computed server-side,
    percent null when the previous month is zero."""
    await _signup(client)
    account = await _account(client)
    dining = await _category(client, "Dining")
    fresh = await _category(client, "Brand New")
    await _txn(client, account, "2026-06-10", -61_200, "OMAKASE", dining)
    await _txn(client, account, "2026-07-10", -43_800, "BISTRO", dining)
    await _txn(client, account, "2026-07-12", -9_900, "POPUP", fresh)

    body = await _report(client)
    assert body["previous"]["month"] == "2026-06"
    assert body["previous"]["total_minor"] == 61_200
    dining_row = _row(body, dining)
    assert dining_row["previous_minor"] == 61_200
    assert abs(dining_row["percent_change"] - (-28.43)) < 0.01
    fresh_row = _row(body, fresh)
    assert fresh_row["previous_minor"] == 0
    assert fresh_row["percent_change"] is None
    assert body["change"]["delta_minor"] == 53_700 - 61_200
    assert body["change"]["percent"] is not None


async def test_foreign_currency_outflows_excluded(client):
    """No rate exists: EUR spending surfaces in the excluded remainder,
    never in the converted totals."""
    await _signup(client)
    usd = await _account(client)
    eur = await _account(client, label="Berlin", currency="EUR")
    await _txn(client, usd, "2026-07-08", -2_000, "MARKET")
    await _txn(client, eur, "2026-07-08", -7_500, "BAKEREI")

    body = await _report(client)
    assert body["total_minor"] == 2_000
    assert body["excluded"] == [{"currency": "EUR", "total_minor": 7_500}]


async def test_month_validation_and_auth(client):
    response = await client.get(SPENDING)
    assert response.status_code == 401

    await _signup(client)
    response = await client.get(SPENDING, params={"month": "July-2026"})
    assert response.status_code == 400
