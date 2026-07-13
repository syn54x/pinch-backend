"""M4 CP2 seam: the import lifecycle over the public API (issue #15).

Upload → suggested mapping → confirm/correct → paginated preview with
validation errors → synchronous atomic commit → transactions exist.
Nothing touches the ledger until commit; a mid-batch failure leaves zero
rows; an uncommitted import is discardable. Duplicates, undo of committed
batches, and profiles are CP3 (issue #16).
"""

import uuid

from litestar.status_codes import HTTP_500_INTERNAL_SERVER_ERROR

from pinch_backend.models import Import, ImportRow, ImportStatus, Transaction
from pinch_backend.settings import settings

ACCOUNTS = "/api/v1/accounts"
IMPORTS = "/api/v1/imports"
SCHEMA_JSON = "/api/v1/schema/openapi.json"

PASSWORD = "correct horse battery staple"

IMPORT_FIELDS = {
    "id",
    "account_id",
    "filename",
    "status",
    "suggested_mapping",
    "confirmed_mapping",
    "row_count",
    "valid_row_count",
    "error_row_count",
    "transaction_count",
    "created_at",
}
ROW_FIELDS = {
    "id",
    "row_index",
    "raw_cells",
    "date",
    "amount_minor",
    "currency",
    "description_raw",
    "errors",
}

HEADERED_CSV = (
    "Date,Description,Amount\n2026-01-05,COFFEE SHOP,-4.50\n2026-01-06,PAYCHECK,2000.00\n"
)
MIXED_VALIDITY_CSV = (
    "Date,Description,Amount\n"
    "2026-01-05,GOOD ROW,-4.50\n"
    "not-a-date,BAD DATE,-1.00\n"
    "2026-01-07,BAD AMOUNT,abc\n"
    "2026-01-08,SUBCENT,0.005\n"
)
DEBIT_CREDIT_CSV = "Date,Payee,Debit,Credit\n01/05/2026,STORE,4.50,\n01/06/2026,EMPLOYER,,2000.00\n"


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


async def _logout(client) -> None:
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))


async def _mint_pat(client, scopes: list[str]) -> str:
    response = await client.post(
        "/api/v1/auth/pats",
        json={"name": f"{'-'.join(scopes)}-pat", "scopes": scopes},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_account(client, *, currency: str = "USD") -> str:
    response = await client.post(
        ACCOUNTS,
        json={"kind": "depository", "label": "Checking", "currency": currency},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(client, account_id: str, csv_text: str, *, headers=None, filename="bank.csv"):
    return await client.post(
        IMPORTS,
        files={"file": (filename, csv_text.encode(), "text/csv")},
        data={"account_id": account_id},
        headers=headers if headers is not None else await _csrf(client),
    )


async def _uploaded(client, account_id: str, csv_text: str, **kwargs) -> dict:
    response = await _upload(client, account_id, csv_text, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def _confirm(client, import_id: str, mapping: dict, *, headers=None):
    return await client.post(
        f"{IMPORTS}/{import_id}/mapping",
        json=mapping,
        headers=headers if headers is not None else await _csrf(client),
    )


async def _previewed(client, account_id: str, csv_text: str) -> dict:
    """Upload and confirm the suggested mapping: the happy path to a preview."""
    imported = await _uploaded(client, account_id, csv_text)
    response = await _confirm(client, imported["id"], imported["suggested_mapping"])
    assert response.status_code == 200, response.text
    return response.json()


async def _rows(client, import_id: str, **params) -> dict:
    response = await client.get(f"{IMPORTS}/{import_id}/rows", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- Upload: an import that touches nothing (story 4) -------------------------------


async def test_upload_creates_an_uploaded_import_with_a_suggested_mapping(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    imported = await _uploaded(client, account_id, HEADERED_CSV)

    assert set(imported) == IMPORT_FIELDS
    assert imported["account_id"] == account_id
    assert imported["filename"] == "bank.csv"
    assert imported["status"] == "uploaded"
    assert imported["confirmed_mapping"] is None
    assert imported["transaction_count"] is None

    suggestion = imported["suggested_mapping"]
    assert suggestion["delimiter"] == ","
    assert suggestion["has_header"] is True
    assert suggestion["date_column"] == 0
    assert suggestion["date_format"] == "%Y-%m-%d"
    assert suggestion["amount_column"] == 2
    assert suggestion["description_columns"] == [1]

    # Nothing touches the ledger — no rows, no transactions (story 4).
    assert await Transaction.select().count() == 0
    assert await ImportRow.select().count() == 0


async def test_upload_suggests_a_debit_credit_pair_mapping(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    imported = await _uploaded(client, account_id, DEBIT_CREDIT_CSV)

    suggestion = imported["suggested_mapping"]
    assert suggestion["date_format"] == "%m/%d/%Y"
    assert suggestion["amount_column"] is None
    assert suggestion["debit_column"] == 2
    assert suggestion["credit_column"] == 3
    assert suggestion["description_columns"] == [1]


async def test_upload_requires_a_manual_account_in_the_acting_ledger(client) -> None:
    from pinch_backend.models import Account, Connection, Ledger

    await _signup(client)
    await _create_account(client)

    # Another ledger's account: 404, never a confirming 403.
    response = await _upload(client, str(uuid.uuid7()), HEADERED_CSV)
    assert response.status_code == 404

    # A connected account in the acting ledger: imports are manual-only (400).
    ledger = (await Ledger.all())[0]
    connection = await Connection.create(ledger=ledger, provider_item_id="item-1")
    connected = await Account.create(
        ledger=ledger, kind="depository", label="Plaid", currency="USD", connection=connection
    )
    response = await _upload(client, str(connected.id), HEADERED_CSV)
    assert response.status_code == 400


async def test_upload_enforces_the_byte_cap_in_the_envelope(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "import_max_bytes", 10)
    await _signup(client)
    account_id = await _create_account(client)
    response = await _upload(client, account_id, HEADERED_CSV)
    assert response.status_code == 400
    body = response.json()
    assert body["status_code"] == 400
    assert await Import.select().count() == 0


async def test_upload_enforces_the_row_cap_in_the_envelope(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "import_max_rows", 1)
    await _signup(client)
    account_id = await _create_account(client)
    response = await _upload(client, account_id, HEADERED_CSV)  # 2 data rows
    assert response.status_code == 400
    assert await Import.select().count() == 0


async def test_upload_rejects_files_that_are_not_utf8_text(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    response = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", b"\xff\xfe garbage \x00", "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert response.status_code == 400


async def test_get_import_answers_the_allowlist_and_unknown_ids_404(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    imported = await _uploaded(client, account_id, HEADERED_CSV)

    fetched = (await client.get(f"{IMPORTS}/{imported['id']}")).json()
    assert fetched == imported
    assert (await client.get(f"{IMPORTS}/{uuid.uuid7()}")).status_code == 404


# --- Mapping confirm: review, not data entry (stories 5, 6, 7) ------------------------


async def test_confirming_the_suggested_mapping_parses_rows_and_previews(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)

    assert previewed["status"] == "previewed"
    assert previewed["row_count"] == 2
    assert previewed["valid_row_count"] == 2
    assert previewed["error_row_count"] == 0

    rows = (await _rows(client, previewed["id"]))["items"]
    assert [set(r) for r in rows] == [ROW_FIELDS, ROW_FIELDS]
    coffee, paycheck = rows
    assert coffee["row_index"] == 0
    assert coffee["date"] == "2026-01-05"
    assert coffee["amount_minor"] == -450  # exact minor units, signed
    assert coffee["currency"] == "USD"
    assert coffee["description_raw"] == "COFFEE SHOP"
    assert coffee["errors"] == []
    assert paycheck["amount_minor"] == 2000_00


async def test_invalid_rows_carry_errors_and_amounts_are_never_rounded(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, MIXED_VALIDITY_CSV)

    assert previewed["row_count"] == 4
    assert previewed["valid_row_count"] == 1
    assert previewed["error_row_count"] == 3

    rows = (await _rows(client, previewed["id"]))["items"]
    good, bad_date, bad_amount, subcent = rows
    assert good["errors"] == []
    assert bad_date["errors"] and bad_date["date"] is None
    assert bad_amount["errors"] and bad_amount["amount_minor"] is None
    # 0.005 USD cannot resolve exactly to minor units: invalid, never 1 cent.
    assert subcent["errors"] and subcent["amount_minor"] is None


async def test_correcting_the_mapping_reparses_and_replaces_rows(client) -> None:
    """The suggestion is a review, not a verdict (story 5): a correction
    re-parses, and rows are replaced — never appended."""
    await _signup(client)
    account_id = await _create_account(client)
    # All-positive single amount column: a card export where charges are
    # positive. The suggestion can't see that; the user corrects the sign.
    csv_text = "Date,Description,Amount\n2026-01-05,CHARGE,4.50\n"
    imported = await _uploaded(client, account_id, csv_text)

    first = await _confirm(client, imported["id"], imported["suggested_mapping"])
    assert first.json()["row_count"] == 1
    assert (await _rows(client, imported["id"]))["items"][0]["amount_minor"] == 450

    corrected = imported["suggested_mapping"] | {"sign": "positive_out"}
    second = await _confirm(client, imported["id"], corrected)
    assert second.status_code == 200
    rows = (await _rows(client, imported["id"]))["items"]
    assert len(rows) == 1  # replaced, not appended
    assert rows[0]["amount_minor"] == -450  # money out, from the account's view


async def test_a_debit_credit_pair_signs_amounts_from_the_accounts_perspective(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, DEBIT_CREDIT_CSV)

    store, employer = (await _rows(client, previewed["id"]))["items"]
    assert store["amount_minor"] == -450  # debit = money out
    assert store["date"] == "2026-01-05"
    assert employer["amount_minor"] == 2000_00  # credit = money in


async def test_zero_exponent_currencies_resolve_whole_units(client) -> None:
    await _signup(client)
    account_id = await _create_account(client, currency="JPY")
    csv_text = "Date,Description,Amount\n2026-01-05,RAMEN,-1200\n2026-01-06,SUSHI,-12.34\n"
    previewed = await _previewed(client, account_id, csv_text)

    ramen, sushi = (await _rows(client, previewed["id"]))["items"]
    assert ramen["amount_minor"] == -1200
    # JPY has no minor units: a fractional yen is invalid, never rounded.
    assert sushi["errors"] and sushi["amount_minor"] is None


async def test_mapping_validation_rejects_incoherent_specs(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    imported = await _uploaded(client, account_id, HEADERED_CSV)
    suggestion = imported["suggested_mapping"]

    both = suggestion | {"debit_column": 1, "credit_column": 2}
    neither = suggestion | {"amount_column": None}
    for bad in (both, neither):
        response = await _confirm(client, imported["id"], bad)
        assert response.status_code == 400, bad


async def test_mapping_a_committed_import_is_a_409(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)
    committed = await client.post(
        f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=await _csrf(client)
    )
    assert committed.status_code == 200, committed.text

    response = await _confirm(client, previewed["id"], previewed["confirmed_mapping"])
    assert response.status_code == 409


# --- Row preview pagination (story 6 / issue #9) ---------------------------------------


async def test_rows_paginate_on_the_m3_convention_in_file_order(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    csv_text = "Date,Description,Amount\n" + "".join(
        f"2026-01-{day:02d},ROW {day},-1.00\n" for day in range(1, 6)
    )
    previewed = await _previewed(client, account_id, csv_text)

    seen: list[int] = []
    cursor = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = await _rows(client, previewed["id"], **params)
        seen += [row["row_index"] for row in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [0, 1, 2, 3, 4]  # file order, every row exactly once

    bad = await client.get(f"{IMPORTS}/{previewed['id']}/rows", params={"cursor": "junk"})
    assert bad.status_code == 400


# --- Commit: one atomic batch (story 9) -------------------------------------------------


async def test_commit_creates_transactions_for_valid_rows_only(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, MIXED_VALIDITY_CSV)

    response = await client.post(
        f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=await _csrf(client)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "committed"
    assert body["transaction_count"] == 1

    (txn,) = await Transaction.all()
    assert str(txn.account_id) == account_id  # ty: ignore[unresolved-attribute]
    assert txn.amount_minor == -450
    assert txn.currency == "USD"
    assert txn.description_raw == "GOOD ROW"
    assert txn.date.isoformat() == "2026-01-05"
    assert str(txn.source_import_id) == previewed["id"]  # ty: ignore[unresolved-attribute]
    assert txn.fingerprint  # stored at commit; the recipe is CP3's contract


async def test_commit_requires_a_preview_and_never_runs_twice(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    headers = await _csrf(client)

    uploaded = await _uploaded(client, account_id, HEADERED_CSV)
    premature = await client.post(f"{IMPORTS}/{uploaded['id']}/commit", json={}, headers=headers)
    assert premature.status_code == 409

    previewed = await _previewed(client, account_id, HEADERED_CSV)
    first = await client.post(f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=headers)
    assert first.status_code == 200
    again = await client.post(f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=headers)
    assert again.status_code == 409
    assert await Transaction.select().count() == 2  # committed exactly once


async def test_a_mid_batch_failure_leaves_zero_transactions(client, monkeypatch) -> None:
    """The atomicity contract (story 9), asserted on both backends via the
    test matrix: a commit that dies half-way leaves the ledger untouched
    and the import still previewed — retryable, never partial."""
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)

    original = Transaction.bulk_create.__func__

    async def die_mid_batch(cls, instances, **kwargs):
        await original(cls, instances[: len(instances) // 2], **kwargs)
        raise RuntimeError("simulated mid-batch death")

    monkeypatch.setattr(Transaction, "bulk_create", classmethod(die_mid_batch))
    response = await client.post(
        f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=await _csrf(client)
    )
    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR

    assert await Transaction.select().count() == 0  # zero rows, not half
    refetched = (await client.get(f"{IMPORTS}/{previewed['id']}")).json()
    assert refetched["status"] == "previewed"
    assert refetched["transaction_count"] is None


# --- DELETE an uncommitted import: dead = gone (story 4) ---------------------------------


async def test_delete_discards_an_uncommitted_import_entirely(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)

    response = await client.delete(f"{IMPORTS}/{previewed['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    assert await Import.select().count() == 0
    assert await ImportRow.select().count() == 0
    assert (await client.get(f"{IMPORTS}/{previewed['id']}")).status_code == 404


async def test_delete_of_a_committed_import_is_reserved_for_undo(client) -> None:
    """CP2 fences committed imports behind 409; CP3 (#16) makes DELETE the
    unconditional undo. This test is rewritten there."""
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)
    headers = await _csrf(client)
    await client.post(f"{IMPORTS}/{previewed['id']}/commit", json={}, headers=headers)

    response = await client.delete(f"{IMPORTS}/{previewed['id']}", headers=headers)
    assert response.status_code == 409
    assert await Transaction.select().count() == 2


# --- Tenancy (AGENTS I-2) -----------------------------------------------------------------


async def test_another_users_imports_answer_404_never_403(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, HEADERED_CSV)
    await _logout(client)

    await _signup(client, email="other@example.com")
    headers = await _csrf(client)
    i_id = previewed["id"]
    assert (await client.get(f"{IMPORTS}/{i_id}")).status_code == 404
    assert (await client.get(f"{IMPORTS}/{i_id}/rows")).status_code == 404
    assert (
        await _confirm(client, i_id, previewed["confirmed_mapping"], headers=headers)
    ).status_code == 404
    assert (
        await client.post(f"{IMPORTS}/{i_id}/commit", json={}, headers=headers)
    ).status_code == 404
    assert (await client.delete(f"{IMPORTS}/{i_id}", headers=headers)).status_code == 404
    # And the victim's import is exactly as they left it.
    assert await Import.select().count() == 1
    assert await ImportRow.select().count() == 2


# --- Credentials: a write PAT drives the whole lifecycle (story 12) -------------------------


async def test_a_write_pat_drives_the_lifecycle_and_a_read_pat_cannot(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    write_token = await _mint_pat(client, ["write"])
    read_token = await _mint_pat(client, ["read"])
    client.cookies.clear()

    write, read = _bearer(write_token), _bearer(read_token)
    imported = await _uploaded(client, account_id, HEADERED_CSV, headers=write)
    assert (
        await _confirm(client, imported["id"], imported["suggested_mapping"], headers=write)
    ).status_code == 200
    # The read PAT sees everything and changes nothing.
    assert (await client.get(f"{IMPORTS}/{imported['id']}", headers=read)).status_code == 200
    assert (await client.get(f"{IMPORTS}/{imported['id']}/rows", headers=read)).status_code == 200
    assert (await _upload(client, account_id, HEADERED_CSV, headers=read)).status_code == 403
    assert (
        await client.post(f"{IMPORTS}/{imported['id']}/commit", json={}, headers=read)
    ).status_code == 403
    assert (await client.delete(f"{IMPORTS}/{imported['id']}", headers=read)).status_code == 403

    committed = await client.post(f"{IMPORTS}/{imported['id']}/commit", json={}, headers=write)
    assert committed.status_code == 200
    assert committed.json()["transaction_count"] == 2


# --- OpenAPI (story 12) -----------------------------------------------------------------------


async def test_openapi_describes_the_import_routes(client) -> None:
    schema = (await client.get(SCHEMA_JSON)).json()
    for path in (
        "/api/v1/imports",
        "/api/v1/imports/{import_id}",
        "/api/v1/imports/{import_id}/mapping",
        "/api/v1/imports/{import_id}/rows",
        "/api/v1/imports/{import_id}/commit",
    ):
        assert path in schema["paths"], f"{path} missing from the OpenAPI document"


# --- The lifecycle vocabulary is the locked one ------------------------------------------------


def test_import_status_carries_the_four_locked_stages() -> None:
    assert [s.value for s in ImportStatus] == ["uploaded", "mapped", "previewed", "committed"]
