"""M4 CP3 seam: duplicate flags, unconditional undo, import profiles
(issue #16) — what makes imports safe to repeat.

The fingerprint recipe is a contract (one versioned function); duplicates
flag against existing transactions AND within the file, skip by default,
override per row; DELETE of a committed import means "this import never
happened"; profiles make the second file from the same bank a
zero-inference event — never skipping the preview, never auto-committing.
"""

import uuid
from datetime import date

from pinch_backend.imports import inference
from pinch_backend.imports.fingerprint import fingerprint_v1, normalize_description
from pinch_backend.models import Import, ImportProfile, ImportRow, Transaction

ACCOUNTS = "/api/v1/accounts"
IMPORTS = "/api/v1/imports"
PROFILES = "/api/v1/import-profiles"
SCHEMA_JSON = "/api/v1/schema/openapi.json"

PASSWORD = "correct horse battery staple"

BANK_CSV = "Date,Description,Amount\n2026-01-05,COFFEE SHOP,-4.50\n2026-01-06,PAYCHECK,2000.00\n"
OVERLAPPING_CSV = (
    "Date,Description,Amount\n2026-01-05,COFFEE SHOP,-4.50\n2026-01-07,NEW THING,-9.99\n"
)
TWO_COFFEES_CSV = (
    "Date,Description,Amount\n"
    "2026-01-05,BLUE BOTTLE COFFEE,-4.50\n"
    "2026-01-05,BLUE BOTTLE COFFEE,-4.50\n"
)
HEADERLESS_CSV = "2026-01-05,COFFEE SHOP,-4.50\n2026-01-06,PAYCHECK,2000.00\n"


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


async def _create_account(client) -> str:
    response = await client.post(
        ACCOUNTS,
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(client, account_id: str, csv_text: str) -> dict:
    response = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", csv_text.encode(), "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _previewed(client, account_id: str, csv_text: str) -> dict:
    """Upload, then confirm mapping if a profile didn't already preview it."""
    imported = await _upload(client, account_id, csv_text)
    if imported["status"] == "previewed":
        return imported
    response = await client.post(
        f"{IMPORTS}/{imported['id']}/mapping",
        json=imported["suggested_mapping"],
        headers=await _csrf(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _commit(client, import_id: str, body: dict | None = None) -> dict:
    response = await client.post(
        f"{IMPORTS}/{import_id}/commit", json=body or {}, headers=await _csrf(client)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _rows(client, import_id: str) -> list[dict]:
    response = await client.get(f"{IMPORTS}/{import_id}/rows")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _committed(client, account_id: str, csv_text: str, body: dict | None = None) -> dict:
    previewed = await _previewed(client, account_id, csv_text)
    return await _commit(client, previewed["id"], body)


# --- The fingerprint recipe is a contract (story 8) ---------------------------------


def test_normalization_is_nfkc_casefold_collapse_trim_and_nothing_cleverer() -> None:
    # NFKC folds the fullwidth letters and the ideographic space; casefold
    # lowers; runs collapse; edges trim.
    assert normalize_description(" ＢＬＵＥ  Bottle　　Coffee ") == "blue bottle coffee"  # noqa: RUF001
    # Digits and punctuation are kept: check numbers stay distinct.
    assert normalize_description("CHECK #1234") != normalize_description("CHECK #1235")


def test_fingerprint_changes_with_every_ingredient_and_only_those() -> None:
    account = uuid.uuid7()
    base = fingerprint_v1(account, date(2026, 1, 5), -450, "COFFEE SHOP")
    assert base == fingerprint_v1(account, date(2026, 1, 5), -450, "coffee  shop ")
    assert base != fingerprint_v1(uuid.uuid7(), date(2026, 1, 5), -450, "COFFEE SHOP")
    assert base != fingerprint_v1(account, date(2026, 1, 6), -450, "COFFEE SHOP")
    assert base != fingerprint_v1(account, date(2026, 1, 5), -451, "COFFEE SHOP")
    assert base != fingerprint_v1(account, date(2026, 1, 5), -450, "COFFEE SHOP #2")


# --- Duplicate flags: cross-file and within-file (story 8) ---------------------------


async def test_a_reimported_file_flags_every_row_and_skips_by_default(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)

    # Profiles make the second upload land previewed; the flags must be there.
    second = await _previewed(client, account_id, BANK_CSV)
    rows = await _rows(client, second["id"])
    assert [row["duplicate"] for row in rows] == [True, True]

    committed = await _commit(client, second["id"])
    assert committed["transaction_count"] == 0  # skip by default, never silent
    assert await Transaction.select().count() == 2  # only the originals


async def test_an_overlapping_file_flags_only_the_shared_row(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)

    second = await _previewed(client, account_id, OVERLAPPING_CSV)
    coffee, new_thing = await _rows(client, second["id"])
    assert coffee["duplicate"] is True
    assert new_thing["duplicate"] is False

    committed = await _commit(client, second["id"])
    assert committed["transaction_count"] == 1
    assert await Transaction.select().count() == 3


async def test_two_identical_coffees_are_both_flagged_and_both_commit_on_override(client) -> None:
    """The two-identical-coffees scenario by name (PRD M4): distinct
    real-world transactions can collide, so the per-row override is the
    escape hatch — and skipping is a default, never silent."""
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, TWO_COFFEES_CSV)

    first, second = await _rows(client, previewed["id"])
    assert first["duplicate"] is True  # both flagged, not just the second
    assert second["duplicate"] is True

    committed = await _commit(
        client, previewed["id"], {"include_duplicates": [first["id"], second["id"]]}
    )
    assert committed["transaction_count"] == 2  # both coffees count
    assert await Transaction.select().count() == 2


async def test_the_override_is_per_row_not_per_import(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, TWO_COFFEES_CSV)
    first, _second = await _rows(client, previewed["id"])

    committed = await _commit(client, previewed["id"], {"include_duplicates": [first["id"]]})
    assert committed["transaction_count"] == 1


async def test_overriding_a_row_that_is_not_an_overridable_duplicate_is_400(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    previewed = await _previewed(client, account_id, BANK_CSV)
    rows = await _rows(client, previewed["id"])
    headers = await _csrf(client)

    for bogus in (str(uuid.uuid7()), rows[0]["id"]):  # unknown; not a duplicate
        response = await client.post(
            f"{IMPORTS}/{previewed['id']}/commit",
            json={"include_duplicates": [bogus]},
            headers=headers,
        )
        assert response.status_code == 400, bogus


# --- Undo: this import never happened (story 10) --------------------------------------


async def test_undo_removes_import_rows_and_transactions_atomically(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    committed = await _committed(client, account_id, BANK_CSV)
    assert await Transaction.select().count() == 2

    response = await client.delete(f"{IMPORTS}/{committed['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    assert await Transaction.select().count() == 0
    assert await ImportRow.select().count() == 0
    assert await Import.select().count() == 0
    assert (await client.get(f"{IMPORTS}/{committed['id']}")).status_code == 404


async def test_reimporting_after_undo_raises_zero_duplicate_flags(client) -> None:
    """Dead = gone is observable (PRD M4): after undo, the fingerprints
    match nothing, and the same file commits cleanly."""
    await _signup(client)
    account_id = await _create_account(client)
    committed = await _committed(client, account_id, BANK_CSV)
    await client.delete(f"{IMPORTS}/{committed['id']}", headers=await _csrf(client))

    again = await _previewed(client, account_id, BANK_CSV)
    assert [row["duplicate"] for row in await _rows(client, again["id"])] == [False, False]
    assert (await _commit(client, again["id"]))["transaction_count"] == 2


async def test_undo_survives_the_user_having_worked_with_the_data(client) -> None:
    """Undo is unconditional (story 10) — even later, even with other
    imports since; only this import's transactions go."""
    await _signup(client)
    account_id = await _create_account(client)
    first = await _committed(client, account_id, BANK_CSV)
    await _committed(client, account_id, OVERLAPPING_CSV, {"include_duplicates": []})
    other_txns = await Transaction.select().count()
    assert other_txns == 3  # 2 + the non-duplicate NEW THING row

    response = await client.delete(f"{IMPORTS}/{first['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    remaining = await Transaction.all()
    assert [t.description_raw for t in remaining] == ["NEW THING"]


# --- Profiles: the second file from the same bank is a zero-inference event (story 11) --


async def test_a_successful_commit_saves_a_ledger_scoped_profile(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    committed = await _committed(client, account_id, BANK_CSV)

    listed = (await client.get(PROFILES)).json()
    (profile,) = listed["items"]
    assert set(profile) == {"id", "header_tuple", "delimiter", "mapping", "created_at"}
    assert profile["header_tuple"] == ["date", "description", "amount"]  # normalized
    assert profile["delimiter"] == ","
    assert profile["mapping"] == committed["confirmed_mapping"]


async def test_a_matching_upload_lands_previewed_with_the_inferrer_never_invoked(
    client, monkeypatch
) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)

    class MustNotRun:
        async def suggest(self, text: str):
            raise AssertionError("a profile matched; the inferrer must not be consulted")

    monkeypatch.setattr(inference, "active_inferrer", MustNotRun())
    second = await _upload(client, account_id, OVERLAPPING_CSV)  # same shape, new data
    # Straight to previewed — but never past it: no auto-commit (story 11).
    assert second["status"] == "previewed"
    assert second["confirmed_mapping"] is not None
    assert second["transaction_count"] is None
    assert second["row_count"] == 2


async def test_the_profile_remembers_the_users_correction(client) -> None:
    """The profile stores what the user confirmed, not what was suggested:
    a corrected sign convention carries to next month's file."""
    await _signup(client)
    account_id = await _create_account(client)
    csv_text = "Date,Description,Amount\n2026-01-05,CHARGE,4.50\n"
    imported = await _upload(client, account_id, csv_text)
    corrected = imported["suggested_mapping"] | {"sign": "positive_out"}
    response = await client.post(
        f"{IMPORTS}/{imported['id']}/mapping", json=corrected, headers=await _csrf(client)
    )
    assert response.status_code == 200, response.text
    await _commit(client, imported["id"])

    next_month = "Date,Description,Amount\n2026-02-05,CHARGE,3.25\n"
    second = await _upload(client, account_id, next_month)
    assert second["status"] == "previewed"
    assert second["confirmed_mapping"]["sign"] == "positive_out"
    (row,) = await _rows(client, second["id"])
    assert row["amount_minor"] == -325  # the correction, applied deterministically


async def test_headerless_files_never_match_a_profile_or_save_one(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)  # a profile now exists

    headerless = await _upload(client, account_id, HEADERLESS_CSV)
    assert headerless["status"] == "uploaded"  # always through mapping confirmation
    assert headerless["suggested_mapping"]["has_header"] is False

    previewed = await _previewed(client, account_id, HEADERLESS_CSV)
    await _commit(client, previewed["id"], {"include_duplicates": []})
    assert await ImportProfile.select().count() == 1  # headerless commits save nothing


async def test_header_order_is_part_of_the_shape_identity(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)

    reordered = "Amount,Description,Date\n-4.50,SOMETHING,2026-03-01\n"
    second = await _upload(client, account_id, reordered)
    assert second["status"] == "uploaded"  # ("amount","description","date") ≠ the profile


async def test_profiles_are_ledger_scoped_and_tenancy_answers_404(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)
    victim_profile = (await client.get(PROFILES)).json()["items"][0]
    await _logout(client)

    await _signup(client, email="other@example.com")
    other_account = await _create_account(client)
    # The same shape from another ledger meets no profile...
    second = await _upload(client, other_account, BANK_CSV)
    assert second["status"] == "uploaded"
    # ...their profile list is empty, and the victim's profile is unreachable.
    assert (await client.get(PROFILES)).json()["items"] == []
    response = await client.delete(
        f"{PROFILES}/{victim_profile['id']}", headers=await _csrf(client)
    )
    assert response.status_code == 404
    assert await ImportProfile.select().count() == 1


async def test_deleting_a_profile_restores_the_suggestion_path(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    await _committed(client, account_id, BANK_CSV)
    profile = (await client.get(PROFILES)).json()["items"][0]

    response = await client.delete(f"{PROFILES}/{profile['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    assert (await client.get(PROFILES)).json()["items"] == []

    second = await _upload(client, account_id, BANK_CSV)
    assert second["status"] == "uploaded"  # back through the inferrer + confirm


async def test_profile_list_pages_on_the_m3_convention(client) -> None:
    await _signup(client)
    account_id = await _create_account(client)
    for csv_text in (
        BANK_CSV,
        "Posted,Payee,Value\n2026-01-05,SHOP,-1.00\n",
        "Date,Memo,Amount\n2026-01-05,THING,-2.00\n",
    ):
        await _committed(client, account_id, csv_text)

    first = (await client.get(PROFILES, params={"limit": 2})).json()
    assert set(first) == {"items", "next_cursor"}
    assert len(first["items"]) == 2
    rest = (await client.get(PROFILES, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None
    assert (await client.get(PROFILES, params={"cursor": "junk"})).status_code == 400


async def test_openapi_describes_the_profile_routes(client) -> None:
    schema = (await client.get(SCHEMA_JSON)).json()
    for path in ("/api/v1/import-profiles", "/api/v1/import-profiles/{profile_id}"):
        assert path in schema["paths"], f"{path} missing from the OpenAPI document"
