"""Review & retraction integration for splits and transfers (M6 CP3, #28).

The richest inbox decisions — split it, or mark it a transfer — are one
gesture: the review body carries them, consume records them honestly
(decision_splits / decision_transfer, names-not-FKs), and retraction
handles both models. Split x transfer exclusivity answers 409 both ways.
"""

TX = "/api/v1/transactions"
TRANSFERS = "/api/v1/transfers"
LOG = "/api/v1/correction-log"
PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text


async def _account(client, label: str = "Checking") -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _category(client, name: str) -> str:
    r = await client.post("/api/v1/categories", json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _commit_csv(client, account_id: str, rows: list[tuple[str, str, str]]) -> str:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{m}" for d, a, m in rows) + "\n"
    up = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    iid = up.json()["id"]
    await client.post(
        f"/api/v1/imports/{iid}/mapping",
        json=up.json()["suggested_mapping"],
        headers=await _csrf(client),
    )
    commit = await client.post(
        f"/api/v1/imports/{iid}/commit", json={}, headers=await _csrf(client)
    )
    assert commit.status_code == 200, commit.text
    return iid


async def _inbox(client) -> list[dict]:
    items = (await client.get(TX, params={"reviewed": "false"})).json()["items"]
    assert items, "expected unreviewed transactions"
    return items


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(f"{TX}/{txn_id}/review", json=body or {}, headers=await _csrf(client))


async def _txn(client, account_id: str, amount_minor: int, description: str, **extra) -> dict:
    r = await client.post(
        TX,
        json={
            "account_id": account_id,
            "date": "2026-07-10",
            "amount_minor": amount_minor,
            "description": description,
        }
        | extra,
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _log_entries(client, **params) -> list[dict]:
    return (await client.get(LOG, params=params)).json()["items"]


# ------------------------------------------------------- review with splits


async def test_review_with_splits_consumes_and_logs_decision_splits(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    await _commit_csv(client, acct, [("2026-07-01", "-70.00", "COSTCO WHOLESALE")])
    await run_jobs()
    (txn,) = await _inbox(client)
    assert txn["proposal"] is not None

    r = await _review(
        client,
        txn["id"],
        {
            "splits": [
                {"amount_minor": -3000, "category_id": groceries},
                {"amount_minor": -4000, "memo": "tires"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["transaction"]
    assert body["reviewed_at"] is not None
    assert body["category"] is None  # split => one layer holds categories
    assert len(body["splits"]) == 2
    assert body["proposal"] is None  # consumed

    (entry,) = await _log_entries(client, kind="decision")
    assert entry["decision_category_id"] is None  # no fake category shrug
    assert entry["decision_transfer"] is None
    assert entry["decision_splits"] == [
        {
            "amount_minor": -3000,
            "category_id": groceries,
            "category_name": "Groceries",
            "memo": None,
        },
        {"amount_minor": -4000, "category_id": None, "category_name": None, "memo": "tires"},
    ]


async def test_review_splits_document_rejections_apply(client) -> None:
    await _signup(client)
    acct = await _account(client)
    txn = await _txn(client, acct, -7000, "COSTCO WHOLESALE")
    r = await _review(client, txn["id"], {"splits": [{"amount_minor": -7000}]})
    assert r.status_code == 400  # same validates-all-first document rules as PUT
    detail = (await client.get(f"{TX}/{txn['id']}")).json()
    assert detail["reviewed_at"] is None  # nothing consumed
    assert detail["splits"] is None


# ----------------------------------------------------- review with transfer


async def test_review_with_untracked_transfer(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    await _commit_csv(client, acct, [("2026-07-01", "-99.00", "VENMO CASHOUT")])
    await run_jobs()
    (txn,) = await _inbox(client)

    r = await _review(client, txn["id"], {"transfer": {"untracked": True}})
    assert r.status_code == 200, r.text
    body = r.json()["transaction"]
    assert body["reviewed_at"] is not None
    assert body["category"] is None
    assert body["transfer"]["kind"] == "untracked"
    assert body["proposal"] is None

    (entry,) = await _log_entries(client, kind="decision")
    assert entry["decision_splits"] is None
    assert entry["decision_category_id"] is None
    assert entry["decision_transfer"] == {
        "kind": "untracked",
        "counterpart_transaction_id": None,
        "counterpart_account_id": None,
    }


async def test_counterpart_review_consumes_both_sides_atomically(client, run_jobs) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    await _commit_csv(client, checking, [("2026-07-01", "-250.00", "TRANSFER TO SAVINGS")])
    await _commit_csv(client, savings, [("2026-07-02", "250.00", "TRANSFER FROM CHECKING")])
    await run_jobs()
    inbox = await _inbox(client)
    assert len(inbox) == 2 and all(t["proposal"] is not None for t in inbox)
    outflow = next(t for t in inbox if t["amount_minor"] < 0)
    inflow = next(t for t in inbox if t["amount_minor"] > 0)

    r = await _review(client, outflow["id"], {"transfer": {"counterpart": inflow["id"]}})
    assert r.status_code == 200, r.text

    for side, other, other_acct in (
        (outflow, inflow, savings),
        (inflow, outflow, checking),
    ):
        detail = (await client.get(f"{TX}/{side['id']}")).json()
        assert detail["reviewed_at"] is not None  # both sides reviewed
        assert detail["proposal"] is None  # both proposals consumed
        assert detail["category"] is None
        assert detail["transfer"]["kind"] == "linked"
        assert detail["transfer"]["counterpart_transaction_id"] == other["id"]
        assert detail["transfer"]["counterpart_account_id"] == other_acct

    entries = await _log_entries(client, kind="decision")
    assert len(entries) == 2  # one per side, never accept-by-filter
    by_txn = {e["transaction_id"]: e for e in entries}
    assert by_txn[outflow["id"]]["decision_transfer"] == {
        "kind": "linked",
        "counterpart_transaction_id": inflow["id"],
        "counterpart_account_id": savings,
    }
    assert by_txn[inflow["id"]]["decision_transfer"] == {
        "kind": "linked",
        "counterpart_transaction_id": outflow["id"],
        "counterpart_account_id": checking,
    }


async def test_reviewed_counterpart_answers_409_and_rolls_everything_back(client, run_jobs) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    await _commit_csv(client, checking, [("2026-07-01", "-250.00", "TRANSFER TO SAVINGS")])
    await _commit_csv(client, savings, [("2026-07-02", "250.00", "TRANSFER FROM CHECKING")])
    await run_jobs()
    inbox = await _inbox(client)
    outflow = next(t for t in inbox if t["amount_minor"] < 0)
    inflow = next(t for t in inbox if t["amount_minor"] > 0)
    assert (await _review(client, inflow["id"])).status_code == 200  # counterpart decided first

    r = await _review(client, outflow["id"], {"transfer": {"counterpart": inflow["id"]}})
    assert r.status_code == 409, r.text
    # Atomicity: the rejected motion left nothing behind.
    detail = (await client.get(f"{TX}/{outflow['id']}")).json()
    assert detail["reviewed_at"] is None
    assert detail["transfer"] is None
    assert (await client.get(TRANSFERS)).json()["items"] == []
    assert len(await _log_entries(client, kind="decision")) == 1  # only the first review


# ----------------------------------------------------------- exclusivity


async def test_decision_shapes_are_mutually_exclusive_422(client) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    txn = await _txn(client, acct, -7000, "COSTCO WHOLESALE")
    lines = [{"amount_minor": -3000}, {"amount_minor": -4000}]

    bodies = [
        {"category_id": groceries, "splits": lines},
        {"category_id": groceries, "transfer": {"untracked": True}},
        {"splits": lines, "transfer": {"untracked": True}},
        {"transfer": {}},  # neither form
        {"transfer": {"untracked": False}},  # no form either
        {"transfer": {"untracked": True, "counterpart": txn["id"]}},  # both forms
    ]
    for body in bodies:
        r = await _review(client, txn["id"], body)
        assert r.status_code == 422, (body, r.text)
    assert (await client.get(f"{TX}/{txn['id']}")).json()["reviewed_at"] is None


async def test_split_x_transfer_409_both_directions(client) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    lines = [{"amount_minor": -3000}, {"amount_minor": -4000}]

    # Direction one: a transferred transaction refuses splits (PUT and review).
    transferred = await _txn(client, checking, -7000, "TRANSFER OUT")
    assert (
        await client.post(
            TRANSFERS, json={"transaction_ids": [transferred["id"]]}, headers=await _csrf(client)
        )
    ).status_code == 201
    r = await client.put(
        f"{TX}/{transferred['id']}/splits", json=lines, headers=await _csrf(client)
    )
    assert r.status_code == 409, r.text
    r2 = await _review(client, transferred["id"], {"splits": lines})
    assert r2.status_code == 409, r2.text

    # Direction two: a split transaction refuses transfers (POST and review).
    split = await _txn(client, checking, -7000, "COSTCO WHOLESALE")
    assert (
        await client.put(f"{TX}/{split['id']}/splits", json=lines, headers=await _csrf(client))
    ).status_code == 200
    r3 = await client.post(
        TRANSFERS, json={"transaction_ids": [split["id"]]}, headers=await _csrf(client)
    )
    assert r3.status_code == 409, r3.text
    counterpart = await _txn(client, savings, 7000, "MATCHING IN")
    r4 = await client.post(
        TRANSFERS,
        json={"transaction_ids": [split["id"], counterpart["id"]]},
        headers=await _csrf(client),
    )
    assert r4.status_code == 409, r4.text
    r5 = await _review(client, split["id"], {"transfer": {"untracked": True}})
    assert r5.status_code == 409, r5.text


# ------------------------------------------------------------- batch & log


async def test_batch_accepts_split_and_transferred_as_is_without_category(client, run_jobs) -> None:
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    # Two arrivals; a category rule guarantees both proposals carry a category.
    await client.post(
        "/api/v1/rules",
        json={
            "condition": {"payee": {"op": "contains", "value": "COSTCO"}},
            "action_category_id": groceries,
        },
        headers=await _csrf(client),
    )
    await _commit_csv(
        client,
        acct,
        [("2026-07-01", "-70.00", "COSTCO WHOLESALE"), ("2026-07-02", "-50.00", "COSTCO GAS")],
    )
    await run_jobs()
    inbox = await _inbox(client)
    assert all(t["proposal"]["category"] is not None for t in inbox)
    split_txn = next(t for t in inbox if t["amount_minor"] == -7000)
    transfer_txn = next(t for t in inbox if t["amount_minor"] == -5000)

    # Pre-review edits: split one, mark the other an untracked transfer.
    assert (
        await client.put(
            f"{TX}/{split_txn['id']}/splits",
            json=[{"amount_minor": -3000}, {"amount_minor": -4000}],
            headers=await _csrf(client),
        )
    ).status_code == 200
    assert (
        await client.post(
            TRANSFERS, json={"transaction_ids": [transfer_txn["id"]]}, headers=await _csrf(client)
        )
    ).status_code == 201

    r = await client.post(
        TX + "/review",
        json={"ids": [split_txn["id"], transfer_txn["id"]]},
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 2

    for txn_id in (split_txn["id"], transfer_txn["id"]):
        detail = (await client.get(f"{TX}/{txn_id}")).json()
        assert detail["reviewed_at"] is not None
        assert detail["category"] is None  # the proposal's category was NOT applied

    by_txn = {e["transaction_id"]: e for e in await _log_entries(client, kind="decision")}
    assert by_txn[split_txn["id"]]["decision_splits"] is not None
    assert by_txn[split_txn["id"]]["decision_category_id"] is None
    assert by_txn[transfer_txn["id"]]["decision_transfer"]["kind"] == "untracked"
    assert by_txn[transfer_txn["id"]]["decision_category_id"] is None


async def test_split_and_transfer_filings_are_deviations_for_category_promotion(
    client, run_jobs
) -> None:
    """A payee with mixed treatment never mints a category rule — the
    split/transfer entries carry no decision_category, so they read as
    deviations with zero new promotion code."""
    await _signup(client)
    acct = await _account(client)
    groceries = await _category(client, "Groceries")
    for day, amount in (("01", "-70.00"), ("02", "-71.00"), ("03", "-72.00"), ("04", "-73.00")):
        await _commit_csv(client, acct, [(f"2026-07-{day}", amount, "COSTCO WHOLESALE")])
    await run_jobs()
    inbox = await _inbox(client)
    assert len(inbox) == 4
    first, second, third, fourth = sorted(inbox, key=lambda t: t["date"])

    assert (await _review(client, first["id"], {"category_id": groceries})).status_code == 200
    assert (await _review(client, second["id"], {"category_id": groceries})).status_code == 200
    # The deviation: filing #3 is a split, not the category.
    r = await _review(
        client, third["id"], {"splits": [{"amount_minor": -3600}, {"amount_minor": -3600}]}
    )
    assert r.status_code == 200, r.text
    # Filing #4 back to the category — three consistent category filings
    # exist now, but the split vote kills all-time consistency.
    final = await _review(client, fourth["id"], {"category_id": groceries})
    assert final.status_code == 200, final.text
    assert final.json()["proposed_rule"] is None
    proposed = (await client.get("/api/v1/rules", params={"status": "proposed"})).json()["items"]
    assert proposed == []


# ---------------------------------------------------------------- undo


async def test_undo_dissolves_reopens_survivor_and_voids_its_entry(client, run_jobs) -> None:
    await _signup(client)
    checking = await _account(client, "Checking")
    savings = await _account(client, "Savings")
    # The doomed side arrives by import; the survivor is a manual entry.
    iid = await _commit_csv(client, checking, [("2026-07-01", "-250.00", "TRANSFER TO SAVINGS")])
    survivor = await _txn(client, savings, 25000, "TRANSFER FROM CHECKING")
    await run_jobs()
    doomed = next(t for t in (await client.get(TX)).json()["items"] if t["amount_minor"] < 0)

    link = await client.post(
        TRANSFERS,
        json={"transaction_ids": [doomed["id"], survivor["id"]]},
        headers=await _csrf(client),
    )
    assert link.status_code == 201, link.text
    # The survivor's decision: reviewed as a transfer member.
    assert (await _review(client, survivor["id"])).status_code == 200
    (entry,) = await _log_entries(client, transaction_id=survivor["id"], kind="decision")
    assert entry["decision_transfer"]["kind"] == "linked"

    undo = await client.delete(f"/api/v1/imports/{iid}", headers=await _csrf(client))
    assert undo.status_code == 204, undo.text

    assert (await client.get(TRANSFERS)).json()["items"] == []  # dissolved
    detail = (await client.get(f"{TX}/{survivor['id']}")).json()
    assert detail["transfer"] is None
    assert detail["reviewed_at"] is None  # reopened — silent report pollution refused
    voids = await _log_entries(client, transaction_id=survivor["id"], kind="void")
    assert len(voids) == 1 and voids[0]["voids"] == entry["id"]  # voided, never deleted
