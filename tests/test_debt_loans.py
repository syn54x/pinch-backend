"""M8 CP4 seam: loan terms, observed pace, the payoff simulator, and the
debt summary over the public API (issue #50).

Terms are nullable fields on Account (no LoanTerms model — PRD #45); pace
is the median of the trailing 6 calendar months' payments into the loan
(transfers whose inflow side is the loan — the M7 hook); the simulator is
plain amortization run pace-vs-minimum, with "never pays off" a legitimate
answer and APR-missing a degradation, never an error.
"""

ACCOUNTS = "/api/v1/accounts"
TRANSACTIONS = "/api/v1/transactions"
DEBT = "/api/v1/reports/debt"

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


async def _account(client, kind: str, label: str) -> str:
    response = await client.post(
        ACCOUNTS,
        json={"kind": kind, "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _patch(client, account_id: str, body: dict):
    return await client.patch(f"{ACCOUNTS}/{account_id}", json=body, headers=await _csrf(client))


async def _balance(client, account_id: str, amount_minor: int, as_of: str = AS_OF) -> None:
    response = await client.post(
        f"{ACCOUNTS}/{account_id}/balance-entries",
        json={"amount_minor": amount_minor, "as_of": f"{as_of}T12:00:00Z"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _payment(client, checking: str, loan: str, date: str, amount_minor: int) -> None:
    """One loan payment: a transfer whose inflow side is the loan."""
    out_response = await client.post(
        TRANSACTIONS,
        json={
            "account_id": checking,
            "date": date,
            "amount_minor": -amount_minor,
            "description": f"LOAN PAYMENT {date}",
        },
        headers=await _csrf(client),
    )
    assert out_response.status_code == 201, out_response.text
    in_response = await client.post(
        TRANSACTIONS,
        json={
            "account_id": loan,
            "date": date,
            "amount_minor": amount_minor,
            "description": f"PAYMENT RECEIVED {date}",
        },
        headers=await _csrf(client),
    )
    assert in_response.status_code == 201, in_response.text
    link = await client.post(
        "/api/v1/transfers",
        json={"transaction_ids": [out_response.json()["id"], in_response.json()["id"]]},
        headers=await _csrf(client),
    )
    assert link.status_code == 201, link.text


async def _payoff(client, account_id: str, **params):
    response = await client.get(
        f"{ACCOUNTS}/{account_id}/payoff", params={"as_of": AS_OF, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_terms_patch_and_kind_guard(client):
    """All five on a loan; apr/minimum only on credit; nothing on
    depository — a term on the wrong kind is a 400, never silence."""
    await _signup(client)
    loan = await _account(client, "loan", "Auto Loan")
    card = await _account(client, "credit", "Card")
    checking = await _account(client, "depository", "Checking")

    response = await _patch(
        client,
        loan,
        {
            "apr": 4.9,
            "minimum_payment_minor": 52_000,
            "origination_date": "2023-01-15",
            "origination_amount_minor": -6_500_000,
            "maturity_date": "2029-01-15",
        },
    )
    assert response.status_code == 200, response.text
    terms = response.json()["terms"]
    assert terms == {
        "apr": 4.9,
        "minimum_payment_minor": 52_000,
        "origination_date": "2023-01-15",
        "origination_amount_minor": -6_500_000,
        "maturity_date": "2029-01-15",
    }

    ok = await _patch(client, card, {"apr": 21.9, "minimum_payment_minor": 4_000})
    assert ok.status_code == 200
    bad_credit = await _patch(client, card, {"origination_date": "2024-01-01"})
    assert bad_credit.status_code == 400
    bad_kind = await _patch(client, checking, {"apr": 1.0})
    assert bad_kind.status_code == 400

    # Present-and-null clears; label PATCH still works alongside.
    cleared = await _patch(client, loan, {"maturity_date": None, "label": "The Car"})
    assert cleared.status_code == 200
    assert cleared.json()["terms"]["maturity_date"] is None
    assert cleared.json()["label"] == "The Car"


async def test_pace_is_median_of_trailing_six_months(client):
    """Months without a payment count as zero; the median absorbs both the
    skipped month and the double-payment outlier."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Auto Loan")
    await _balance(client, loan, -4_000_000)
    # Trailing-6 window before as_of month (Jul): Jan..Jun.
    await _payment(client, checking, loan, "2026-01-10", 50_000)
    await _payment(client, checking, loan, "2026-02-10", 50_000)
    await _payment(client, checking, loan, "2026-03-10", 120_000)  # outlier
    # April skipped: counts as zero.
    await _payment(client, checking, loan, "2026-05-10", 50_000)
    await _payment(client, checking, loan, "2026-06-10", 60_000)
    # Out-of-window noise: an old payment and a current-month one.
    await _payment(client, checking, loan, "2025-11-10", 999_000)
    await _payment(client, checking, loan, "2026-07-05", 999_000)

    body = await _payoff(client, loan)
    # Monthly totals Jan..Jun: [50000, 50000, 120000, 0, 50000, 60000]
    # → sorted [0, 50000, 50000, 50000, 60000, 120000] → median 50000.
    assert body["pace_payment_minor"] == 50_000


async def test_payoff_simulation_pace_vs_minimum(client):
    """Closed-form check: $100.00 at 12% APR, $50.00/mo pays off in 3
    months with $1.53 interest."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Loan")
    await _balance(client, loan, -10_000)
    await _patch(client, loan, {"apr": 12.0, "minimum_payment_minor": 2_000})
    for month in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        await _payment(client, checking, loan, f"{month}-10", 5_000)

    body = await _payoff(client, loan)
    assert body["pace_payment_minor"] == 5_000
    at_pace = body["projections"]["at_pace"]
    assert at_pace["never_pays_off"] is False
    assert at_pace["months"] == 3
    assert at_pace["payoff_date"] == "2026-10-22"
    assert at_pace["total_interest_minor"] == 153
    at_minimum = body["projections"]["at_minimum"]
    assert at_minimum["never_pays_off"] is False
    assert at_minimum["months"] > at_pace["months"]
    headline = body["projections"]["headline"]
    assert headline["months_earlier"] == at_minimum["months"] - at_pace["months"]
    assert (
        headline["interest_saved_minor"]
        == at_minimum["total_interest_minor"] - at_pace["total_interest_minor"]
    )
    # The chart curves: monthly remaining balances, account-signed, ending at 0.
    assert at_pace["series"][-1]["balance_minor"] == 0
    assert len(at_pace["series"]) == at_pace["months"]


async def test_never_pays_off_is_a_legitimate_answer(client):
    """21.9% APR at $18/mo against $1,000: interest outruns the payment."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    card = await _account(client, "credit", "Card")
    await _balance(client, card, -100_000)
    await _patch(client, card, {"apr": 21.9, "minimum_payment_minor": 1_500})
    for month in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        await _payment(client, checking, card, f"{month}-10", 1_800)

    body = await _payoff(client, card)
    at_pace = body["projections"]["at_pace"]
    assert at_pace["never_pays_off"] is True
    assert at_pace["payoff_date"] is None
    assert body["projections"]["at_minimum"]["never_pays_off"] is True
    assert body["projections"]["headline"] is None


async def test_missing_apr_degrades_never_errors(client):
    """Balance and pace still serve; the projection waits for a rate."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Mystery Loan")
    await _balance(client, loan, -500_000)
    for month in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        await _payment(client, checking, loan, f"{month}-10", 30_000)

    body = await _payoff(client, loan)
    assert body["balance_minor"] == -500_000
    assert body["pace_payment_minor"] == 30_000
    assert body["projections"] is None


async def test_extra_monthly_scenario(client):
    """The what-if widget: same pure function, stateless."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    loan = await _account(client, "loan", "Loan")
    await _balance(client, loan, -1_000_000)
    await _patch(client, loan, {"apr": 6.0})
    for month in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        await _payment(client, checking, loan, f"{month}-10", 50_000)

    base = await _payoff(client, loan)
    scenario = await _payoff(client, loan, extra_monthly=20_000)
    assert base["scenario"] is None
    assert scenario["scenario"]["extra_monthly_minor"] == 20_000
    assert scenario["scenario"]["months_sooner"] > 0
    assert scenario["scenario"]["interest_saved_minor"] > 0


async def test_payoff_kind_guard(client):
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    response = await client.get(f"{ACCOUNTS}/{checking}/payoff", params={"as_of": AS_OF})
    assert response.status_code == 400


async def test_debt_report_partial_honesty(client):
    """Total and count are always exact; minimums, weighted APR, and
    debt-free-by each name how many loans they had to exclude."""
    await _signup(client)
    checking = await _account(client, "depository", "Checking")
    auto = await _account(client, "loan", "Auto Loan")
    mystery = await _account(client, "loan", "Mystery Loan")
    await _balance(client, auto, -4_000_000)
    await _balance(client, mystery, -1_000_000)
    await _patch(
        client,
        auto,
        {
            "apr": 4.9,
            "minimum_payment_minor": 52_000,
            "origination_amount_minor": -6_000_000,
        },
    )
    for month in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        await _payment(client, checking, auto, f"{month}-10", 68_000)

    response = await client.get(DEBT, params={"as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_debt_minor"] == -5_000_000
    assert body["loan_count"] == 2
    assert body["monthly_minimums_minor"] == 52_000
    assert body["minimums_excluded_count"] == 1
    assert body["weighted_apr"] == 4.9
    assert body["apr_excluded_count"] == 1
    assert body["debt_free_by"] is not None
    assert body["debt_free_excluded_count"] == 1

    rows = {r["id"]: r for r in body["loans"]}
    assert set(rows) == {auto, mystery}
    auto_row = rows[auto]
    assert auto_row["apr"] == 4.9
    assert auto_row["pace_payment_minor"] == 68_000
    # Payoff ring: (6.0M - 4.0M) / 6.0M of the original principal is paid.
    assert abs(auto_row["payoff_percent"] - 33.33) < 0.01
    assert auto_row["payoff_date"] is not None
    mystery_row = rows[mystery]
    assert mystery_row["apr"] is None
    assert mystery_row["payoff_percent"] is None
    assert mystery_row["payoff_date"] is None


async def test_debt_requires_authentication(client):
    response = await client.get(DEBT)
    assert response.status_code == 401
