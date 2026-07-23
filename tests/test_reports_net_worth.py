"""M8 CP1 seam: the net-worth report over the public API (issue #47).

Compute-on-read: forward-filled balance history, kind-split totals, deltas,
the OLS run-rate projection, and the FX excluded remainder — all pinned
through GET /api/v1/reports/net-worth with an explicit ``as_of`` (the clock
seam; no time freezing).
"""

from datetime import date

NET_WORTH = "/api/v1/reports/net-worth"
ACCOUNTS = "/api/v1/accounts"

PASSWORD = "correct horse battery staple"

TODAY = date(2026, 7, 22)


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


async def _account(client, kind: str, label: str, currency: str = "USD") -> str:
    response = await client.post(
        ACCOUNTS,
        json={"kind": kind, "label": label, "currency": currency},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _balance(client, account_id: str, amount_minor: int, as_of: str) -> None:
    response = await client.post(
        f"{ACCOUNTS}/{account_id}/balance-entries",
        json={"amount_minor": amount_minor, "as_of": f"{as_of}T12:00:00Z"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _report(client, **params):
    response = await client.get(NET_WORTH, params={"as_of": TODAY.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()


async def test_totals_split_by_kind(client):
    """Assets = depository/investment/asset kinds; liabilities = credit/loan;
    net worth = the sum of both (CONTEXT.md: negative balances on debt)."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Auto Loan")
    house = await _account(client, "asset", "Home")
    await _balance(client, checking, 100_000, "2026-07-20")
    await _balance(client, loan, -40_000, "2026-07-20")
    await _balance(client, house, 500_000, "2026-07-20")

    body = await _report(client)
    assert body["currency"] == "USD"
    assert body["assets_minor"] == 600_000
    assert body["liabilities_minor"] == -40_000
    assert body["net_worth_minor"] == 560_000


async def test_forward_fill_carries_sparse_history(client):
    """An account observed once, months ago, still contributes its last-known
    balance to every later bucket and to the as_of totals."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    await _balance(client, checking, 123_400, "2026-04-01")

    body = await _report(client, range="6m")
    assert body["net_worth_minor"] == 123_400
    series = body["series"]
    assert series[-1]["date"] == TODAY.isoformat()
    assert series[-1]["net_worth_minor"] == 123_400
    # Buckets before the observation carry nothing-yet (0 by summation),
    # buckets after carry the value forward.
    before = [p for p in series if p["date"] < "2026-04-01"]
    after = [p for p in series if p["date"] >= "2026-04-01"]
    assert before and all(p["net_worth_minor"] == 0 for p in before)
    assert after and all(p["net_worth_minor"] == 123_400 for p in after)


async def test_as_of_replays_the_past(client):
    """The clock seam: an as_of before an entry excludes it."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    await _balance(client, checking, 50_000, "2026-06-01")
    await _balance(client, checking, 80_000, "2026-07-10")

    body = await _report(client)
    assert body["net_worth_minor"] == 80_000
    past = await (lambda: _report(client, as_of="2026-06-15"))()
    assert past["net_worth_minor"] == 50_000
    assert past["as_of"] == "2026-06-15"


async def test_archived_accounts_are_invisible(client):
    """The binding #33 hook: archived accounts appear in no total, series,
    or account list."""
    await _signup(client)
    keep = await _account(client, "depository", "Checking")
    dead = await _account(client, "depository", "Old Checking")
    await _balance(client, keep, 10_000, "2026-07-01")
    await _balance(client, dead, 99_000, "2026-07-01")
    archived = await client.post(f"{ACCOUNTS}/{dead}/archive", headers=await _csrf(client))
    assert archived.status_code == 200

    body = await _report(client)
    assert body["net_worth_minor"] == 10_000
    assert [a["id"] for a in body["accounts"]] == [keep]
    assert all(p["net_worth_minor"] <= 10_000 for p in body["series"])


async def test_foreign_currency_lands_in_excluded_remainder(client):
    """No FX rate exists in v0: a EUR account is excluded from converted
    totals and surfaced explicitly — honest exclusion, never a fake rate."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    berlin = await _account(client, "depository", "Berlin", currency="EUR")
    await _balance(client, checking, 10_000, "2026-07-01")
    await _balance(client, berlin, 120_000, "2026-07-01")

    body = await _report(client)
    assert body["net_worth_minor"] == 10_000
    assert body["excluded"] == [{"currency": "EUR", "balance_minor": 120_000}]
    assert [a["id"] for a in body["accounts"]] == [checking]


async def test_granularity_by_range(client):
    """1M daily, 6M weekly, 1Y weekly, All monthly — self-described."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    await _balance(client, checking, 10_000, "2026-01-01")

    ranges = [("1m", "daily"), ("6m", "weekly"), ("1y", "weekly"), ("all", "monthly")]
    for rng, granularity in ranges:
        body = await _report(client, range=rng)
        assert body["range"] == rng
        assert body["granularity"] == granularity, rng
        assert body["series"][-1]["date"] == TODAY.isoformat()

    response = await client.get(NET_WORTH, params={"range": "2w", "as_of": TODAY.isoformat()})
    assert response.status_code == 400


async def test_projection_extends_the_run_rate(client):
    """OLS over the range's series, horizon = range length, dashed-line data
    returned as its own series + endpoint."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    # A clean +1000/week run-rate across the 6m window.
    await _balance(client, checking, 0, "2026-01-22")
    for week in range(1, 27):
        d = date.fromordinal(date(2026, 1, 22).toordinal() + 7 * week)
        await _balance(client, checking, week * 1_000, d.isoformat())

    body = await _report(client, range="6m")
    projection = body["projection"]
    assert projection is not None
    proj_series = projection["series"]
    assert proj_series[0]["date"] > TODAY.isoformat()
    assert projection["endpoint"] == proj_series[-1]
    # Horizon mirrors the range: the endpoint lands ~6 months out.
    assert proj_series[-1]["date"] >= "2027-01-01"
    # The run-rate continues upward at roughly +1000/week.
    assert proj_series[-1]["net_worth_minor"] > body["net_worth_minor"] + 20_000


async def test_projection_null_without_enough_history(client):
    """A single observation has no slope; the projection is null, never a
    fabricated flat line."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    await _balance(client, checking, 10_000, TODAY.isoformat())

    body = await _report(client)
    assert body["projection"] is None


async def test_deltas_and_zero_reference_percent(client):
    """Month-to-date vs calendar month start; range delta vs range start;
    percent is null when the reference value is zero — never infinity."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    await _balance(client, checking, 100_000, "2026-06-20")
    await _balance(client, checking, 110_000, "2026-07-10")

    body = await _report(client, range="6m")
    # Month start (Jul 1) forward-fills the June entry: 100_000.
    assert body["month_to_date"]["delta_minor"] == 10_000
    assert abs(body["month_to_date"]["percent"] - 10.0) < 0.01
    # Range start (late January) predates all history: reference is 0.
    assert body["since_range_start"]["delta_minor"] == 110_000
    assert body["since_range_start"]["percent"] is None


async def test_per_account_series_power_the_sparklines(client):
    """Each included account carries its own forward-filled series."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Auto Loan")
    await _balance(client, checking, 30_000, "2026-05-01")
    await _balance(client, loan, -5_000, "2026-05-01")

    body = await _report(client, range="6m")
    accounts = {a["id"]: a for a in body["accounts"]}
    assert set(accounts) == {checking, loan}
    assert accounts[loan]["kind"] == "loan"
    assert accounts[loan]["balance_minor"] == -5_000
    assert accounts[loan]["series"][-1]["balance_minor"] == -5_000
    assert len(accounts[loan]["series"]) == len(body["series"])


async def test_requires_authentication(client):
    response = await client.get(NET_WORTH)
    assert response.status_code == 401
